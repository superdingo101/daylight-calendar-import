"""Public owner inbound controls reject malformed or ambiguous requests."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "schemas").is_dir():
    ROOT = ROOT.parent
SCHEMAS = ROOT / "schemas" / "hosted" / "v1"


def validate(name, value):
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema).is_valid(value)


def test_inbound_settings_response_bounds():
    value = {"schema_version": 1, "address": None,
             "sender_policy": {"revision": None, "allowed_senders": []}}
    assert validate("account-inbound-settings", value)
    value["address"] = {"email": "a" * 48 + "@in.example.test", "revision": "b" * 64}
    value["sender_policy"] = {"revision": "c" * 64, "allowed_senders": ["a@example.test"]}
    assert validate("account-inbound-settings", value)
    value["address"]["email"] += "\n"
    assert not validate("account-inbound-settings", value)


@pytest.mark.parametrize("revision", [None, "a" * 64])
def test_address_rotation_requires_cas_revision(revision):
    value = {"schema_version": 1, "expected_revision": revision}
    assert validate("account-inbound-address-request", value)
    for invalid in ({**value, "address": "chosen@example.test"},
                    {**value, "expected_revision": "a" * 64 + "\n"},
                    {"schema_version": 1}):
        assert not validate("account-inbound-address-request", invalid)


def test_sender_policy_requires_unique_bounded_values_and_revision():
    value = {"schema_version": 1, "expected_revision": None, "allowed_senders": []}
    assert validate("account-inbound-senders-request", value)
    value["allowed_senders"] = ["sender@example.test"]
    assert validate("account-inbound-senders-request", value)
    for invalid in ({**value, "allowed_senders": ["same@example.test"] * 2},
                    {**value, "allowed_senders": ["no-at-sign"]},
                    {**value, "allowed_senders": [f"{i}@example.test" for i in range(51)]},
                    {**value, "account_id": "untrusted"}):
        assert not validate("account-inbound-senders-request", invalid)
