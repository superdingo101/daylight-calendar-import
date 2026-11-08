"""Fail-closed mutant-level partitions for the expensive __init__.py shard.

Mutmut still discovers its normal associated tests and generates the full source
population on each worker. Only the *executed mutant identities* are partitioned.
No cached verdicts, test selection overrides or production code changes.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import subprocess
import sys

from scripts import mutation_shards as shards

MODULE = shards.SOURCE + "__init__.py"
MUTANT_PREFIX = "custom_components.daylight_calendar_import.x_"
SETUP_PREFIX = MUTANT_PREFIX + "async_setup_entry__mutmut_"
SEED = SETUP_PREFIX + "1"
PARTS = 5
CATALOG = Path("mutation-shards/split-catalog.json")
RESULTS = Path("mutation-shards/split-results")
SPLIT_DIAGNOSTICS = Path("split-diagnostics")
SHARD_DIAGNOSTICS = Path("shard-diagnostics/mutation-shard-diagnostics-1")
NAME = re.compile(r"custom_components\.daylight_calendar_import\.x_[A-Za-z_][A-Za-z_0-9]*__mutmut_([1-9][0-9]*)\Z")
DIAGNOSTIC_NAMES = ("mutation-survivors.txt", "mutation-survivor-names.txt",
                    "mutation-survivor-report.txt")


def meta() -> dict:
    path = Path("mutants") / (MODULE + ".meta")
    data = json.loads(path.read_text(encoding="utf-8"))
    verdicts = data.get("exit_code_by_key")
    if not isinstance(verdicts, dict) or not verdicts:
        raise ValueError("Missing complete init mutant metadata")
    if any(not isinstance(key, str) or NAME.fullmatch(key) is None for key in verdicts):
        raise ValueError("Malformed init mutant identity")
    return data


def partitions(population: list[str]) -> list[list[str]]:
    if not population or population != sorted(set(population)):
        raise ValueError("Incomplete or duplicate init mutant population")
    result = [[] for _ in range(PARTS)]
    for name in population:
        match = NAME.fullmatch(name)
        if match is None:
            raise ValueError("Unexpected init mutant identity")
        if name.startswith(SETUP_PREFIX):
            index = (int(match.group(1)) - 1) % (PARTS - 1)
        else:
            index = PARTS - 1
        result[index].append(name)
    if any(not part for part in result):
        raise ValueError("Empty init mutation partition")
    return result


def verified_catalog(path: Path = CATALOG) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if type(obj) is not dict:
        raise ValueError("Invalid split catalog")
    digest = obj.get("catalog_sha256")
    if digest != shards.sha256(shards.canonical({k: v for k, v in obj.items()
                                                if k != "catalog_sha256"})):
        raise ValueError("Split catalog checksum mismatch")
    manifest = shards.load_manifest(shards.MANIFEST)
    if (obj.get("schema") != 1 or obj.get("target_sha") != shards.git("rev-parse", "HEAD")
            or obj.get("target_sha") != manifest["target_sha"]
            or obj.get("manifest_sha256") != manifest["manifest_sha256"]
            or obj.get("module") != MODULE or obj.get("shard_index") != 1
            or obj.get("source_sha256") != shards.sha256(Path(MODULE).read_bytes())
            or obj.get("parts") != PARTS
            or shards.shard_entry(manifest, 1)["modules"] != [MODULE]):
        raise ValueError("Split catalog differs from exact SHA or source manifest")
    names = obj.get("population")
    if type(names) is not list or any(type(name) is not str for name in names):
        raise ValueError("Split catalog population malformed")
    if (obj.get("population_sha256") != shards.sha256(shards.canonical(names))
            or obj.get("assignments") != partitions(names)):
        raise ValueError("Split partition population or assignments disagree")
    return obj


def catalog() -> dict:
    """Enumerate new mutants from a one-mutant cold mutmut probe, never from an old run."""
    manifest = shards.load_manifest(shards.MANIFEST)
    shards.verify_manifest(manifest)
    if shards.shard_entry(manifest, 1)["modules"] != [MODULE]:
        raise ValueError("Shard 1 is no longer the sole init module")
    names = sorted(meta()["exit_code_by_key"])
    if SEED not in names:
        raise ValueError("Init probe mutant missing from the generated population")
    obj = {
        "schema": 1,
        "target_sha": manifest["target_sha"],
        "manifest_sha256": manifest["manifest_sha256"],
        "module": MODULE,
        "shard_index": 1,
        "source_sha256": shards.sha256(Path(MODULE).read_bytes()),
        "parts": PARTS,
        "population": names,
        "population_sha256": shards.sha256(shards.canonical(names)),
        "assignments": partitions(names),
    }
    obj["catalog_sha256"] = shards.sha256(shards.canonical(obj))
    shards.write_json(CATALOG, obj)
    verified_catalog()
    print("Generated exact-SHA init mutation catalog:", len(names),
          "mutants;", [len(part) for part in obj["assignments"]], "per worker")
    return obj


def selected(obj: dict, part: int) -> list[str]:
    if type(part) is not int or not 0 <= part < PARTS:
        raise ValueError("Unknown split worker index")
    return obj["assignments"][part]


def run(part: int) -> None:
    obj = verified_catalog()
    mutants = selected(obj, part)
    # mutmut 3.8 uses the given exact names to select both mutants and the
    # relevant clean-test union. Globs here would silently select no clean tests.
    subprocess.run(["mutmut", "run", *mutants], check=True)


def record(part: int, seconds: int) -> dict:
    obj = verified_catalog()
    shards.verify_manifest(shards.load_manifest(shards.MANIFEST))
    if type(seconds) is not int or seconds < 0:
        raise ValueError("Invalid split worker runtime")
    data = meta()
    if sorted(data["exit_code_by_key"]) != obj["population"]:
        raise ValueError("Mutmut-generated population disagrees with split catalog")
    from mutmut.stats import status_by_exit_code

    names = selected(obj, part)
    verdicts = {}
    durations = {}
    for name in names:
        code = data["exit_code_by_key"][name]
        if type(code) is not int:
            raise ValueError(f"Missing clean verdict for {name}")
        status = status_by_exit_code.get(code, "suspicious").replace(" ", "_")
        if status not in shards.FIELDS:
            raise ValueError(f"Unexpected status for {name}: {status}")
        verdicts[name] = status
        duration = data.get("durations_by_key", {}).get(name)
        if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
            durations[name] = duration

    counts = Counter(verdicts.values())
    stats = {field: counts.get(field, 0) for field in shards.FIELDS}
    stats["total"] = len(verdicts)
    shards.stats_checked(stats, require_nonempty=True)
    shards.validate_mutants({MODULE: verdicts}, [MODULE], stats)
    result = {
        "schema": 1, "complete": True, "index": 1, "part": part,
        "target_sha": obj["target_sha"], "manifest_sha256": obj["manifest_sha256"],
        "catalog_sha256": obj["catalog_sha256"],
        "population_sha256": obj["population_sha256"], "module": MODULE,
        "selection_sha256": shards.sha256(shards.canonical(names)),
        "mutants": {MODULE: verdicts}, "stats": stats, "seconds": seconds,
        "durations": durations, "environment": shards.environment(),
    }
    shards.write_json(RESULTS / f"split-1-{part}.json", result)
    # Export only this worker's verdicts; nonselected meta entries intentionally
    # have no verdict and must never enter diagnostics or the scored aggregate.
    from scripts.export_mutation_survivors import export_survivors
    from mutmut.mutation.data import SourceFileMutationData
    from mutmut.mutation.diff_apply import get_diff_for_mutant

    code = export_survivors(
        paths=[Path(MODULE)],
        metadata_factory=SourceFileMutationData,
        diff_for_mutant=get_diff_for_mutant,
        status_by_exit_code=status_by_exit_code,
        selected_mutants=set(names),
    )
    if code:
        raise ValueError("Failed to export complete split diagnostics")
    print(f"Recorded init partition {part}: {stats['total']} mutants in {seconds}s")
    return result


def combine() -> dict:
    obj = verified_catalog()
    shards.verify_manifest(shards.load_manifest(shards.MANIFEST))
    expected = {f"split-1-{part}.json" for part in range(PARTS)}
    present = {path.name for path in RESULTS.glob("*.json")}
    if expected != present:
        raise ValueError("Missing or unexpected split partition evidence")
    all_verdicts = {}
    totals = Counter()
    environment = None
    timings = {}
    all_durations = {}
    for part in range(PARTS):
        value = json.loads((RESULTS / f"split-1-{part}.json").read_text())
        names = selected(obj, part)
        if (value.get("schema") != 1 or value.get("complete") is not True
                or value.get("index") != 1 or type(value.get("part")) is not int
                or value["part"] != part
                or value.get("target_sha") != obj["target_sha"]
                or value.get("manifest_sha256") != obj["manifest_sha256"]
                or value.get("catalog_sha256") != obj["catalog_sha256"]
                or value.get("population_sha256") != obj["population_sha256"]
                or value.get("selection_sha256") != shards.sha256(shards.canonical(names))
                or value.get("module") != MODULE
                or type(value.get("seconds")) is not int or value["seconds"] < 0
                or set(value.get("mutants", {})) != {MODULE}
                or set(value["mutants"][MODULE]) != set(names)):
            raise ValueError(f"Split worker {part} is incomplete or incorrectly scoped")
        shards.validate_environment(value.get("environment"))
        if environment is None:
            environment = value["environment"]
        elif environment["sha256"] != value["environment"]["sha256"]:
            raise ValueError("Split worker resolved environments differ")
        counts = shards.stats_checked(value.get("stats"), require_nonempty=True)
        shards.validate_mutants(value["mutants"], [MODULE], counts)
        overlap = set(all_verdicts) & set(value["mutants"][MODULE])
        if overlap:
            raise ValueError("Duplicate init mutant results across workers")
        all_verdicts.update(value["mutants"][MODULE])
        totals.update(counts)
        timings[str(part)] = value["seconds"]
        durations = value.get("durations", {})
        if (not isinstance(durations, dict) or not set(durations).issubset(names)
                or any(type(t) not in (int, float) or not math.isfinite(t) or t < 0
                       for t in durations.values())):
            raise ValueError("Invalid per-mutant timing diagnostics")
        all_durations.update(durations)
    if sorted(all_verdicts) != obj["population"]:
        raise ValueError("Init mutant population is not completely accounted for")
    stats = {key: totals[key] for key in (*shards.FIELDS, "total")}
    shards.validate_mutants({MODULE: all_verdicts}, [MODULE], stats)
    combined = {
        "schema": shards.SCHEMA, "complete": True,
        "index": 1, "modules": [MODULE], "manifest_sha256": obj["manifest_sha256"],
        "target_sha": obj["target_sha"], "mutants": {MODULE: all_verdicts},
        "stats": stats, "environment": environment,
        "seconds": max(timings.values()), "split_seconds": timings,
        "mutant_durations": all_durations,
    }
    shards.write_json(Path("mutation-shards/results/shard-1.json"), combined)
    print("Combined init mutation split:", stats["total"],
          "unique complete mutants;", timings, "seconds")
    return combined



def compare_legacy() -> dict:
    """Prove the cold old and new shard 1 have identical scored mutant outcomes.

    Timeouts count as kills under the existing repository policy; retain raw
    status differences as diagnostics. Both runs must use the same checked-out
    SHA, resolved userland and full mutant identity population.
    """
    catalog = verified_catalog()
    old_path = Path("mutation-shards/legacy-shard-1.json")
    new_path = Path("mutation-shards/results/shard-1.json")
    old = json.loads(old_path.read_text(encoding="utf-8"))
    new = json.loads(new_path.read_text(encoding="utf-8"))
    for label, result in (("legacy", old), ("split", new)):
        if (result.get("schema") != shards.SCHEMA
                or result.get("complete") is not True
                or type(result.get("index")) is not int or result["index"] != 1
                or result.get("target_sha") != catalog["target_sha"]
                or result.get("manifest_sha256") != catalog["manifest_sha256"]
                or result.get("modules") != [MODULE]):
            raise ValueError(f"{label} result is not the same complete exact-SHA init shard")
        counts = shards.stats_checked(result.get("stats"), require_nonempty=True)
        shards.validate_mutants(result.get("mutants"), [MODULE], counts)
        shards.validate_environment(result.get("environment"))
        if sorted(result["mutants"][MODULE]) != catalog["population"]:
            raise ValueError(f"{label} result has a different mutant population")
    if old["environment"]["sha256"] != new["environment"]["sha256"]:
        raise ValueError("Legacy and split init shard executed in different environments")
    original = old["mutants"][MODULE]
    divided = new["mutants"][MODULE]
    normalized = lambda verdict: "killed" if verdict in ("timeout", "killed") else verdict
    differences = [
        {"mutant": name, "legacy": original[name], "split": divided[name]}
        for name in catalog["population"]
        if original[name] != divided[name]
    ]
    changed_scored = [
        item for item in differences
        if normalized(item["legacy"]) != normalized(item["split"])
    ]
    if changed_scored:
        raise ValueError(f"Legacy and split scored outcomes differ for {len(changed_scored)} mutants")
    proof = {
        "schema": 1, "equivalent": True,
        "target_sha": catalog["target_sha"],
        "environment_sha256": old["environment"]["sha256"],
        "manifest_sha256": catalog["manifest_sha256"],
        "catalog_sha256": catalog["catalog_sha256"],
        "population_sha256": catalog["population_sha256"],
        "mutant_count": len(original),
        "scored_outcomes_identical": True,
        "legacy_stats": old["stats"],
        "split_stats": new["stats"],
        "raw_status_differences": differences,
        "legacy_seconds": old["seconds"],
        "split_seconds": new["seconds"],
    }
    shards.write_json(Path("mutation-shards/legacy-init-equivalence.json"), proof)
    print("PASS: same-SHA, same-userland legacy and split init shard outcomes match for",
          len(original), "mutants; raw timing-status differences:", len(differences))
    return proof


def join_diagnostics() -> None:
    obj = verified_catalog()
    # First require every worker result; diagnostics are not a replacement for
    # the fail-closed scored result.
    expected = {f"mutation-split-diagnostics-{part}" for part in range(PARTS)}
    if {path.name for path in SPLIT_DIAGNOSTICS.iterdir() if path.is_dir()} != expected:
        raise ValueError("Missing or unexpected split diagnostic artifact")
    SHARD_DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
    for name in DIAGNOSTIC_NAMES:
        parts = []
        for index in range(PARTS):
            parts.append((SPLIT_DIAGNOSTICS / f"mutation-split-diagnostics-{index}" /
                          name).read_text())
        (SHARD_DIAGNOSTICS / name).write_text("".join(parts), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("catalog", "run", "record", "combine", "join-diagnostics", "compare-legacy"))
    parser.add_argument("--part", type=int)
    parser.add_argument("--seconds", type=int)
    args = parser.parse_args()
    try:
        if args.command == "catalog":
            catalog()
        elif args.command == "run":
            run(args.part)
        elif args.command == "record":
            record(args.part, args.seconds)
        elif args.command == "combine":
            combine()
        elif args.command == "compare-legacy":
            compare_legacy()
        else:
            join_diagnostics()
        return 0
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError,
            subprocess.CalledProcessError, ImportError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
