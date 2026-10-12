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
