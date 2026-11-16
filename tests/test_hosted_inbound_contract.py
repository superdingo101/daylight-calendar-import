"""Public owner inbound controls reject malformed or ambiguous requests."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

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


@pytest.mark.parametrize("sender", [
    "a@@example.test", "a@.", "a@example.test,b@example.test",
    ".a@example.test", "a..b@example.test", "a@-example.test",
    "a@example..test", "a" * 65 + "@example.test",
])
def test_sender_is_one_valid_ascii_mailbox(sender):
    request = {"schema_version": 1, "expected_revision": None, "allowed_senders": [sender]}
    response = {"schema_version": 1, "address": None,
                "sender_policy": {"revision": None, "allowed_senders": [sender]}}
    assert not validate("account-inbound-senders-request", request)
    assert not validate("account-inbound-settings", response)


def test_forwarding_address_matches_installation_status_limit():
    settings = {"schema_version": 1, "address": {"email": "a" * 48 + "@" +
                "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "test",
                "revision": "b" * 64},
                "sender_policy": {"revision": None, "allowed_senders": []}}
    installation = json.loads((ROOT / "tests" / "fixtures" / "hosted" / "v1" / "valid"
                               / "account-installation-status.json").read_text(encoding="utf-8"))
    installation["forwarding_address"] = settings["address"]["email"]
    schema = json.loads((SCHEMAS / "installation-status-response.schema.json").read_text(
        encoding="utf-8"))
    assert validate("account-inbound-settings", settings)
    assert Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(installation)
    settings["address"]["email"] = "a" * 48 + "@" + "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 13
    assert len(settings["address"]["email"]) > 254
    assert not validate("account-inbound-settings", settings)
