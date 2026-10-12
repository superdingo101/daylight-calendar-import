"""Fail-closed contracts for clean sharded mutation results."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from scripts import mutation_shards as ms

SOURCE = ms.SOURCE


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def make(tmp_path, path, content):
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content)
    return file


@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.invalid")
    make(tmp_path, "pyproject.toml", '[tool.mutmut]\nsource_paths = ["custom_components/daylight_calendar_import/"]\n')
    make(tmp_path, "requirements_test.txt", "mutmut==3.8.0\n")
    make(tmp_path, "mutation-baseline.json", '{"minimum_score":95.22}\n')
    for n in range(16):
        make(tmp_path, SOURCE + f"module_{n}.py", "def check(x):\n    return x > 0\n" * (n + 1))
    make(tmp_path, SOURCE + "frontend/skipped.py", "def ignored(): return 2\n")
    make(tmp_path, "tests/test_ignored.py", "def test_ignored(): assert True\n")
    git("add", ".")
    git("commit", "-qm", "initial")
    sha = git("rev-parse", "HEAD")
    return tmp_path, sha


def stats(killed=96, survived=4, skipped=0, **others):
    raw = dict.fromkeys(ms.FIELDS, 0)
    raw.update(killed=killed, survived=survived, skipped=skipped, **others)
    raw["total"] = sum(raw.values())
    return raw


def mutant_map(modules, data):
    result = {path: {} for path in modules}
    first = modules[0]
    for status in ms.FIELDS:
        for n in range(data[status]):
            result[first][f"{first}.function_{status}_{n}__mutmut_1"] = status
    return result


def write_results(manifest, sample, *, data=None):
    root, _ = sample
    directory = root / "mutation-shards/results"
    directory.mkdir(parents=True, exist_ok=True)
    for item in manifest["shards"]:
        result = dict(
            schema=ms.SCHEMA,
            manifest_sha256=manifest["manifest_sha256"],
            target_sha=manifest["target_sha"],
            index=item["index"], modules=item["modules"],
            complete=True, seconds=12, stats=data or stats(), environment=ms.environment(),
            mutants=mutant_map(item["modules"], data or stats()),
        )
        ms.write_json(directory / f"shard-{item['index']}.json", result)
    return directory


def test_deterministic_manifest_covers_source_exactly_once(sample):
    root, sha = sample
    m = ms.create_manifest(sha, 8)
    assert m == ms.create_manifest(sha, 8)
    assert m["shard_count"] == 8
    assert all(x["modules"] for x in m["shards"])
    modules = [path for row in m["shards"] for path in row["modules"]]
    assert sorted(modules) == ms.source_paths()
    assert len(modules) == len(set(modules)) == 16
    assert not any("frontend/" in p for p in modules)
    ms.verify_manifest(m)


def test_mismatched_exact_sha_is_rejected(sample):
    with pytest.raises(ValueError, match="exact"):
        ms.create_manifest("a" * 40)


@pytest.mark.parametrize("count", [0, 17, 100])
def test_invalid_or_excessive_shard_count_rejected(sample, count):
    _, sha = sample
    with pytest.raises(ValueError):
        ms.create_manifest(sha, count)


def test_modified_source_or_config_invalidates_manifest(sample):
    root, sha = sample
    m = ms.create_manifest(sha)
    (root / SOURCE / "module_1.py").write_text("def changed(): return 1\n")
    with pytest.raises(ValueError, match="Manifest"):
        ms.verify_manifest(m)


def test_config_mutation_and_restore_with_exact_modules(sample):
    _, sha = sample
    m = ms.create_manifest(sha)
    ms.write_json(ms.MANIFEST, m)
    entry = ms.prepare(ms.MANIFEST, 2)
    modified = Path("pyproject.toml").read_text()
    assert "only_mutate" in modified
    for module in entry["modules"]:
        assert module in modified
    from scripts.pr_mutation import restore
    restore()
    ms.verify_manifest(m)


def test_weighted_global_score_and_legacy_checker(sample):
    _, sha = sample
    m = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(m, sample, data=stats(killed=96, survived=4))
    report = ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))
    assert report["score"] == 96
    assert report["complete"] is True
    assert report["stats"]["total"] == 800
    from scripts import check_mutation_score
    assert check_mutation_score.main() == 0


def test_weighted_score_not_mean_of_per_shard_percentages(sample):
    _, sha = sample
    m = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(m, sample)
    poor = directory / "shard-0.json"
    value = json.loads(poor.read_text())
    value["stats"] = stats(killed=1, survived=99)
    value["mutants"] = mutant_map(value["modules"], value["stats"])
    ms.write_json(poor, value)
    result = ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))
    assert result["score"] == 84.125
    from scripts import check_mutation_score
    assert check_mutation_score.main() == 1


def test_incomplete_artifact_is_rejected(sample):
    _, sha = sample
    m = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(m, sample)
    (directory / "shard-7.json").unlink()
    with pytest.raises(ValueError, match="Missing"):
        ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))


def test_unexpected_artifact_is_rejected(sample):
    _, sha = sample
    m = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(m, sample)
    ms.write_json(directory / "shard-100.json", {})
    with pytest.raises(ValueError, match="unexpected"):
        ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))


@pytest.mark.parametrize("corruption", ["sha", "checksum", "index", "modules", "complete", "stats", "duration"])
def test_invalid_identity_or_partial_stats_fail_closed(sample, corruption):
    _, sha = sample
    m = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(m, sample)
    target = directory / "shard-2.json"
    value = json.loads(target.read_text())
    if corruption == "sha":
        value["target_sha"] = "a" * 40
    elif corruption == "checksum":
        value["manifest_sha256"] = "f" * 64
    elif corruption == "index":
        value["index"] = 5
    elif corruption == "modules":
        value["modules"] = m["shards"][1]["modules"]
    elif corruption == "complete":
        value["complete"] = False
    elif corruption == "stats":
        value["stats"]["survived"] -= 1
    else:
        value["seconds"] = -1
    ms.write_json(target, value)
    with pytest.raises(ValueError):
        ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))


@pytest.mark.parametrize("corruption", [
    {"check_was_interrupted_by_user": True},
    {"killed": -1, "survived": 101},
])
def test_reject_interrupted_or_invalid_counts(sample, corruption):
    raw = stats()
    raw.update(corruption)
    with pytest.raises(ValueError):
        ms.stats_checked(raw, require_nonempty=True)


def test_no_tests_count_fails_original_quality_gate(sample):
    from scripts import check_mutation_score
    raw = stats(killed=95, survived=4, no_tests=1)
    ms.write_json(ms.STATS, raw)
    assert ms.stats_checked(raw, require_nonempty=True)["no_tests"] == 1
    assert check_mutation_score.main() == 1


def test_zero_mutants_not_valid_clean_shard(sample):
    raw = stats(killed=0, survived=0)
    with pytest.raises(ValueError, match="Invalid total"):
        ms.stats_checked(raw, require_nonempty=True)


def test_empty_or_duplicate_paths_cannot_be_claimed_complete(sample):
    _, sha = sample
    m = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(m, sample)
    altered = json.loads((directory / "shard-0.json").read_text())
    altered["modules"].append(m["shards"][1]["modules"][0])
    ms.write_json(directory / "shard-0.json", altered)
    with pytest.raises(ValueError):
        ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))


def test_different_resolved_shard_environment_rejected(sample):
    _, sha = sample
    m = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(m, sample)
    path = directory / "shard-2.json"
    obj = json.loads(path.read_text())
    obj["environment"]["details"]["python"] = "3.14.999"
    obj["environment"]["sha256"] = ms.sha256(ms.canonical(obj["environment"]["details"]))
    ms.write_json(path, obj)
    with pytest.raises(ValueError, match="environments differ"):
        ms.reduce(ms.MANIFEST, directory, Path("out.json"))


def test_corrupt_environment_evidence_rejected(sample):
    with pytest.raises(ValueError, match="environment"):
        ms.validate_environment({"details": {}, "sha256": "a" * 64})


def test_skipped_mutants_cannot_count_as_complete_clean_validation(sample):
    with pytest.raises(ValueError, match="skipped"):
        ms.stats_checked(stats(killed=99, survived=0, skipped=1), require_nonempty=True)


@pytest.fixture
def comparison_evidence(sample, monkeypatch):
    _, sha = sample
    manifest = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(manifest, sample)
    aggregate = Path("mutation-shards/aggregate.json")
    evidence = ms.reduce(ms.MANIFEST, directory, aggregate)
    codes = {"killed": 1, "survived": 0, "timeout": 36}
    for module, verdicts in evidence["mutants"].items():
        ms.write_json(Path("mutants") / (module + ".meta"),
                      {"exit_code_by_key": {key: codes[status] for key, status in verdicts.items()}})
    collector = ms.collect_mutants
    monkeypatch.setattr(ms, "collect_mutants", lambda modules: collector(
        modules, statuses={1: "killed", 0: "survived", 36: "timeout"}))
    unsharded = Path("mutation-shards/unsharded.json")
    ms.record_unsharded(ms.MANIFEST, 12, unsharded)
    return aggregate, unsharded


def test_comparison_accepts_complete_same_environment_evidence(comparison_evidence):
    aggregate, unsharded = comparison_evidence
    result = ms.compare(ms.MANIFEST, aggregate, unsharded, Path("equivalence.json"))
    assert result["equivalent"] is True
    assert result["sharded_stats"]["total"] == 800


@pytest.mark.parametrize("corruption", ["environment", "sha", "checksum", "complete", "modules", "population", "score", "interrupted"])
def test_comparison_rejects_incompatible_engine_evidence(comparison_evidence, corruption):
    aggregate, unsharded = comparison_evidence
    obj = json.loads(unsharded.read_text())
    if corruption == "environment":
        obj["environment"]["details"]["python"] = "3.14.999"
        obj["environment"]["sha256"] = ms.sha256(ms.canonical(obj["environment"]["details"]))
    elif corruption == "sha":
        obj["target_sha"] = "a" * 40
    elif corruption == "checksum":
        obj["manifest_sha256"] = "a" * 64
    elif corruption == "complete":
        obj["complete"] = False
    elif corruption == "modules":
        obj["modules"] = obj["modules"][:-1]
    elif corruption == "population":
        obj["stats"]["killed"] += 1
        obj["stats"]["total"] += 1
    elif corruption == "score":
        obj["stats"]["killed"] -= 1
        obj["stats"]["survived"] += 1
    else:
        obj["stats"]["check_was_interrupted_by_user"] = 1
    ms.write_json(unsharded, obj)
    with pytest.raises(ValueError):
        ms.compare(ms.MANIFEST, aggregate, unsharded, Path("equivalence.json"))
    assert not Path("equivalence.json").exists()


def test_comparison_retains_timeout_score_semantics(comparison_evidence):
    aggregate, unsharded = comparison_evidence
    obj = json.loads(unsharded.read_text())
    obj["stats"]["killed"] -= 1
    obj["stats"]["timeout"] += 1
    verdicts = next(v for v in obj["mutants"].values() if "killed" in v.values())
    key = next(k for k, status in verdicts.items() if status == "killed")
    verdicts[key] = "timeout"
    ms.write_json(unsharded, obj)
    assert ms.compare(ms.MANIFEST, aggregate, unsharded, Path("equivalence.json"))["equivalent"]


def test_workflow_target_and_comparison_contracts():
    import yaml
    root = Path(__file__).resolve().parents[1]
    file = root / ".github/workflows/mutation-sharded.yml"
    if not file.exists() and root.name == "mutants":
        pytest.skip("Workflow not copied to mutation sandbox")
    workflow = yaml.safe_load(file.read_text())
    jobs = workflow["jobs"]
    target = next(s for s in jobs["plan"]["steps"] if s.get("id") == "target")
    assert target["env"]["TARGET_SHA"] == "${{ inputs.target_sha || github.sha }}"
    assert 'echo "sha=$TARGET_SHA"' in target["run"]
    assert "github.run_id" in workflow["concurrency"]["group"]
    assert "workflow_call" in workflow.get("on", workflow.get(True))
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert set(jobs["equivalence"]["needs"]) == {"plan", "aggregate", "unsharded"}
    assert "always()" in jobs["equivalence"]["if"]
    for engine in ("shards", "unsharded"):
        commands = [s.get("run") for s in jobs[engine]["steps"]]
        assert "python -m pip install --upgrade pip" in commands
        assert "pip install -r requirements_test.txt" in commands
    assert any("scripts.mutation_shards compare" in s.get("run", "") for s in jobs["equivalence"]["steps"])
    assert not (root / ".github/workflows/mutation-comparison.yml").exists()


@pytest.mark.parametrize("corruption", ["renamed_identity", "exchanged_outcomes"])
def test_equal_aggregate_counts_cannot_hide_different_mutants(comparison_evidence, corruption):
    aggregate, unsharded = comparison_evidence
    obj = json.loads(unsharded.read_text())
    verdicts = next(v for v in obj["mutants"].values() if "killed" in v.values())
    killed = next(k for k, status in verdicts.items() if status == "killed")
    if corruption == "renamed_identity":
        verdicts[killed + "different_mutant"] = verdicts.pop(killed)
    else:
        survived = next(k for k, status in verdicts.items() if status == "survived")
        verdicts[killed], verdicts[survived] = "survived", "killed"
    ms.write_json(unsharded, obj)
    assert obj["stats"] == json.loads(aggregate.read_text())["stats"]
    with pytest.raises(ValueError, match="identities or per-mutant"):
        ms.compare(ms.MANIFEST, aggregate, unsharded, Path("equivalence.json"))


def test_incomplete_mutant_metadata_fails_closed(sample):
    module = ms.source_paths()[0]
    path = Path("mutants") / (module + ".meta")
    with pytest.raises(OSError):
        ms.collect_mutants([module], statuses={1: "killed"})
    ms.write_json(path, {"exit_code_by_key": {"unfinished_mutant": None}})
    with pytest.raises(ValueError, match="Incomplete"):
        ms.collect_mutants([module], statuses={1: "killed"})


def test_reducer_rejects_identity_outcomes_inconsistent_with_stats(sample):
    _, sha = sample
    manifest = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(manifest, sample)
    path = directory / "shard-0.json"
    obj = json.loads(path.read_text())
    verdicts = next(v for v in obj["mutants"].values() if v)
    verdicts.pop(next(iter(verdicts)))
    ms.write_json(path, obj)
    with pytest.raises(ValueError, match="reported statistics"):
        ms.reduce(ms.MANIFEST, directory, Path("aggregate.json"))


@pytest.mark.parametrize("missing", [False, True])
def test_join_diagnostics_requires_every_shard_file(sample, missing):
    _, sha = sample
    manifest = ms.plan(sha, 8, ms.MANIFEST)
    names = ("mutation-survivors.txt", "mutation-survivor-names.txt", "mutation-survivor-report.txt")
    for index in range(8):
        for name in names:
            path = Path("diagnostics") / f"mutation-shard-diagnostics-{index}" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"shard {index}\n")
    if missing:
        path.unlink()
        with pytest.raises(OSError):
            ms.join_diagnostics(ms.MANIFEST, Path("diagnostics"), Path("reports"))
        assert not Path("reports").exists()
    else:
        ms.join_diagnostics(ms.MANIFEST, Path("diagnostics"), Path("reports"))
        assert (Path("reports") / names[0]).read_text().splitlines() == [f"shard {i}" for i in range(8)]


@pytest.mark.parametrize("minimum_failure", [False, True])
def test_sharded_release_proof_rechecks_full_evidence(sample, monkeypatch, minimum_failure):
    from scripts import release_mutation_proof
    root, _ = sample
    file = Path(SOURCE + "manifest.json")
    file.write_text('{"version":"0.6.1"}')
    git("add", ".")
    git("commit", "-qm", "release candidate")
    sha = git("rev-parse", "HEAD")
    manifest = ms.plan(sha, 8, ms.MANIFEST)
    directory = write_results(manifest, sample,
                              data=stats(killed=94, survived=6) if minimum_failure else stats())
    ms.reduce(ms.MANIFEST, directory, Path("mutation-shards/aggregate.json"))
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    if minimum_failure:
        with pytest.raises(ValueError, match="release score"):
            release_mutation_proof.create("v0.6.1", sharded=True)
        assert not release_mutation_proof.PROOF_FILE.exists()
    else:
        release_mutation_proof.create("v0.6.1", sharded=True)
        proof = json.loads(release_mutation_proof.PROOF_FILE.read_text())
        assert proof["mode"] == "clean" and proof["source_commit"] == sha
        assert proof["source_tree"] == git("rev-parse", "HEAD^{tree}")
        assert proof["workflow_run_id"] == 12345
        assert proof["execution"]["mode"] == "complete-sharded"
        assert proof["execution"]["manifest_sha256"] == manifest["manifest_sha256"]
