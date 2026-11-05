"""Opt-in synthetic M3 HA service-path acceptance recorder.

No network request or model call is made by plan. Live submissions require
explicit confirmation, selected cases, an isolated loopback HA, and a token.
Evidence is private and does not claim the browser UI or model internals.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "extraction"
DOMAIN = "daylight_calendar_import"
CASES = {
    "text_basic": ("explicit-practice.json", "81cf6f0389b984b1a130dd3d82fd3e9e87077daa"),
    "text_zero": ("no-events-newsletter.json", "900b6cc7a4d1a28fd4464e48549e543b0f682f13"),
    "text_all_day": ("dates-all-day-and-multiday.json", "6277bde732b9cbd834c8fbbda6aa16ae035305cb"),
    "text_meeting": ("meeting-join-details.json", "df0b6f4d4a445be3b032669db3ea738100574a6a"),
    "text_dst": ("dates-dst-fall-back.json", "2c0d1322cae0757035e7ee878b3b71532a050516"),
    "text_mixed": ("email-school-newsletter.json", "cf7176f028a968e906d4f307a96fc341a0562aa7"),
    "text_long": ("docs/fixtures/hosted-long-description.txt", "fcfd10141e6644108f6c00203297334914f06505"),
    "image": ("media-screenshot.json", "3998be4cc9b5af8fe46a03518134b8044d104d97"),
    "pdf_text": ("media-multipage-text.json", "ab421fcce9663ed751d0933219d665b841864560"),
    "pdf_scan": ("media-multipage-scan.json", "73119d8a40bc7f3bcffe1fc1de0c31665791240a"),
}
MEDIA = {"image", "pdf_text", "pdf_scan"}
ASSET = {
    "image": "assets/media-screenshot.png",
    "pdf_text": "assets/media-multipage-text.pdf",
    "pdf_scan": "assets/media-multipage-scan.pdf",
}
MAX_RESPONSE = 2 * 1024 * 1024


def git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


def fixture(case: str) -> tuple[str, list[dict], bytes | None]:
    name, expected = CASES[case]
    path = ROOT / name if case == "text_long" else FIXTURES / name
    raw = path.read_bytes()
    if case == "text_long":
        source, events = raw.decode("utf-8"), [{
            "title": "Community preparedness workshop",
            "start": "2026-11-18T18:00:00-08:00",
            "end": "2026-11-18T20:00:00-08:00",
            "all_day": False,
            "location": "Cedar Community Hall",
        }]
    else:
        data = json.loads(raw)
        events = data["expected"]["events"]
        source = data["source_text"]
    media = None
    if case in MEDIA:
        media = (FIXTURES / ASSET[case]).read_bytes()
        if git_blob_sha(media) != expected:
            raise ValueError("Fixture media integrity mismatch")
    elif git_blob_sha(raw) != expected:
        raise ValueError("Fixture text integrity mismatch")
    if not isinstance(source, str) or not isinstance(events, list):
        raise ValueError("Invalid synthetic fixture")
    return source, events, media


def validate_origin(origin: str) -> str:
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in origin):
        raise ValueError("Invalid HA origin")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("localhost", "127.0.0.1", "::1")
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.port
        or parsed.path not in ("", "/")
        or parsed.query or parsed.fragment
    ):
        raise ValueError("Only an explicit local HTTP Home Assistant origin is allowed")
    return origin.rstrip("/")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HAClient:
    def __init__(self, origin: str, token: str):
        self.origin = validate_origin(origin)
        if not token.strip():
            raise ValueError("Missing protected HA token")
        self.token = token
        self.opener = build_opener(NoRedirects())

    def request(self, path: str, payload: bytes | None = None, content_type: str = "application/json") -> dict:
        if not path.startswith("/api/") or "://" in path:
            raise ValueError("Invalid HA API path")
        method = "POST" if payload is not None else "GET"
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = content_type
        try:
            with self.opener.open(
                Request(self.origin + path, data=payload, headers=headers, method=method),
                timeout=90,
            ) as response:
                if response.status != 200:
                    raise ValueError("Unexpected HA response status")
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise ValueError("Oversized HA response")
        except HTTPError as error:
            error.close()
            raise ValueError("HA returned HTTP " + str(error.code)) from None
        except (URLError, TimeoutError, OSError):
            raise ValueError("HA request failed or outcome uncertain") from None
        try:
            result = json.loads(body)
        except (UnicodeError, ValueError):
            raise ValueError("Malformed HA JSON response") from None
        if not isinstance(result, dict):
            raise ValueError("Unexpected HA response shape")
        return result

    def service(self, name: str, data: dict | None = None, domain: str = DOMAIN) -> dict:
        body = json.dumps(data or {}, ensure_ascii=False).encode("utf-8")
        result = self.request(f"/api/services/{domain}/{name}?return_response", body)
        output = result.get("service_response")
        if not isinstance(output, dict):
            raise ValueError("Missing HA service response")
        return output

    def upload(self, filename: str, raw: bytes) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", filename) or len(raw) > 10 * 1024 * 1024:
            raise ValueError("Unsafe synthetic upload")
        boundary = "daylight" + uuid4().hex
        part = (
            ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; filename=\""
             + filename + "\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
            + raw + ("\r\n--" + boundary + "--\r\n").encode()
        )
        result = self.request("/api/file_upload", part, "multipart/form-data; boundary=" + boundary)
        file_id = result.get("file_id")
        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", file_id):
            raise ValueError("Invalid upload receipt")
        return file_id


def normalized_datetime(raw: str) -> str:
    if len(raw) == 10:
        return raw
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC).isoformat()


def inspect_drafts(expected: list[dict], observed: list[dict], long: bool = False) -> list[str]:
    errors = []
    if len(observed) != len(expected):
        errors.append("event_count")
    for index, (want, item) in enumerate(zip(expected, observed)):
        actual = item if isinstance(item, dict) else None
        if not isinstance(actual, dict):
            errors.append(f"event_{index}_shape")
            continue
        for field in ("start", "end"):
            try:
                if normalized_datetime(actual[field]) != normalized_datetime(want[field]):
                    errors.append(f"event_{index}_{field}")
            except (KeyError, ValueError, TypeError):
                errors.append(f"event_{index}_{field}")
        if actual.get("all_day") is not want["all_day"]:
            errors.append(f"event_{index}_all_day")
        for field in ("title", "location"):
            value = want.get(field)
            if value and value.casefold() not in str(actual.get(field) or "").casefold():
                errors.append(f"event_{index}_{field}")
        description = want.get("description")
        if description and description.casefold() not in str(actual.get("description") or "").casefold():
            errors.append(f"event_{index}_description")
        if long:
            desc = str(actual.get("description") or "")
            for step in range(1, 25):
                if f"Step {step:02d}" not in desc:
                    errors.append(f"long_description_step_{step:02d}")
            if "Return the completed worksheet to the information desk before leaving." not in desc:
                errors.append("long_description_tail")
    return errors


def _private_write(path: Path, record: dict) -> None:
    temp = path.with_name(path.name + ".pending")
    raw = json.dumps(record, sort_keys=True, indent=2).encode("utf-8")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _new_directory(destination: Path) -> None:
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)


def execute_cases(client: HAClient, cases: list[str], destination: Path) -> dict:
    _new_directory(destination)
    run_id = uuid4().hex
    record = {
        "schema_version": 1,
        "evidence_type": "live_ha_service_path",
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "cases": [],
        "limitations": [
            "Service path does not prove browser New import UI",
            "Provider input bytes and internal model calls are not attested",
            "Restart persistence requires a separate verify command",
            "Calendar no-write snapshot and BYO regression require separate evidence",
        ],
        "completed": False,
    }
    _private_write(destination / "evidence.json", record)
    for case in cases:
        text, expected, media = fixture(case)
        row = {"case": case, "started_at": datetime.now(UTC).isoformat(),
               "state": "in_progress", "issues": []}
        record["cases"].append(row)
        _private_write(destination / "evidence.json", record)
        try:
            data = {"source_id": "m3-evidence-" + run_id + "-" + case}
            if media is None:
                data["text"] = text
                service = "submit_text"
            else:
                data["file_id"] = client.upload(Path(ASSET[case]).name, media)
                service = "submit_image" if case == "image" else "submit_pdf"
            output = client.service(service, data)
            status = output.get("status")
            row["disposition"] = status
            row["activity_observed"] = bool(output.get("activity_id"))
            pending = output.get("pending")
            if case == "text_zero":
                row["issues"] = [] if status == "no_events" and pending is None else ["zero_event_disposition"]
            elif status == "pending_review" and isinstance(pending, dict):
                identifier = pending.get("id")
                if not isinstance(identifier, str):
                    raise ValueError("No durable pending ID")
                row["pending_id"] = identifier
                detail = client.service("get_pending", {"pending_id": identifier}).get("pending")
                if not isinstance(detail, dict):
                    raise ValueError("No pending item returned")
                row["issues"] = inspect_drafts(expected, detail.get("events", []), case == "text_long")
            else:
                row["issues"] = ["missing_pending_or_duplicate"]
            row["state"] = "pass" if not row["issues"] else "quality_mismatch"
        except (ValueError, KeyError, TypeError) as error:
            row["state"] = "inconclusive"
            row["issues"] = ["service_or_local_failure"]
            record["halt_reason"] = type(error).__name__
            # Never resubmit after a timeout or unknown remote outcome.
            break
        finally:
            row["ended_at"] = datetime.now(UTC).isoformat()
            _private_write(destination / "evidence.json", record)
    record["completed"] = len(record["cases"]) == len(cases) and all(
        row["state"] in ("pass", "quality_mismatch") for row in record["cases"]
    )
    record["ended_at"] = datetime.now(UTC).isoformat()
    _private_write(destination / "evidence.json", record)
    return record


def verify_persistence(client: HAClient, evidence: Path, destination: Path) -> dict:
    document = json.loads(evidence.read_bytes())
    if document.get("evidence_type") != "live_ha_service_path" or not isinstance(document.get("cases"), list):
        raise ValueError("Unsupported prior evidence")
    rows = []
    for case in document["cases"]:
        if not isinstance(case, dict) or case.get("case") not in CASES:
            raise ValueError("Invalid prior evidence entry")
        identifier = case.get("pending_id")
        if not identifier:
            continue
        outcome = client.service("get_pending", {"pending_id": identifier})
        item = outcome.get("pending")
        rows.append({"case": case["case"], "persisted": isinstance(item, dict)
                     and item.get("id") == identifier})
    result = {"schema_version": 1, "evidence_type": "post_restart_readback",
              "checked_at": datetime.now(UTC).isoformat(),
              "prior_run_id": document.get("run_id"), "cases": rows,
              "restart_was_observed": False,
              "limitations": ["Operator must independently attest HA restart and exact revision"]}
    _new_directory(destination)
    _private_write(destination / "verification.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "verify"))
    parser.add_argument("--case", action="append", choices=tuple(CASES))
    parser.add_argument("--ha-origin")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            for case in args.case or CASES:
                fixture(case)
            print("Synthetic fixture hashes verified. No network or provider requests made.")
            return 0
        if not args.confirm or args.output is None or not args.ha_origin:
            raise ValueError("Live operation requires --confirm, --ha-origin and --output")
        if args.command == "run" and (not args.case or len(set(args.case)) != len(args.case)):
            raise ValueError("Select each live case exactly once")
        if args.command == "verify" and (args.evidence is None or args.case):
            raise ValueError("Verification requires prior evidence and no new cases")
        token = os.environ.get("DAYLIGHT_M3_HA_TOKEN", "")
        client = HAClient(args.ha_origin, token)
        if args.command == "run":
            for case in args.case:
                fixture(case)
            result = execute_cases(client, args.case, args.output)
            print("Recorded " + str(len(result["cases"])) + " synthetic case outcome(s) privately.")
            return 0 if result["completed"] else 1
        result = verify_persistence(client, args.evidence, args.output)
        print("Recorded " + str(len(result["cases"])) + " pending readback(s) privately.")
        return 0 if all(r["persisted"] for r in result["cases"]) else 1
    except (ValueError, OSError, json.JSONDecodeError):
        print("M3 evidence operation failed; inspect protected record and verify remote state before retrying.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
