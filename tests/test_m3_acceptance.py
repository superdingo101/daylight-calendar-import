"""Offline coverage for the opt-in M3 service-path evidence recorder."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import ProxyHandler
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import m3_acceptance as m3


class FakeClient:
    def __init__(self):
        self.uploaded = []
        self.calls = []
        self.pending = {"id": "pending-1", "events": [{
            "title": "Soccer practice",
            "start": "2026-10-15T17:00:00-07:00",
            "end": "2026-10-15T18:00:00-07:00",
            "all_day": False,
            "location": "Community Park",
            "description": "Bring water.",
        }]}

    def upload(self, name, content):
        self.uploaded.append((name, content))
        return "file-12345678"

    def service(self, name, data=None, domain=m3.DOMAIN):
        self.calls.append((name, data))
        if name == "get_pending":
            return {"pending": self.pending}
        if name == "list_pending":
            return {"imports": [{"id": self.pending["id"]}]}
        if name == "submit_text":
            return {"status": "pending_review", "activity_id": "activity-1",
                    "pending": {"id": "pending-1"}}
        raise AssertionError(name)


def test_plan_checks_hashes_without_network(capsys):
    assert m3.main(["plan"]) == 0
    assert "No network" in capsys.readouterr().out


def test_rejects_remote_or_unsafe_origins():
    for value in ("https://127.0.0.1:8124", "http://localhost:8124/secret",
                  "http://127.0.0.1:8124?token=yes", "http://user:pass@localhost:8124",
                  "http://example.com:8124", "http://127.0.0.1:8124\n",
                  "http://localhost:8124#x"):
        try:
            m3.validate_origin(value)
        except ValueError:
            continue
        raise AssertionError(value)
    assert m3.validate_origin("http://127.0.0.1:8124") == "http://127.0.0.1:8124"


def test_fixture_catalog_is_integrity_checked():
    for name in m3.CASES:
        source, expected, media = m3.fixture(name)
        assert isinstance(source, str) and isinstance(expected, list)
        assert (media is not None) == (name in m3.MEDIA)


def test_draft_comparator_preserves_dst_and_all_day():
    expected = [{"title": "Shift", "start": "2026-11-01T01:30:00-07:00",
                 "end": "2026-11-01T01:30:00-08:00", "all_day": False}]
    assert m3.inspect_drafts(expected, [expected[0]]) == []
    wrong = dict(expected[0], end="2026-11-01T01:30:00-07:00")
    assert m3.inspect_drafts(expected, [wrong]) == ["event_0_end"]
    assert m3.inspect_drafts([{"title": "Day", "start": "2026-10-16",
                               "end": "2026-10-17", "all_day": True}],
                             [{"title": "Day", "start": "2026-10-16",
                               "end": "2026-10-17", "all_day": True}]) == []


def test_records_live_service_path_and_requires_separate_persistence_proof():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        client = FakeClient()
        result = m3.execute_cases(client, ["text_basic"], base / "live")
        assert result["completed"]
        assert result["cases"][0]["state"] == "pass"
        assert (base / "live/evidence.json").stat().st_mode & 0o077 == 0
        readback = m3.verify_persistence(client, base / "live/evidence.json", base / "readback")
        assert readback["cases"] == [{"case": "text_basic", "persisted": True, "state": "present"}]
        assert readback["restart_was_observed"] is False
        assert [call[0] for call in client.calls] == ["submit_text", "get_pending", "list_pending", "get_pending"]


def test_failed_request_stops_without_retry():
    client = FakeClient()

    def fail(name, data=None, domain=m3.DOMAIN):
        raise ValueError("uncertain outcome")

    client.service = fail
    with TemporaryDirectory() as tmp:
        result = m3.execute_cases(client, ["text_basic", "text_zero"], Path(tmp) / "live")
        assert not result["completed"]
        assert len(result["cases"]) == 1
        assert result["cases"][0]["state"] == "inconclusive"
        assert "uncertain outcome" not in json.dumps(result)


def test_requires_confirmation_before_credential_or_network(monkeypatch):
    monkeypatch.delenv("DAYLIGHT_M3_HA_TOKEN", raising=False)
    assert m3.main(["run", "--case", "text_basic", "--ha-origin", "http://127.0.0.1:8124",
                    "--output", "/tmp/not-created-without-confirm"]) == 2
    with patch.object(m3, "HAClient", side_effect=AssertionError("network")):
        assert m3.main(["plan", "--case", "text_basic"]) == 0


def test_mutmut_copy_uses_original_immutable_asset_directory(tmp_path):
    original = tmp_path / "checkout"
    mutant = original / "mutants"
    mutant.mkdir(parents=True)
    assert m3.fixture_root(mutant) == original
    (mutant / "tests/fixtures/extraction").mkdir(parents=True)
    (mutant / "tests/fixtures/extraction/explicit-practice.json").write_text("exists")
    assert m3.fixture_root(mutant) == mutant


def test_no_proxy_with_configured_environment_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://remote-proxy.invalid:8888")
    monkeypatch.setenv("http_proxy", "http://remote-proxy.invalid:8888")
    client = m3.HAClient("http://127.0.0.1:8124", "T" * 48)
    assert not any(isinstance(handler, ProxyHandler) for handler in client.opener.handlers)


def test_long_description_rejects_labels_without_full_instructions():
    source, expected, _ = m3.fixture("text_long")
    complete = {**expected[0], "description": source}
    assert not m3.inspect_drafts(expected, [complete], long=True)
    just_labels = " ".join(f"Step {i:02d}" for i in range(1, 25))
    just_labels += " Final instruction: Return the completed worksheet to the information desk before leaving."
    errors = m3.inspect_drafts(expected, [{**complete, "description": just_labels}], long=True)
    assert "long_description_step_01" in errors
    assert "long_description_step_24" in errors
    changed = source.replace("check the north entrance sign", "skip the north entrance sign", 1)
    assert "long_description_step_01" in m3.inspect_drafts(
        expected, [{**complete, "description": changed}], long=True
    )


def test_media_cases_force_explicit_reviewable_reimport(monkeypatch):
    class MediaClient(FakeClient):
        def service(self, name, data=None, domain=m3.DOMAIN):
            if name in ("submit_image", "submit_pdf"):
                assert data["reimport_duplicates"] is True
                assert "file_id" in data
                return {"status": "pending_review", "pending": {"id": "pending-1"}}
            if name == "get_pending":
                return {"pending": self.pending}
            raise AssertionError(name)

    client = MediaClient()
    _, expected, _ = m3.fixture("image")
    client.pending = {"id": "pending-1", "events": expected}
    with TemporaryDirectory() as directory:
        result = m3.execute_cases(client, ["image", "pdf_text", "pdf_scan"],
                                  Path(directory) / "record")
    assert result["completed"]
    assert all(row["state"] == "pass" for row in result["cases"])
    assert len(client.uploaded) == 3


def test_missing_readback_writes_failures_and_continues():
    class MissingClient(FakeClient):
        def service(self, name, data=None, domain=m3.DOMAIN):
            if name == "list_pending":
                return {"imports": [{"id": "other"}]}
            raise AssertionError(name)

    evidence = {"evidence_type": "live_ha_service_path", "run_id": "original",
                "cases": [{"case": "text_basic", "pending_id": "missing1"},
                          {"case": "text_dst", "pending_id": "missing2"}]}
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.json"
        source.write_text(json.dumps(evidence))
        result = m3.verify_persistence(MissingClient(), source, root / "readback")
        assert result["cases"] == [
            {"case": "text_basic", "persisted": False, "state": "missing"},
            {"case": "text_dst", "persisted": False, "state": "missing"},
        ]
        assert json.loads((root / "readback/verification.json").read_text()) == result


def test_failed_listing_or_detail_is_inconclusive_not_missing():
    class InconclusiveClient(FakeClient):
        def service(self, name, data=None, domain=m3.DOMAIN):
            if name == "list_pending":
                raise ValueError("401 sensitive details")
            raise AssertionError(name)

    source = {"evidence_type": "live_ha_service_path",
              "cases": [{"case": "text_basic", "pending_id": "pending-1"}]}
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "source.json"
        path.write_text(json.dumps(source))
        result = m3.verify_persistence(InconclusiveClient(), path, root / "out")
        assert result["cases"] == [
            {"case": "text_basic", "persisted": None, "state": "inconclusive"}
        ]
        assert "sensitive" not in (root / "out/verification.json").read_text()


def test_detail_transport_error_is_not_false_negative():
    class DetailErrorClient(FakeClient):
        def service(self, name, data=None, domain=m3.DOMAIN):
            if name == "get_pending":
                raise ValueError("500")
            return super().service(name, data, domain)

    source = {"evidence_type": "live_ha_service_path",
              "cases": [{"case": "text_basic", "pending_id": "pending-1"}]}
    with TemporaryDirectory() as directory:
        root = Path(directory)
        path = root / "source.json"
        path.write_text(json.dumps(source))
        result = m3.verify_persistence(DetailErrorClient(), path, root / "out")
        assert result["cases"][0]["state"] == "inconclusive"
