"""CI-only contracts for exact mutant-level partitioning of the init module."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import mutation_shards as ms
from scripts import mutation_split as split


def key(function: str, number: int) -> str:
    return f"custom_components.daylight_calendar_import.x_{function}__mutmut_{number}"


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = Path(split.MODULE)
    root.parent.mkdir(parents=True)
    root.write_text("async def async_setup_entry(): pass\n")
    manifest = {
        "target_sha": "a" * 40, "manifest_sha256": "b" * 64, "shard_count": 8,
        "shards": [{"index": i, "modules": [split.MODULE] if i == 1 else ["other"]} for i in range(8)],
    }
    monkeypatch.setattr(split.shards, "git", lambda *args: "a" * 40)
    monkeypatch.setattr(split.shards, "load_manifest", lambda *args: manifest)
    monkeypatch.setattr(split.shards, "verify_manifest", lambda *args: None)
    names = sorted([key("async_setup_entry", i) for i in range(1, 17)]
                   + [key("async_unload_entry", 1), key("_helper", 1)])
    metadata = Path("mutants") / (split.MODULE + ".meta")
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"exit_code_by_key": {name: None for name in names}}))
    return names, metadata


def test_partition_covers_every_mutant_once_and_balances_large_setup_function(fixture):
    names, _ = fixture
    parts = split.partitions(names)
    assert [len(x) for x in parts] == [4, 4, 4, 4, 2]
    assert sorted(name for part in parts for name in part) == names
    assert all(name.startswith(split.SETUP_PREFIX) for part in parts[:4] for name in part)
    assert all(not name.startswith(split.SETUP_PREFIX) for name in parts[4])


@pytest.mark.parametrize("population", [[], [key("async_setup_entry", 1)] * 2, ["evil"]])
def test_invalid_populations_are_rejected(population):
    with pytest.raises(ValueError):
        split.partitions(population)


def test_exact_source_catalog_detects_tampering(fixture):
    names, _ = fixture
    catalog = split.catalog()
    assert catalog["population"] == names
    assert split.verified_catalog() == catalog
    for field, value in [
        ("target_sha", "c" * 40),
        ("source_sha256", "c" * 64),
        ("population_sha256", "c" * 64),
    ]:
        damaged = {**catalog, field: value}
        damaged["catalog_sha256"] = ms.sha256(ms.canonical({
            k: v for k, v in damaged.items() if k != "catalog_sha256"
        }))
        ms.write_json(split.CATALOG, damaged)
        with pytest.raises(ValueError):
            split.verified_catalog()
    ms.write_json(split.CATALOG, catalog)
    assert split.verified_catalog()["parts"] == 5


def test_combiner_rejects_missing_duplicate_or_changed_population(fixture):
    names, _ = fixture
    obj = split.catalog()
    environment = ms.environment()
    for part in range(split.PARTS):
        selected = obj["assignments"][part]
        statuses = {name: "killed" for name in selected}
        stats = {name: 0 for name in ms.FIELDS}
        stats["killed"] = len(selected)
        stats["total"] = len(selected)
        ms.write_json(split.RESULTS / f"split-1-{part}.json", {
            "schema": 1, "complete": True, "index": 1, "part": part,
            "target_sha": obj["target_sha"], "manifest_sha256": obj["manifest_sha256"],
            "catalog_sha256": obj["catalog_sha256"],
            "population_sha256": obj["population_sha256"], "module": split.MODULE,
            "selection_sha256": ms.sha256(ms.canonical(selected)),
            "mutants": {split.MODULE: statuses}, "stats": stats,
            "seconds": 12, "durations": {}, "environment": environment,
        })
    result = split.combine()
    assert result["stats"]["total"] == len(names)
    assert result["stats"]["killed"] == len(names)
    assert result["split_seconds"] == {str(i): 12 for i in range(5)}
    first = split.RESULTS / "split-1-0.json"
    saved = first.read_text()
    first.unlink()
    with pytest.raises(ValueError, match="Missing"):
        split.combine()
    first.write_text(saved)
    tampered = json.loads(saved)
    tampered["mutants"][split.MODULE] = {names[0]: "killed"}
    ms.write_json(first, tampered)
    with pytest.raises(ValueError):
        split.combine()


def test_combiner_rejects_mixed_environments(fixture):
    split.catalog()
    # A complete set of worker artifacts is mandatory, not just a valid catalog.
    with pytest.raises(ValueError, match="Missing"):
        split.combine()


def test_selected_diagnostic_export_never_includes_unselected_mutants(fixture, monkeypatch):
    from scripts import export_mutation_survivors as exporter

    names, _ = fixture
    class FakeMetadata:
        path = Path(split.MODULE)
        exit_code_by_key = {names[0]: 0, names[1]: 0, names[2]: 1}
        def __init__(self, *, path):
            pass
        def load(self):
            pass

    assert exporter.export_survivors(
        paths=[Path(split.MODULE)],
        metadata_factory=FakeMetadata,
        diff_for_mutant=lambda name, **kwargs: "diff",
        status_by_exit_code={0: "survived", 1: "killed"},
        selected_mutants={names[0]},
    ) == 0
    assert Path("mutation-survivor-names.txt").read_text().strip() == names[0]
