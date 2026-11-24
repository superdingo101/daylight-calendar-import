"""Public durable operation bindings; no credentials or source content persisted."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from uuid import UUID

import voluptuous as vol


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
                                   "accepted", "time_zone"} or type(record["accepted"]) is not bool
                or str(UUID(record["key"])) != record["key"]
                or type(record["digest"]) is not str or len(record["digest"]) != 64):
            raise ValueError("invalid operation journal")
        for key in ("request_id", "received_at", "reference_datetime", "time_zone"):
            if type(record[key]) is not str or not 1 <= len(record[key]) <= 128:
                raise ValueError("invalid operation journal")
    return deepcopy(value)


def validate_envelope(value, key, expected_digest):
    from datetime import UTC, datetime
    from .hosted_transport import _timestamp
    if (type(value) is not dict or set(value) != {
            "schema_version", "operation_id", "payload_sha256", "status", "result",
            "replay_expires_at"} or type(value["schema_version"]) is not int or value["schema_version"] != 2
            or value["operation_id"] != key or value["payload_sha256"] != expected_digest
            or value["status"] not in ("pending", "uncertain", "succeeded", "failed", "expired")
            or (value["status"] == "succeeded" and type(value["result"]) is not dict)
            or (value["status"] != "succeeded" and value["result"] is not None)):
        raise ValueError("invalid operation envelope")
    expiry = value["replay_expires_at"]
    if expiry is not None:
        try:
            _timestamp(expiry)
        except (TypeError, vol.Invalid):
            raise ValueError("invalid operation expiry") from None
    if value["status"] == "succeeded" and (
            expiry is None or datetime.fromisoformat(expiry.upper()) <= datetime.now(UTC)):
        raise ValueError("expired operation result")
    return value


def validate_account(value, account_origin):
    """Allow only bounded public facts and a local trusted account destination."""
    from .hosted_transport import _timestamp
    if (type(value) is not dict or value.get("schema_version") != 2
            or value.get("parse_operations_v2") is not True
            or set(value) != {"schema_version", "parse_operations_v2", "capabilities", "billing",
                              "drain_cutoff_at", "account_url"}):
        raise ValueError("invalid account status")
    capabilities = value["capabilities"]
    if (type(capabilities) is not dict or set(capabilities) != {
            "managed_ai", "new_email_intake", "accepted_source_drain"}
            or any(type(v) is not bool for v in capabilities.values())):
        raise ValueError("invalid capabilities")
    billing = value["billing"]
    if (type(billing) is not dict or set(billing) != {
            "state", "trial_ends_at", "trial_remaining", "paid_remaining", "period_ends_at",
            "grace_ends_at", "cancel_at_period_end"}
            or billing["state"] not in ("unavailable", "trial", "active", "grace", "cancelled",
                                        "drain", "capped", "incomplete", "expired")
            or type(billing["cancel_at_period_end"]) is not bool):
        raise ValueError("invalid billing status")
    for field, maximum in (("trial_remaining", 20), ("paid_remaining", 100)):
        if type(billing[field]) is not int or not 0 <= billing[field] <= maximum:
            raise ValueError("invalid allowance")
    for stamp in (value["drain_cutoff_at"], *(billing[key] for key in (
            "trial_ends_at", "period_ends_at", "grace_ends_at"))):
        if stamp is not None:
            try:
                _timestamp(stamp)
            except (TypeError, vol.Invalid):
                raise ValueError("invalid account deadline") from None
    if value["account_url"] != account_origin + "/account":
        raise ValueError("untrusted account action")
    return deepcopy(value)
