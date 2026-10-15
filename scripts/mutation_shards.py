"""Full, clean mutation sharding with fail-closed aggregate verification.

Cold executor validated against an exact-SHA unsharded run in PR #129.
No cross-PR verdict caches and no partial-job successes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import platform
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SOURCE = "custom_components/daylight_calendar_import/"
SCHEMA = 1
SHARDS = 8
MANIFEST = Path("mutation-shards/manifest.json")
STATS = Path("mutants/mutmut-cicd-stats.json")
FIELDS = ("killed", "survived", "timeout", "no_tests", "suspicious", "segfault", "skipped")
MAX_SHARDS = 16


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def canonical(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def environment() -> dict:
    """Bind verdicts to the executed userland, interpreter and package set."""
    packages = sorted((d.metadata["Name"].lower().replace("_", "-"), d.version)
                      for d in importlib.metadata.distributions())
    details = {"python": platform.python_version(), "implementation": platform.python_implementation(),
               "system": platform.system(), "machine": platform.machine(),
               "packages": packages}
    execution_image = os.environ.get("MUTATION_EXECUTION_IMAGE", "")
    if execution_image:
        if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", execution_image):
            raise ValueError("Mutation execution image must be pinned by digest")
        details.update(
            execution_image=execution_image,
            node=subprocess.check_output(["node", "--version"], text=True).strip(),
        )
    else:
        # Local/legacy host executions still require identical host images.
        details.update(runner_image=os.environ.get("ImageOS", ""),
                       runner_image_version=os.environ.get("ImageVersion", ""))
    # JSON roundtrip removes tuple/list differences when loading evidence.
    details = json.loads(canonical(details))
    # Keep the host runner build in diagnostics even for digest-pinned
    # container runs. Only the executed userland belongs to the identity;
    # local/legacy host executions retain the strict image-version identity.
    return {"details": details, "sha256": sha256(canonical(details)),
            "runner_image_version": os.environ.get("ImageVersion", "")}


def validate_environment(value: dict) -> None:
    if (not isinstance(value, dict) or not isinstance(value.get("details"), dict)
            or value.get("sha256") != sha256(canonical(value["details"]))
            or not isinstance(value.get("runner_image_version"), str)):
        raise ValueError("Missing or corrupt resolved environment evidence")


def source_paths() -> list[str]:
    names = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
    files = []
    for item in names:
        if not item:
            continue
        name = os.fsdecode(item)
        if name.startswith(SOURCE) and name.endswith(".py") and not name.startswith(SOURCE + "frontend/"):
            if not Path(name).is_file():
                raise ValueError(f"Tracked source is missing: {name}")
            files.append(name)
    return sorted(files)


def create_manifest(sha: str, count: int = SHARDS) -> dict:
    if count < 1 or count > MAX_SHARDS:
        raise ValueError(f"Shard count must be 1..{MAX_SHARDS}")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or git("rev-parse", "HEAD") != sha:
        raise ValueError("Target SHA must equal exact checked-out HEAD")
    paths = source_paths()
    if len(paths) < count:
        raise ValueError("Cannot create more shards than source modules")
    # Source size is a coarse cost heuristic; actual mutmut per-module timings
    # can be used to rebalance in a later measurement-only change.
    weighted = [(p, max(1, Path(p).stat().st_size)) for p in paths]
    bins = [{"modules": [], "estimated_weight": 0} for _ in range(count)]
    for path, weight in sorted(weighted, key=lambda item: (-item[1], item[0])):
        dest = min(range(count), key=lambda i: (bins[i]["estimated_weight"], i))
        bins[dest]["modules"].append(path)
        bins[dest]["estimated_weight"] += weight
    for entry in bins:
        entry["modules"].sort()
        if not entry["modules"]:
            raise ValueError("Empty mutation shard")
    source_fingerprint = sha256(canonical({
        p: sha256(Path(p).read_bytes()) for p in paths
    }))
    config_fingerprint = sha256(canonical({
        p: sha256(Path(p).read_bytes())
        for p in ("pyproject.toml", "requirements_test.txt", "mutation-baseline.json")
    }))
    manifest = {
        "schema": SCHEMA, "target_sha": sha, "source_sha256": source_fingerprint,
        "config_sha256": config_fingerprint, "shard_count": count,
        "shards": [{"index": i, **entry} for i, entry in enumerate(bins)],
    }
    manifest["manifest_sha256"] = sha256(canonical(manifest))
    return manifest


def verify_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("Invalid manifest")
    claimed = manifest.get("manifest_sha256")
    if not isinstance(claimed, str) or claimed != sha256(canonical({
        k: v for k, v in manifest.items() if k != "manifest_sha256"
    })):
        raise ValueError("Manifest checksum mismatch")
    expected = create_manifest(manifest["target_sha"], manifest["shard_count"])
    if manifest != expected:
        raise ValueError("Manifest no longer matches exact checkout and module partition")


def stats_checked(stats: dict, *, require_nonempty: bool) -> dict[str, int]:
    if not isinstance(stats, dict) or stats.get("check_was_interrupted_by_user"):
        raise ValueError("Mutation statistics missing or interrupted")
    normalized = {}
    for field in FIELDS:
        value = stats.get(field)
        if type(value) is not int or value < 0:
            raise ValueError(f"Bad mutation count: {field}={value!r}")
        normalized[field] = value
    total = stats.get("total")
    if type(total) is not int or total < 0 or (require_nonempty and total == 0):
        raise ValueError(f"Invalid total mutant count: {total!r}")
    if sum(normalized.values()) != total:
        raise ValueError("Incomplete statistics: statuses do not sum to total")
    if normalized["skipped"]:
        raise ValueError("Clean mutation validation skipped mutants")
    normalized["total"] = total
    return normalized



def collect_mutants(modules: list[str], *, statuses: dict | None = None) -> dict:
    """Read every generated identity and verdict without spawning a CLI per mutant."""
    if statuses is None:
        # Keep the policy helper importable in HA compatibility tests, which
        # deliberately do not install the mutation engine.
        from mutmut.stats import status_by_exit_code
        statuses = status_by_exit_code
    result = {}
    for module in modules:
        path = Path("mutants") / (module + ".meta")
        # mutmut 3.8 creates metadata even for selected files with zero mutants.
        # Missing metadata must not masquerade as an empty mutation population.
        metadata = json.loads(path.read_text())
        verdicts = metadata.get("exit_code_by_key")
        if not isinstance(verdicts, dict):
            raise ValueError(f"Missing mutant verdicts for {module}")
        result[module] = {}
        for identity, exit_code in verdicts.items():
            if type(exit_code) is not int:
                raise ValueError(f"Incomplete or invalid mutant verdict: {identity}")
            status = statuses.get(exit_code, "suspicious").replace(" ", "_")
            if status not in FIELDS:
                raise ValueError(f"Unsupported/incomplete mutant outcome: {status}")
            result[module][identity] = status
    return result


def validate_mutants(mutants: dict, modules: list[str], stats: dict) -> None:
    """Bind canonical per-source mutant identities to the exact raw counts."""
    if not isinstance(mutants, dict) or sorted(mutants) != sorted(modules):
        raise ValueError("Mutant identity map has missing or unexpected source modules")
    counts = dict.fromkeys(FIELDS, 0)
    seen = set()
    for module, verdicts in mutants.items():
        if not isinstance(verdicts, dict):
            raise ValueError(f"Malformed mutant identity map for {module}")
        for identity, status in verdicts.items():
            if not isinstance(identity, str) or not identity or identity in seen:
                raise ValueError("Missing or duplicated mutant identity")
            if not isinstance(status, str) or status not in FIELDS:
                raise ValueError("Invalid mutant outcome")
            seen.add(identity)
            counts[status] += 1
    counts["total"] = len(seen)
    if counts != stats_checked(stats, require_nonempty=True):
        raise ValueError("Mutant identity outcomes do not match reported statistics")


def scored_mutants(mutants: dict) -> dict:
    # Preserve the existing quality policy for each identity, not just totals.
    return {module: {identity: "killed" if status == "timeout" else status
                     for identity, status in verdicts.items()}
            for module, verdicts in mutants.items()}


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def shard_entry(manifest: dict, index: int) -> dict:
    if index < 0 or index >= manifest["shard_count"]:
        raise ValueError("Shard index outside manifest")
    entry = manifest["shards"][index]
    if entry["index"] != index or not entry["modules"]:
        raise ValueError("Shard manifest is malformed")
    return entry


def plan(target_sha: str, count: int, out: Path) -> dict:
    result = create_manifest(target_sha, count)
    write_json(out, result)
    return result


def prepare(manifest_path: Path, index: int) -> dict:
    manifest = load_manifest(manifest_path)
    verify_manifest(manifest)
    entry = shard_entry(manifest, index)
    if Path(".pr-mutation-pyproject-original").exists():
        raise ValueError("Mutation configuration backup already exists")
    from scripts.pr_mutation import configure_only_mutate
    configure_only_mutate(entry["modules"])
    print(f"Prepared shard {index}: {len(entry['modules'])} modules")
    return entry


def record(manifest_path: Path, index: int, out: Path, duration: int) -> dict:
    manifest = load_manifest(manifest_path)
    # The runner must restore pyproject.toml before validation and recording.
    verify_manifest(manifest)
    entry = shard_entry(manifest, index)
    stats = stats_checked(json.loads(STATS.read_text()), require_nonempty=True)
    if duration < 0:
        raise ValueError("Invalid elapsed time")
    mutants = collect_mutants(entry["modules"])
    validate_mutants(mutants, entry["modules"], stats)
    result = {
        "schema": SCHEMA, "manifest_sha256": manifest["manifest_sha256"],
        "target_sha": manifest["target_sha"], "index": index,
        "modules": entry["modules"], "complete": True,
        "seconds": duration, "stats": stats, "environment": environment(),
        "mutants": mutants,
    }
    write_json(out, result)
    print(f"Recorded shard {index}: {stats['total']} mutants in {duration}s")
    return result


def reduce(manifest_path: Path, results_dir: Path, out: Path) -> dict:
    manifest = load_manifest(manifest_path)
    verify_manifest(manifest)
    expected = {f"shard-{i}.json" for i in range(manifest["shard_count"])}
    present = {p.name for p in results_dir.glob("*.json")}
    if present != expected:
        raise ValueError(f"Missing or unexpected shard results: missing={sorted(expected-present)}, extra={sorted(present-expected)}")
    observed_modules = []
    sums = {key: 0 for key in (*FIELDS, "total")}
    runtimes = {}
    resolved_environment = None
    runner_image_versions = {}
    all_mutants = {}
    for index in range(manifest["shard_count"]):
        obj = json.loads((results_dir / f"shard-{index}.json").read_text(encoding="utf-8"))
        entry = shard_entry(manifest, index)
        if (obj.get("schema") != SCHEMA or obj.get("complete") is not True
                or obj.get("target_sha") != manifest["target_sha"]
                or obj.get("manifest_sha256") != manifest["manifest_sha256"]
                or type(obj.get("index")) is not int or obj["index"] != index
                or obj.get("modules") != entry["modules"]):
            raise ValueError(f"Shard {index} missing success evidence or manifest mismatch")
        current_environment = obj.get("environment")
        validate_environment(current_environment)
        if resolved_environment is None:
            resolved_environment = current_environment
        elif resolved_environment["sha256"] != current_environment["sha256"]:
            raise ValueError("Shard resolved environments differ")
        runner_image_versions[str(index)] = current_environment["runner_image_version"]
        counts = stats_checked(obj.get("stats"), require_nonempty=True)
        validate_mutants(obj.get("mutants"), entry["modules"], counts)
        all_mutants.update(obj["mutants"])
        for key, value in counts.items():
            sums[key] += value
        secs = obj.get("seconds")
        if type(secs) is not int or secs < 0:
            raise ValueError(f"Shard {index} missing valid runtime")
        runtimes[str(index)] = secs
        observed_modules.extend(entry["modules"])
    if sorted(observed_modules) != source_paths() or len(set(observed_modules)) != len(observed_modules):
        raise ValueError("Module coverage is incomplete or duplicated")
    validate_mutants(all_mutants, source_paths(), sums)
    score = 100 * (sums["killed"] + sums["timeout"]) / (sums["total"] - sums["skipped"]) if sums["total"] > sums["skipped"] else 0
    result = {
        "schema": SCHEMA, "mode": "complete-sharded", "target_sha": manifest["target_sha"],
        "manifest_sha256": manifest["manifest_sha256"],
        "shard_count": manifest["shard_count"], "stats": sums,
        "score": round(score, 6), "shard_seconds": runtimes,
        "environment": resolved_environment,
        "runner_image_versions": runner_image_versions,
        "modules": sorted(observed_modules), "mutants": all_mutants,
        "complete": True,
    }
    # Keep the original quality checker and exactly its global threshold.
    # A failing score deliberately retains its output for investigation.
    write_json(out, result)
    write_json(STATS, sums)
    return result


def record_unsharded(manifest_path: Path, duration: int, out: Path) -> dict:
    manifest = load_manifest(manifest_path)
    verify_manifest(manifest)
    if type(duration) is not int or duration < 0:
        raise ValueError("Invalid elapsed time")
    result = {
        "schema": SCHEMA, "mode": "complete-unsharded", "complete": True,
        "target_sha": manifest["target_sha"],
        "manifest_sha256": manifest["manifest_sha256"],
        "modules": source_paths(), "seconds": duration,
        "environment": environment(),
        "stats": stats_checked(json.loads(STATS.read_text()), require_nonempty=True),
    }
    result["mutants"] = collect_mutants(result["modules"])
    validate_mutants(result["mutants"], result["modules"], result["stats"])
    write_json(out, result)
    return result


def compare(manifest_path: Path, sharded_path: Path, unsharded_path: Path, out: Path) -> dict:
    """Reject a mixed-environment or incomplete engine benchmark."""
    manifest = load_manifest(manifest_path)
    verify_manifest(manifest)
    sharded = json.loads(sharded_path.read_text())
    unsharded = json.loads(unsharded_path.read_text())
    for result, mode in ((sharded, "complete-sharded"), (unsharded, "complete-unsharded")):
        if (result.get("schema") != SCHEMA or result.get("mode") != mode
                or result.get("complete") is not True
                or result.get("target_sha") != manifest["target_sha"]
                or result.get("manifest_sha256") != manifest["manifest_sha256"]):
            raise ValueError("Engine evidence is incomplete or belongs to a different target")
        validate_environment(result.get("environment"))
        stats = stats_checked(result.get("stats"), require_nonempty=True)
        if result.get("modules") != source_paths():
            raise ValueError("Engine evidence omits production source")
        validate_mutants(result.get("mutants"), source_paths(), stats)
        if any(stats[key] for key in ("no_tests", "suspicious", "segfault")):
            raise ValueError("Engine benchmark contains error statuses")
    if sharded.get("shard_count") != manifest["shard_count"]:
        raise ValueError("Sharded comparison partition count differs")
    if unsharded.get("modules") != source_paths():
        raise ValueError("Unsharded evidence omits production source")
    if sharded["environment"]["sha256"] != unsharded["environment"]["sha256"]:
        raise ValueError("Sharded and unsharded resolved environments differ")
    a, b = sharded["stats"], unsharded["stats"]
    # Timeouts retain the repository's killed-mutant score semantics. A
    # kill/timeout split can vary with scheduling without changing the score.
    if (any(a[key] != b[key] for key in ("total", "survived", "skipped"))
            or a["killed"] + a["timeout"] != b["killed"] + b["timeout"]):
        raise ValueError("Engine mutant population or scored outcomes differ")
    if scored_mutants(sharded["mutants"]) != scored_mutants(unsharded["mutants"]):
        raise ValueError("Engine mutant identities or per-mutant outcomes differ")
    result = {"schema": SCHEMA, "equivalent": True,
              "target_sha": manifest["target_sha"],
              "environment_sha256": sharded["environment"]["sha256"],
              "sharded_stats": a, "unsharded_stats": b,
              "mutant_population_sha256": sha256(canonical(scored_mutants(sharded["mutants"]))) }
    write_json(out, result)
    return result


def join_diagnostics(manifest_path: Path, directory: Path, out: Path) -> None:
    manifest = load_manifest(manifest_path)
    verify_manifest(manifest)
    names = ("mutation-survivors.txt", "mutation-survivor-names.txt", "mutation-survivor-report.txt")
    # Read every expected file first. Missing diagnostics cannot silently
    # produce a partial report or an apparently successful release proof.
    contents = {name: [] for name in names}
    for index in range(manifest["shard_count"]):
        for name in names:
            contents[name].append((directory / f"mutation-shard-diagnostics-{index}" / name).read_text())
    out.mkdir(parents=True, exist_ok=True)
    for name, parts in contents.items():
        (out / name).write_text("".join(parts))


def main() -> int:
    cli = argparse.ArgumentParser()
    parts = cli.add_subparsers(dest="command", required=True)
    p = parts.add_parser("plan")
    p.add_argument("--sha", required=True)
    p.add_argument("--count", type=int, default=SHARDS)
    p.add_argument("--out", type=Path, default=MANIFEST)
    p = parts.add_parser("prepare")
    p.add_argument("--manifest", type=Path, default=MANIFEST)
    p.add_argument("--index", type=int, required=True)
    p = parts.add_parser("record")
    p.add_argument("--manifest", type=Path, default=MANIFEST)
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--seconds", required=True, type=int)
    p = parts.add_parser("reduce")
    p.add_argument("--manifest", type=Path, default=MANIFEST)
    p.add_argument("--results", type=Path, default=Path("mutation-shards/results"))
    p.add_argument("--out", type=Path, default=Path("mutation-shards/aggregate.json"))
    p = parts.add_parser("record-unsharded")
    p.add_argument("--manifest", type=Path, default=MANIFEST)
    p.add_argument("--seconds", required=True, type=int)
    p.add_argument("--out", type=Path, default=Path("mutation-shards/unsharded.json"))
    p = parts.add_parser("compare")
    p.add_argument("--manifest", type=Path, default=MANIFEST)
    p.add_argument("--sharded", type=Path, default=Path("mutation-shards/aggregate.json"))
    p.add_argument("--unsharded", type=Path, default=Path("mutation-shards/unsharded.json"))
    p.add_argument("--out", type=Path, default=Path("mutation-shards/equivalence.json"))
    p = parts.add_parser("join-diagnostics")
    p.add_argument("--manifest", type=Path, default=MANIFEST)
    p.add_argument("--directory", type=Path, default=Path("shard-diagnostics"))
    p.add_argument("--out", type=Path, default=Path("."))
    args = cli.parse_args()
    try:
        if args.command == "plan":
            result = plan(args.sha, args.count, args.out)
            print(f"Mutation shard manifest: {result['shard_count']} shards, {len(source_paths())} source modules")
        elif args.command == "prepare":
            prepare(args.manifest, args.index)
        elif args.command == "record":
            record(args.manifest, args.index, args.out, args.seconds)
        elif args.command == "join-diagnostics":
            join_diagnostics(args.manifest, args.directory, args.out)
        elif args.command == "record-unsharded":
            record_unsharded(args.manifest, args.seconds, args.out)
        elif args.command == "compare":
            compare(args.manifest, args.sharded, args.unsharded, args.out)
            print("Exact-SHA mutation engines have equivalent scored outcomes in the same environment")
        else:
            result = reduce(args.manifest, args.results, args.out)
            print(f"Complete sharded mutation score: {result['score']:.2f}% across {result['stats']['total']} mutants")
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
