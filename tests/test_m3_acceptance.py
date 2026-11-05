"""Offline coverage for the opt-in M3 service-path evidence recorder."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import m3_acceptance as m3


class FakeClient:
    def __init__(self):
        self.uploaded = []
        self.calls = []
        self.pending = {"id": "pending-1", "events": [{"draft": {
            "title": "Soccer practice",
            "start": "2026-10-15T17:00:00-07:00",
            "end": "2026-10-15T18:00:00-07:00",
            "all_day": False,
            "location": "Community Park",
            "description": "Bring water.",
        }}]}

    def upload(self, name, content):
        self.uploaded.append((name, content))
        return "file-12345678"

    def service(self, name, data=None, domain=m3.DOMAIN):
        self.calls.append((name, data))
        if name == "get_pending":
            return {"pending": self.pending}
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
    assert m3.inspect_drafts(expected, [{"draft": expected[0]}]) == []
    wrong = dict(expected[0], end="2026-11-01T01:30:00-07:00")
    assert m3.inspect_drafts(expected, [{"draft": wrong}]) == ["event_0_end"]
    assert m3.inspect_drafts([{"title": "Day", "start": "2026-10-16",
                               "end": "2026-10-17", "all_day": True}],
                             [{"draft": {"title": "Day", "start": "2026-10-16",
                                          "end": "2026-10-17", "all_day": True}}]) == []


def test_records_live_service_path_and_requires_separate_persistence_proof():
    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        client = FakeClient()
        result = m3.execute_cases(client, ["text_basic"], base / "live")
        assert result["completed"]
        assert result["cases"][0]["state"] == "pass"
        assert (base / "live/evidence.json").stat().st_mode & 0o077 == 0
        readback = m3.verify_persistence(client, base / "live/evidence.json", base / "readback")
        assert readback["cases"] == [{"case": "text_basic", "persisted": True}]
        assert readback["restart_was_observed"] is False
        assert [call[0] for call in client.calls] == ["submit_text", "get_pending", "get_pending"]


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
