"""Public durable operation bindings; no credentials or source content persisted."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from uuid import UUID


def digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def payload_digest(request, attachments) -> str:
    return digest({"request": request, "attachments": sorted([
        {"id": ident, "media_type": media, "sha256": sha256(content).hexdigest()}
        for ident, media, content in attachments
    ], key=lambda item: item["id"])})


def source_identity(request, attachments, *, namespace: str, upstream: str | None,
                    reparse: str | None = None) -> str:
    source = deepcopy(request["source"])
    source.pop("id")
    source.pop("received_at")
    return digest({"namespace": namespace, "upstream": upstream, "reparse": reparse,
                   "source": source, "attachments": [
                       {"media_type": media, "sha256": sha256(content).hexdigest()}
                       for _, media, content in attachments]})


def validate_records(value):
    if type(value) is not dict or len(value) > 10000:
        raise ValueError("invalid operation journal")
    for identity, record in value.items():
        if (type(identity) is not str or len(identity) != 64 or type(record) is not dict
                or set(record) != {"key", "digest", "request_id", "received_at", "reference_datetime",
                                   "accepted"} or type(record["accepted"]) is not bool
                or str(UUID(record["key"])) != record["key"]
                or type(record["digest"]) is not str or len(record["digest"]) != 64):
            raise ValueError("invalid operation journal")
        for key in ("request_id", "received_at", "reference_datetime"):
            if type(record[key]) is not str or not 1 <= len(record[key]) <= 128:
                raise ValueError("invalid operation journal")
    return deepcopy(value)


def validate_envelope(value, key, expected_digest):
    if (type(value) is not dict or set(value) != {
            "schema_version", "operation_id", "payload_sha256", "status", "result",
            "replay_expires_at"} or value["schema_version"] != 2
            or value["operation_id"] != key or value["payload_sha256"] != expected_digest
            or value["status"] not in {"pending", "uncertain", "succeeded", "failed", "expired"}
            or (value["status"] == "succeeded") != (type(value["result"]) is dict)):
        raise ValueError("invalid operation envelope")
    return value
