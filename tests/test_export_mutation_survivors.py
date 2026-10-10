"""Regression tests for the bulk mutation survivor exporter."""

from pathlib import Path

from scripts import export_mutation_survivors as exporter


def _metadata(monkeypatch, by_path):
    class FakeMetadata:
        def __init__(self, *, path):
            self.path = path
            self.exit_code_by_key = {}

        def load(self):
            self.exit_code_by_key = by_path.get(str(self.path), {})

    monkeypatch.setattr(exporter, "walk_mutatable_files", lambda: iter(Path(path) for path in by_path))
    monkeypatch.setattr(exporter, "SourceFileMutationData", FakeMetadata)


def test_export_keeps_survivors_and_other_non_killed_statuses(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _metadata(monkeypatch, {
        "custom_components/a.py": {
            "a.x__mutmut_1": 1,
            "a.x__mutmut_2": 0,
            "a.x__mutmut_3": 5,
            "a.x__mutmut_4": None,
        },
        "custom_components/b.py": {"b.y__mutmut_1": 0, "b.y__mutmut_2": 36},
    })
    calls = []

    def diff(mutant, *, path):
        calls.append((mutant, str(path)))
        return f"--- {path}\n+++ {path}\n@@ -1 +1 @@\n-before\n+{mutant}"

    monkeypatch.setattr(exporter, "get_diff_for_mutant", diff)
    assert exporter.main() == 0
    assert exporter.RESULTS_PATH.read_text() == (
        "    a.x__mutmut_2: survived\n"
        "    a.x__mutmut_3: no tests\n"
        "    a.x__mutmut_4: not checked\n"
        "    b.y__mutmut_1: survived\n"
        "    b.y__mutmut_2: timeout\n"
    )
    assert exporter.NAMES_PATH.read_text() == "a.x__mutmut_2\nb.y__mutmut_1\n"
    assert calls == [
        ("a.x__mutmut_2", "custom_components/a.py"),
        ("b.y__mutmut_1", "custom_components/b.py"),
    ]
    assert exporter.REPORT_PATH.read_text() == (
        "===== a.x__mutmut_2 =====\n"
        "# a.x__mutmut_2: survived\n"
        "--- custom_components/a.py\n"
        "+++ custom_components/a.py\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+a.x__mutmut_2\n\n"
        "===== b.y__mutmut_1 =====\n"
        "# b.y__mutmut_1: survived\n"
        "--- custom_components/b.py\n"
        "+++ custom_components/b.py\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+b.y__mutmut_1\n\n"
    )


def test_no_mutation_data_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _metadata(monkeypatch, {"custom_components/a.py": {}})
    assert exporter.main() == 1
    assert "no mutation results found" in capsys.readouterr().err


def test_diff_failure_retains_other_survivors(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    _metadata(monkeypatch, {"custom_components/a.py": {"a.x__mutmut_1": 0, "a.x__mutmut_2": 0}})

    def diff(mutant, *, path):
        if mutant.endswith("_1"):
            raise ValueError("missing index")
        return "valid diff"

    monkeypatch.setattr(exporter, "get_diff_for_mutant", diff)
    assert exporter.main() == 1
    assert "failed to export 1 survivor diffs" in capsys.readouterr().err
    assert exporter.NAMES_PATH.read_text() == "a.x__mutmut_1\na.x__mutmut_2\n"
    assert "# a.x__mutmut_2: survived\nvalid diff" in exporter.REPORT_PATH.read_text()
