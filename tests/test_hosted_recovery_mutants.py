"""Target previously surviving hosted operation contract mutants."""
from copy import deepcopy
from uuid import uuid4

import pytest

from custom_components.daylight_calendar_import import hosted_operations as ops
from tests.test_hosted_operations import account_status, SERVICE


@pytest.mark.parametrize("state", [
    "unavailable", "active", "grace", "cancelled", "drain", "capped", "incomplete", "expired",
])
def test_public_billing_states(state):
    value = account_status()
    value["billing"]["state"] = state
    assert ops.validate_account(value, SERVICE.account_origin) == value


def test_trial_allowance_above_maximum():
    value = account_status()
    value["billing"]["trial_remaining"] = 21
    with pytest.raises(ValueError, match="^invalid allowance$"):
        ops.validate_account(value, SERVICE.account_origin)


def _record():
    return {
        "key": str(uuid4()), "digest": "a" * 64, "accepted": True,
        "request_id": "source", "received_at": "2026-11-17T09:00:00Z",
        "reference_datetime": "2026-11-17T10:00:00Z", "time_zone": "UTC",
    }


def _compact(record):
    return {**{key: record[key] for key in ("key", "digest", "accepted")}, "compacted": True}


def test_compacted_records_do_not_consume_active_capacity(monkeypatch):
    monkeypatch.setattr(ops, "_MAX_ACTIVE_OPERATIONS", 2)
    monkeypatch.setattr(ops, "_MAX_COMPACTED_OPERATIONS", 3)
    records = {"a" * 64: _compact(_record()), "b" * 64: _compact(_record()), "c" * 64: _record()}
    before = deepcopy(records)
    ops.reserve_operation_space(records)
    assert records == before


def test_compaction_respects_existing_records_and_exact_limit(monkeypatch):
    monkeypatch.setattr(ops, "_MAX_ACTIVE_OPERATIONS", 3)
    monkeypatch.setattr(ops, "_MAX_COMPACTED_OPERATIONS", 3)
    existing = _compact(_record())
    first, second, third = _record(), _record(), _record()
    records = {"a" * 64: existing, "b" * 64: first, "c" * 64: second, "d" * 64: third}
    ops.reserve_operation_space(records)
    assert records == {
        "a" * 64: existing, "b" * 64: _compact(first),
        "c" * 64: _compact(second), "d" * 64: third,
    }
    assert ops.validate_records(records) == records


def test_validation_counts_all_compacted_records(monkeypatch):
    monkeypatch.setattr(ops, "_MAX_ACTIVE_OPERATIONS", 1)
    monkeypatch.setattr(ops, "_MAX_COMPACTED_OPERATIONS", 2)
    records = {
        "a" * 64: _compact(_record()), "b" * 64: _compact(_record()),
        "c" * 64: {**_record(), "accepted": False},
    }
    assert ops.validate_records(records) == records
    monkeypatch.setattr(ops, "_MAX_COMPACTED_OPERATIONS", 1)
    del records["c" * 64]
    with pytest.raises(ValueError, match="^invalid operation journal$"):
        ops.validate_records(records)


def test_validation_continues_after_compacted_record():
    records = {
        "a" * 64: _compact(_record()),
        "b" * 64: {**_record(), "key": "invalid"},
    }
    with pytest.raises(ValueError, match="^invalid operation journal$"):
        ops.validate_records(records)
