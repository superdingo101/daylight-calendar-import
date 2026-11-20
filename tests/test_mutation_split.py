"""CI-only fail-closed tests for splitting the oversized mutmut module shard."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import types

import pytest

from scripts import mutation_shards as ms
from scripts import mutation_split as split


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.invalid")
    Path("pyproject.toml").write_text('[tool.mutmut]\nsource_paths = ["custom_components/daylight_calendar_import/"]\n')
    Path("requirements_test.txt").write_text("mutmut==3.8.0\n")
    Path("mutation-baseline.json").write_text('{"minimum_score":95.22}\n')
    sizes = [("storage.py", 4000), ("__init__.py", 3000)]
    sizes.extend((f"module_{i}.py", 1000 - i * 100) for i in range(6))
    for name, size in sizes:
        path = Path(ms.SOURCE + name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def tested(x):\n    return x + 1\n" + "#" * size + "\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    sha = git("rev-parse", "HEAD")
    names = [split.MODULE.removesuffix("/__init__.py").replace("/", ".")
             + f".x_tested__mutmut_{i}" for i in range(1, 51)]
    monkeypatch.setattr(split, "generate_population", lambda: sorted(names))
    legacy = ms.create_manifest(sha, 8)
    assert legacy["shards"][1]["modules"] == [split.MODULE]
    return sha, legacy, sorted(names)


def data_for(shard, manifest, environment=None):
    environment = environment or ms.environment()
    if "mutant_names" in shard:
        verdicts = {split.MODULE: {n: "killed" for n in shard["mutant_names"]}}
    else:
        verdicts = {module: {module + ".x_test__mutmut_1": "killed"}
                    for module in shard["modules"]}
    total = sum(len(items) for items in verdicts.values())
    counts = {field: 0 for field in ms.FIELDS}
    counts.update(killed=total, total=total)
    return dict(schema=ms.SCHEMA, manifest_sha256=manifest["manifest_sha256"],
                target_sha=manifest["target_sha"], complete=True,
                index=shard["index"], modules=shard["modules"],
                seconds=9, environment=environment, mutants=verdicts, stats=counts)


def test_exact_split_preserves_original_seven_module_shards(project):
    sha, legacy, names = project
    manifest = ms.create_manifest(sha, 12)
    ms.verify_manifest(manifest)
    assert manifest == ms.create_manifest(sha, 12)
    assert manifest["shard_count"] == 12
    assert manifest["split_population"] == names
    assert manifest["shards"][0] == legacy["shards"][0]
    assert manifest["shards"][2:8] == legacy["shards"][2:8]
    assignments = [name for i in split.SPLIT_INDICES
                   for name in manifest["shards"][i]["mutant_names"]]
    assert sorted(assignments) == names
    assert len(set(assignments)) == len(names)
    assert all(manifest["shards"][i]["modules"] == [split.MODULE]
               for i in split.SPLIT_INDICES)


@pytest.mark.parametrize("change", ["duplicate", "missing", "shift", "old_module", "population", "checksum"])
def test_manifest_refuses_lost_or_duplicated_source_or_mutants(project, change):
    sha, _, _ = project
    manifest = ms.create_manifest(sha, 12)
    if change == "duplicate":
        manifest["shards"][8]["mutant_names"][0] = manifest["shards"][1]["mutant_names"][0]
    elif change == "missing":
        manifest["shards"][8]["mutant_names"].pop()
    elif change == "shift":
        manifest["shards"][2]["modules"] = [split.MODULE]
    elif change == "old_module":
        manifest["shards"][1]["modules"] = [ms.SOURCE + "storage.py"]
    elif change == "population":
        manifest["split_population"].pop()
    else:
        manifest["manifest_sha256"] = "0" * 64
    if change != "checksum":
        manifest["manifest_sha256"] = ms.sha256(ms.canonical({
            k: v for k, v in manifest.items() if k != "manifest_sha256"
        }))
    with pytest.raises(ValueError):
        ms.verify_manifest(manifest)


def test_reducer_requires_every_subshard_and_merges_without_duplicate_coverage(project):
    sha, _, names = project
    manifest = ms.plan(sha, 12, ms.MANIFEST)
    directory = Path("mutation-shards/results")
    directory.mkdir(exist_ok=True, parents=True)
    for entry in manifest["shards"]:
        ms.write_json(directory / f"shard-{entry['index']}.json",
                      data_for(entry, manifest))
    output = ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))
    assert output["shard_count"] == 12
    assert sorted(output["mutants"][split.MODULE]) == names
    assert sorted(output["modules"]) == ms.source_paths()
    assert output["score"] == 100
    assert output["stats"]["skipped"] == 0
    (directory / "shard-11.json").unlink()
    with pytest.raises(ValueError, match="Missing or unexpected"):
        ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))


def test_reducer_rejects_wrong_mutant_even_when_score_is_unchanged(project):
    sha, _, _ = project
    manifest = ms.plan(sha, 12, ms.MANIFEST)
    directory = Path("mutation-shards/results")
    directory.mkdir(exist_ok=True, parents=True)
    for entry in manifest["shards"]:
        ms.write_json(directory / f"shard-{entry['index']}.json",
                      data_for(entry, manifest))
    path = directory / "shard-8.json"
    value = json.loads(path.read_text())
    name = next(iter(value["mutants"][split.MODULE]))
    value["mutants"][split.MODULE].pop(name)
    value["mutants"][split.MODULE]["forged.x_other__mutmut_1"] = "killed"
    ms.write_json(path, value)
    with pytest.raises(ValueError, match="omitted or replaced"):
        ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))


def test_split_record_rejects_incomplete_and_cross_shard_results(project, monkeypatch):
    sha, _, names = project
    manifest = ms.plan(sha, 12, ms.MANIFEST)
    entry = manifest["shards"][1]
    path = Path("mutants") / (split.MODULE + ".meta")
    path.parent.mkdir(exist_ok=True, parents=True)
    codes = {name: (1 if name in entry["mutant_names"] else None) for name in names}
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    mutmut = types.ModuleType("mutmut")
    fake_stats = types.ModuleType("mutmut.stats")
    fake_stats.status_by_exit_code = {0: "survived", 1: "killed", 36: "timeout"}
    monkeypatch.setitem(sys.modules, "mutmut", mutmut)
    monkeypatch.setitem(sys.modules, "mutmut.stats", fake_stats)

    output = ms.record(ms.MANIFEST, 1, Path("mutation-shards/results/shard-1.json"), 17)
    assert output["stats"]["total"] == len(entry["mutant_names"])
    assert output["stats"]["killed"] == len(entry["mutant_names"])
    assert output["stats"]["skipped"] == 0
    assert sorted(output["mutants"][split.MODULE]) == entry["mutant_names"]
    assert json.loads(ms.STATS.read_text()) == output["stats"]

    codes[entry["mutant_names"][0]] = None
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    with pytest.raises(ValueError, match="Incomplete"):
        ms.record(ms.MANIFEST, 1, Path("mutation-shards/results/shard-1.json"), 17)
    codes[entry["mutant_names"][0]] = 1
    codes[manifest["shards"][8]["mutant_names"][0]] = 1
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    with pytest.raises(ValueError, match="Another subshard"):
        ms.record(ms.MANIFEST, 1, Path("mutation-shards/results/shard-1.json"), 17)
    codes.pop(entry["mutant_names"][0])
    path.write_text(json.dumps({"exit_code_by_key": codes}))
    with pytest.raises(ValueError, match="population differs"):
        ms.record(ms.MANIFEST, 1, Path("mutation-shards/results/shard-1.json"), 17)


def test_split_run_names_every_selected_mutant_once(project, monkeypatch):
    sha, _, names = project
    manifest = ms.create_manifest(sha, 12)
    commands = []
    real_run = subprocess.run

    def run_without_executing_mutants(command, **kwargs):
        if command[:1] == ["mutmut"]:
            commands.append((command, kwargs.get("check")))
            return subprocess.CompletedProcess(command, 0)
        return real_run(command, **kwargs)

    monkeypatch.setattr(split.subprocess, "run", run_without_executing_mutants)
    split.run(manifest, 8)
    assert commands == [(["mutmut", "run", *manifest["shards"][8]["mutant_names"]], True)]
    split.run(manifest, 0)
    assert commands[-1] == (["mutmut", "run"], True)


def test_one_time_legacy_vs_split_verdict_parity_is_per_identity(project):
    sha, original, names = project
    manifest = ms.create_manifest(sha, 12)
    env = ms.environment()
    original_result = data_for(original["shards"][1], original, env)
    original_result["mutants"] = {split.MODULE: {name: "killed" for name in names}}
    original_result["stats"]["killed"] = original_result["stats"]["total"] = len(names)
    results = {i: data_for(manifest["shards"][i], manifest, env)
               for i in split.SPLIT_INDICES}
    proof = split.compare_parity(manifest, original, original_result, results)
    assert proof["equivalent"] is True
    assert proof["mutant_count"] == len(names)
    assert proof["original_shards"] == 8 and proof["split_shards"] == 12

    bad = copy.deepcopy(results)
    name = manifest["shards"][8]["mutant_names"][0]
    bad[8]["mutants"][split.MODULE][name] = "survived"
    bad[8]["stats"]["killed"] -= 1
    bad[8]["stats"]["survived"] += 1
    with pytest.raises(ValueError, match="changed scored mutant outcomes"):
        split.compare_parity(manifest, original, original_result, bad)

    missing = copy.deepcopy(results)
    missing[8]["mutants"][split.MODULE].pop(name)
    with pytest.raises(ValueError):
        split.compare_parity(manifest, original, original_result, missing)

    wrong_commit = copy.deepcopy(original_result)
    wrong_commit["target_sha"] = "0" * 40
    with pytest.raises(ValueError, match="Original shard"):
        split.compare_parity(manifest, original, wrong_commit, results)

    wrong_env = copy.deepcopy(results)
    wrong_env[8]["environment"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="environment"):
        split.compare_parity(manifest, original, original_result, wrong_env)


def test_shard_diagnostic_export_imports_ci_package_reliably():
    """Do not run the exporter by path: scripts imports need the repo root."""
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow_file = root / ".github/workflows/mutation-sharded.yml"
    if not workflow_file.exists() and root.name == "mutants":
        pytest.skip("CI workflow is not present in the mutmut test sandbox")
    workflow = yaml.safe_load(workflow_file.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["shards"]["steps"]
    diagnostic = next(s for s in steps if s.get("name") == "Export shard diagnostics even on failure")
    assert "python -m scripts.export_mutation_survivors" in diagnostic["run"]
    assert "python scripts/export_mutation_survivors.py" not in diagnostic["run"]
    assert diagnostic["if"] == "always()"
