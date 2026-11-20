"""Deterministic, lossless subshards for the oversized __init__.py mutation set.

The seven other original shards stay unchanged. Each selected mutant runs in
one and only one clean runner, and aggregation uses the original global gate.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

from scripts import mutation_shards as ms

ORIGINAL_COUNT = 8
SPLIT_COUNT = 5
COUNT = ORIGINAL_COUNT + SPLIT_COUNT - 1
ORIGINAL_INDEX = 1
SPLIT_INDICES = (1, 8, 9, 10, 11)
MODULE = ms.SOURCE + "__init__.py"


def generate_population() -> list[str]:
    """Ask the pinned mutmut generator for the *actual* full-module identities."""
    from mutmut.mutation.file_mutation import mutate_file_contents
    from mutmut.utils.format_utils import get_mutant_name

    path = Path(MODULE)
    result = mutate_file_contents(str(path), path.read_text(encoding="utf-8"))
    names = sorted(get_mutant_name(path, item) for item in result.mutant_names)
    if not names or len(names) != len(set(names)):
        raise ValueError("Mutmut yielded an empty or duplicate mutant population")
    return names


def split_manifest(sha: str) -> dict:
    original = ms.create_manifest(sha, ORIGINAL_COUNT)
    if original["shards"][ORIGINAL_INDEX]["modules"] != [MODULE]:
        raise ValueError("The original shard 1 module assignment changed; refuse to split")
    population = generate_population()
    result = copy.deepcopy(original)
    result["shard_count"] = COUNT
    result["split_source"] = MODULE
    result["split_original_count"] = ORIGINAL_COUNT
    result["split_population"] = population
    for part, index in enumerate(SPLIT_INDICES):
        selected = population[part::SPLIT_COUNT]
        if not selected:
            raise ValueError("Empty mutant subshard")
        entry = dict(index=index, modules=[MODULE], estimated_weight=0,
                     mutant_names=selected)
        if index == ORIGINAL_INDEX:
            entry["estimated_weight"] = original["shards"][ORIGINAL_INDEX]["estimated_weight"]
            result["shards"][ORIGINAL_INDEX] = entry
        else:
            result["shards"].append(entry)
    result["manifest_sha256"] = ms.sha256(ms.canonical({
        k: v for k, v in result.items() if k != "manifest_sha256"
    }))
    verify_manifest(result)
    return result


def verify_manifest(manifest: dict) -> None:
    """Verify the old partition and the full, disjoint mutant-name partition.

    This check intentionally needs only the standard library: aggregators do
    not install mutmut. The planner and split workers additionally recompute
    the generator's identities from the exact checked-out production source.
    """
    if (manifest.get("shard_count") != COUNT
            or manifest.get("split_source") != MODULE
            or manifest.get("split_original_count") != ORIGINAL_COUNT):
        raise ValueError("Invalid split manifest configuration")
    original = ms.create_manifest(manifest["target_sha"], ORIGINAL_COUNT)
    for key in ("schema", "target_sha", "source_sha256", "config_sha256"):
        if manifest.get(key) != original[key]:
            raise ValueError(f"Split manifest changed original {key}")
    entries = manifest.get("shards")
    if not isinstance(entries, list) or len(entries) != COUNT:
        raise ValueError("Split manifest must contain every shard")
    for i, old in enumerate(original["shards"]):
        actual = entries[i]
        if {key: value for key, value in actual.items() if key != "mutant_names"} != old:
            raise ValueError(f"Original shard {i} module partition changed")
    population = manifest.get("split_population")
    prefix = MODULE.removesuffix("/__init__.py").replace("/", ".") + "."
    if (not isinstance(population, list) or not population
            or population != sorted(set(population))
            or any(not isinstance(name, str) or not name.startswith(prefix)
                   or "__mutmut_" not in name for name in population)):
        raise ValueError("Invalid or duplicated original mutant population")
    assigned = []
    for part, index in enumerate(SPLIT_INDICES):
        entry = entries[index]
        if (entry.get("index") != index or entry.get("modules") != [MODULE]
                or entry.get("mutant_names") != population[part::SPLIT_COUNT]
                or entry.get("estimated_weight") != 0 if index != ORIGINAL_INDEX else False):
            # The original entry retains its original weight.
            if index != ORIGINAL_INDEX:
                raise ValueError("Mutant slice does not match deterministic assignment")
        if index == ORIGINAL_INDEX:
            if entry.get("mutant_names") != population[0::SPLIT_COUNT]:
                raise ValueError("Original shard 1 mutant slice mismatch")
        assigned.extend(entry["mutant_names"])
    if sorted(assigned) != population or len(assigned) != len(set(assigned)):
        raise ValueError("Mutant population is missing or duplicated")
    for index in range(ORIGINAL_COUNT, COUNT):
        if entries[index]["index"] != index or entries[index]["estimated_weight"] != 0:
            raise ValueError("Unexpected additional shard")
    expected_checksum = ms.sha256(ms.canonical({
        key: val for key, val in manifest.items() if key != "manifest_sha256"
    }))
    if manifest.get("manifest_sha256") != expected_checksum:
        raise ValueError("Split manifest checksum mismatch")


def verify_generated(manifest: dict) -> None:
    verify_manifest(manifest)
    if generate_population() != manifest["split_population"]:
        raise ValueError("Mutmut's actual population differs from the manifest")


def is_split_entry(entry: dict) -> bool:
    return "mutant_names" in entry


def selected_verdicts(manifest: dict, entry: dict) -> tuple[dict, dict]:
    """Read only owned mutant verdicts; reject omissions and cross-shard work."""
    verify_generated(manifest)
    meta = Path("mutants") / (MODULE + ".meta")
    raw = json.loads(meta.read_text(encoding="utf-8"))
    codes = raw.get("exit_code_by_key")
    if not isinstance(codes, dict) or sorted(codes) != manifest["split_population"]:
        raise ValueError("Generated full-module population differs from the original")
    selected = set(entry["mutant_names"])
    if any(code is not None for name, code in codes.items() if name not in selected):
        raise ValueError("Another subshard's mutants were executed")
    from mutmut.stats import status_by_exit_code
    verdicts = {}
    for name in entry["mutant_names"]:
        code = codes[name]
        if type(code) is not int:
            raise ValueError(f"Incomplete mutant verdict for {name}")
        status = status_by_exit_code[code].replace(" ", "_")
        if status not in ms.FIELDS:
            raise ValueError(f"Unacceptable mutant result for {name}: {status}")
        verdicts[name] = status
    counts = Counter(verdicts.values())
    stats = {field: counts.get(field, 0) for field in ms.FIELDS}
    stats["total"] = len(verdicts)
    ms.validate_mutants({MODULE: verdicts}, [MODULE], stats)
    ms.stats_checked(stats, require_nonempty=True)
    return {MODULE: verdicts}, stats


def record(manifest: dict, entry: dict, index: int, out: Path, duration: int) -> dict:
    if type(duration) is not int or duration < 0:
        raise ValueError("Invalid mutation runtime")
    mutants, stats = selected_verdicts(manifest, entry)
    ms.write_json(ms.STATS, stats)
    result = dict(schema=ms.SCHEMA, manifest_sha256=manifest["manifest_sha256"],
                  target_sha=manifest["target_sha"], index=index,
                  modules=entry["modules"], complete=True, seconds=duration,
                  stats=stats, environment=ms.environment(), mutants=mutants)
    ms.write_json(out, result)
    print(f"Recorded split shard {index}: {stats['total']} exact mutants")
    return result


def run(manifest: dict, index: int) -> None:
    ms.verify_manifest(manifest)
    entry = ms.shard_entry(manifest, index)
    if is_split_entry(entry):
        verify_generated(manifest)
        command = ["mutmut", "run", *entry["mutant_names"]]
    else:
        command = ["mutmut", "run"]
    subprocess.run(command, check=True)


def diagnostics(manifest: dict, index: int) -> None:
    entry = ms.shard_entry(manifest, index)
    if is_split_entry(entry):
        _, stats = selected_verdicts(manifest, entry)
        ms.write_json(ms.STATS, stats)
    else:
        subprocess.run(["mutmut", "export-cicd-stats"], check=True)


def compare_parity(manifest: dict, original_manifest: dict,
                   original_result: dict, split_results: dict[int, dict]) -> dict:
    """The one-time proof compares *every* scored mutant identity and verdict."""
    ms.verify_manifest(manifest)
    ms.verify_manifest(original_manifest)
    if original_manifest["shard_count"] != ORIGINAL_COUNT:
        raise ValueError("Original proof is not the old eight-shard process")
    if original_manifest["target_sha"] != manifest["target_sha"]:
        raise ValueError("Parity runs use different source commits")
    if original_manifest["shards"][ORIGINAL_INDEX]["modules"] != [MODULE]:
        raise ValueError("Original shard 1 source changed")
    if (original_result.get("complete") is not True
            or original_result.get("index") != ORIGINAL_INDEX
            or original_result.get("target_sha") != manifest["target_sha"]
            or original_result.get("manifest_sha256") != original_manifest["manifest_sha256"]
            or original_result.get("modules") != [MODULE]):
        raise ValueError("Original shard evidence is missing or incomplete")
    ms.validate_environment(original_result.get("environment"))
    ms.validate_mutants(original_result.get("mutants"),
                        [MODULE], original_result.get("stats"))
    original_keys = original_result["mutants"][MODULE]
    if sorted(original_keys) != manifest["split_population"]:
        raise ValueError("Old and split mutant populations differ")
    combined: dict[str, str] = {}
    for index in SPLIT_INDICES:
        result = split_results.get(index)
        entry = manifest["shards"][index]
        if (not isinstance(result, dict) or result.get("complete") is not True
                or result.get("index") != index or result.get("target_sha") != manifest["target_sha"]
                or result.get("manifest_sha256") != manifest["manifest_sha256"]
                or result.get("modules") != [MODULE]):
            raise ValueError(f"Missing or incorrect split shard {index}")
        ms.validate_environment(result.get("environment"))
        if result["environment"]["sha256"] != original_result["environment"]["sha256"]:
            raise ValueError("Original and split engines ran in different environments")
        ms.validate_mutants(result.get("mutants"), [MODULE], result.get("stats"))
        identities = result["mutants"][MODULE]
        if sorted(identities) != entry["mutant_names"]:
            raise ValueError(f"Split shard {index} executed incorrect mutants")
        overlap = set(combined).intersection(identities)
        if overlap:
            raise ValueError("Duplicate mutant identity across split shards")
        combined.update(identities)
    if sorted(combined) != manifest["split_population"]:
        raise ValueError("Split execution omitted mutants")
    old = ms.scored_mutants({MODULE: original_keys})[MODULE]
    new = ms.scored_mutants({MODULE: combined})[MODULE]
    if old != new:
        mismatch = [name for name in manifest["split_population"] if old[name] != new[name]]
        raise ValueError(f"Split changed scored mutant outcomes: {mismatch[:5]}")
    proof = dict(schema=ms.SCHEMA, equivalent=True,
                 target_sha=manifest["target_sha"],
                 execution_environment_sha256=original_result["environment"]["sha256"],
                 mutant_count=len(combined),
                 mutant_outcomes_sha256=ms.sha256(ms.canonical(old)),
                 original_shards=ORIGINAL_COUNT, split_shards=COUNT)
    return proof


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "diagnostics", "compare"])
    parser.add_argument("--index", type=int)
    parser.add_argument("--original-result", type=Path,
                        default=Path("mutation-shards/parity/original.json"))
    parser.add_argument("--original-manifest", type=Path,
                        default=Path("mutation-shards/original-manifest.json"))
    parser.add_argument("--results", type=Path, default=Path("mutation-shards/results"))
    parser.add_argument("--out", type=Path, default=Path("mutation-shards/parity.json"))
    args = parser.parse_args()
    try:
        manifest = ms.load_manifest(ms.MANIFEST)
        if args.command in ("run", "diagnostics"):
            if args.index is None:
                parser.error("--index required")
            if args.command == "run":
                run(manifest, args.index)
            else:
                diagnostics(manifest, args.index)
        else:
            original = ms.load_manifest(args.original_manifest)
            original_result = json.loads(args.original_result.read_text())
            selected = {i: json.loads((args.results / f"shard-{i}.json").read_text())
                        for i in SPLIT_INDICES}
            proof = compare_parity(manifest, original, original_result, selected)
            ms.write_json(args.out, proof)
            print(f"One-time parity proved for {proof['mutant_count']} identical scored mutants")
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
