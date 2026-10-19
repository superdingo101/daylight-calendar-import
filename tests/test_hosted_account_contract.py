"""Executable public M2 examples and reference semantics, not a Cloud service.

The small reference state machine makes lifecycle invariants reviewable. Cloud
must independently prove them against its real transactions and authentication.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

from tests.hosted_contract import _decode_strict

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "schemas").is_dir():
    ROOT = ROOT.parent
SCHEMA_DIR = ROOT / "schemas" / "hosted" / "v1"
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "hosted" / "v1"


def _read(path: Path) -> dict:
    return _decode_strict(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def account_registry():
    schemas = {path.name: _read(path) for path in SCHEMA_DIR.glob("*.schema.json")}
    for schema in schemas.values():
        Draft202012Validator.check_schema(schema)
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )
    return schemas, registry


def _validate(name, document, account_registry):
    schemas, registry = account_registry
    Draft202012Validator(
        schemas[name], registry=registry, format_checker=FormatChecker()
    ).validate(document)


@dataclass
class _Candidate:
    installation: str
    previous: str | None
    expires_at: int
    token_expires_at: int
    old_expiry: int | None = None
    consumed: bool = False


@dataclass
class _ReferenceLifecycle:
    """Single-household reference; time is an authoritative integer clock."""

    installation: str | None = None
    credential: str | None = None
    expiry: int = 0
    candidates: dict[str, _Candidate] = field(default_factory=dict)
    overlap: dict[str, int] = field(default_factory=dict)
    forwarding_address: str = "household@example.test"

    def stage_pairing(self, secret, installation, *, now, previous=None):
        self.candidates[secret] = _Candidate(installation, previous, now + 600, now + 90 * 86400)

    def stage_rotation(self, secret, *, now):
        if any(
            candidate.old_expiry is not None
            and candidate.installation == self.installation
            and not candidate.consumed
            and now < candidate.expires_at
            for candidate in self.candidates.values()
        ):
            raise ValueError("rotation pending")
        self.candidates[secret] = _Candidate(
            self.installation, self.credential, now + 600, now + 90 * 86400,
            old_expiry=self.expiry
        )

    def confirm(self, secret, *, now):
        if secret == self.credential and now < self.expiry:
            return self.installation
        candidate = self.candidates[secret]
        if candidate.consumed or now >= candidate.expires_at:
            raise ValueError("candidate expired or consumed")
        current = self.credential if candidate.old_expiry is not None else self.installation
        if candidate.previous != current:
            raise ValueError("replacement conflict")
        if candidate.old_expiry is None and candidate.installation == self.installation:
            raise ValueError("replacement requires a new installation identity")
        if candidate.old_expiry is not None and now >= candidate.old_expiry:
            raise ValueError("issuer expired")
        if candidate.old_expiry is not None:
            self.overlap[self.credential] = min(now + 600, candidate.old_expiry)
        else:
            self.overlap.clear()
        self.installation = candidate.installation
        self.credential = secret
        self.expiry = candidate.token_expires_at
        candidate.consumed = True
        return self.installation

    def authenticated(self, secret, *, now):
        return (secret == self.credential and now < self.expiry) or (
            secret in self.overlap and now < self.overlap[secret]
        )


def _connected():
    model = _ReferenceLifecycle()
    model.stage_pairing("original", "installation-a", now=0)
    model.confirm("original", now=1)
    return model


def test_reference_staging_does_not_authorize_or_replace_existing_installation():
    model = _connected()
    model.stage_pairing("replacement", "installation-b", now=10, previous="installation-a")
    assert model.authenticated("original", now=11)
    assert not model.authenticated("replacement", now=11)
    assert model.installation == "installation-a"


def test_reference_confirm_proves_possession_before_replacing_and_is_recoverable():
    model = _connected()
    model.stage_pairing("replacement", "installation-b", now=10, previous="installation-a")
    assert model.confirm("replacement", now=11) == "installation-b"
    assert not model.authenticated("original", now=11)
    assert model.authenticated("replacement", now=11)
    # A lost confirmation response does not activate a second installation.
    assert model.confirm("replacement", now=12) == "installation-b"


def test_reference_candidate_expiry_is_exclusive_and_old_connection_survives():
    model = _connected()
    model.stage_pairing("lost-response", "installation-b", now=10, previous="installation-a")
    with pytest.raises(ValueError, match="expired"):
        model.confirm("lost-response", now=610)
    assert model.installation == "installation-a"
    assert model.authenticated("original", now=610)


def test_reference_concurrent_replacements_have_only_one_winner():
    model = _connected()
    for secret in ("first", "second"):
        model.stage_pairing(secret, secret, now=10, previous="installation-a")
    model.confirm("first", now=11)
    with pytest.raises(ValueError, match="conflict"):
        model.confirm("second", now=11)
    assert model.installation == "first"
    assert not model.authenticated("second", now=11)


def test_reference_concurrent_initial_pairings_have_only_one_winner():
    model = _ReferenceLifecycle()
    model.stage_pairing("first", "installation-a", now=0)
    model.stage_pairing("second", "installation-b", now=0)
    model.confirm("first", now=1)
    with pytest.raises(ValueError, match="conflict"):
        model.confirm("second", now=1)
    assert model.installation == "installation-a"


def test_reference_rotation_staging_survives_lost_response_without_revocation():
    model = _connected()
    model.stage_rotation("lost-candidate", now=10)
    assert model.authenticated("original", now=11)
    assert not model.authenticated("lost-candidate", now=11)
    with pytest.raises(ValueError, match="expired"):
        model.confirm("lost-candidate", now=610)
    assert model.authenticated("original", now=610)


def test_reference_rotation_retains_installation_and_bounds_overlap():
    model = _connected()
    model.stage_rotation("rotated", now=10)
    model.confirm("rotated", now=11)
    assert model.installation == "installation-a"
    assert model.authenticated("original", now=610)
    assert not model.authenticated("original", now=611)
    assert model.authenticated("rotated", now=611)


def test_reference_rotation_never_extends_old_token_expiry():
    model = _connected()
    model.expiry = 100
    model.stage_rotation("rotated", now=90)
    model.confirm("rotated", now=91)
    assert model.authenticated("original", now=99)
    assert not model.authenticated("original", now=100)


def test_reference_rotation_preserves_identity_and_replacement_preserves_address():
    model = _connected()
    address = model.forwarding_address
    model.stage_rotation("rotated", now=10)
    model.confirm("rotated", now=11)
    assert model.installation == "installation-a"
    model.stage_pairing("replacement", "installation-b", now=20, previous="installation-a")
    model.confirm("replacement", now=21)
    assert model.forwarding_address == address
    assert model.installation == "installation-b"


def test_reference_rotation_rechecks_issuer_expiry_at_confirmation():
    model = _connected()
    model.expiry = 100
    model.stage_rotation("rotated", now=90)
    with pytest.raises(ValueError, match="issuer expired"):
        model.confirm("rotated", now=100)
    assert model.credential == "original"
    assert not model.authenticated("rotated", now=100)


def test_reference_confirmation_never_extends_issued_token_deadline():
    model = _ReferenceLifecycle()
    model.stage_pairing("candidate", "installation-a", now=0)
    model.confirm("candidate", now=500)
    assert model.expiry == 90 * 86400
    model.confirm("candidate", now=1000)
    assert model.expiry == 90 * 86400


VALID_ACCOUNT_FIXTURES = (
    ("account-enroll-request", "account-enroll-request"),
    ("account-disconnect-request", "account-disconnect-request"),
    ("account-status", "account-status"),
    ("installation-status-response", "account-installation-status"),
    ("installation-disconnect-response", "account-installation-disconnect"),
    ("pairing-start-request", "pairing-start-request"),
    ("pairing-start-response", "pairing-start-response"),
    ("pairing-status-response", "pairing-status-response"),
    ("pairing-approval-request", "pairing-approval-request"),
    ("pairing-approval-request", "pairing-replacement-request"),
    ("pairing-approval-response", "pairing-approval-response"),
    ("credential-action-request", "credential-action-request"),
    ("credential-issued-response", "credential-issued-response"),
    ("credential-confirmed-response", "credential-confirmed-response"),
)
INVALID_ACCOUNT_FIXTURES = (
    ("pairing-start-request", "pairing-plain-challenge"),
    ("pairing-start-request", "pairing-short-challenge"),
    ("pairing-start-response", "pairing-url-secret"),
    ("pairing-start-response", "pairing-url-userinfo"),
    ("pairing-approval-request", "pairing-replacement-missing-identity"),
    ("pairing-approval-request", "pairing-connect-selects-identity"),
    ("account-enroll-request", "account-selected-owner"),
    ("account-status", "account-leaked-token"),
    ("account-disconnect-request", "account-disconnect-client-selector"),
    ("installation-status-response", "account-installation-suspended"),
    ("credential-issued-response", "credential-short-token"),
    ("credential-issued-response", "credential-invalid-timestamp"),
)


@pytest.mark.parametrize(("schema", "fixture"), VALID_ACCOUNT_FIXTURES)
def test_account_valid_public_examples(schema, fixture, account_registry):
    _validate(f"{schema}.schema.json", _read(FIXTURE_DIR / "valid" / f"{fixture}.json"), account_registry)


@pytest.mark.parametrize(("schema", "fixture"), INVALID_ACCOUNT_FIXTURES)
def test_account_invalid_public_examples(schema, fixture, account_registry):
    with pytest.raises(ValidationError):
        _validate(f"{schema}.schema.json", _read(FIXTURE_DIR / "invalid" / f"{fixture}.json"), account_registry)


@pytest.mark.parametrize(("schema", "fixture"), VALID_ACCOUNT_FIXTURES)
@pytest.mark.parametrize("mutation", ("version", "unknown", "missing"))
def test_account_envelopes_reject_versions_unknown_fields_and_missing_fields(
    schema, fixture, mutation, account_registry
):
    data = _read(FIXTURE_DIR / "valid" / f"{fixture}.json")
    if mutation == "version":
        data["schema_version"] = 2
    elif mutation == "unknown":
        data["owner_id"] = "client-selected-owner"
    else:
        del data["schema_version"]
    with pytest.raises(ValidationError):
        _validate(f"{schema}.schema.json", data, account_registry)


@pytest.mark.parametrize("field", ("managed_ai", "email_intake", "source_delivery"))
def test_account_capabilities_are_required_booleans(field, account_registry):
    for schema, fixture in (
        ("account-status", "account-status"),
        ("installation-status-response", "account-installation-status"),
    ):
        data = _read(FIXTURE_DIR / "valid" / f"{fixture}.json")
        data["capabilities"][field] = 1
        with pytest.raises(ValidationError):
            _validate(f"{schema}.schema.json", data, account_registry)
        del data["capabilities"][field]
        with pytest.raises(ValidationError):
            _validate(f"{schema}.schema.json", data, account_registry)


@pytest.mark.parametrize("interval", (4, 31, 5.5, True))
def test_pairing_poll_interval_is_bounded_integer(interval, account_registry):
    data = _read(FIXTURE_DIR / "valid" / "pairing-start-response.json")
    data["poll_interval_seconds"] = interval
    with pytest.raises(ValidationError):
        _validate("pairing-start-response.schema.json", data, account_registry)


@pytest.mark.parametrize("suffix", ("\n", "\r", "=", " "))
def test_pairing_challenge_rejects_suffixes_and_padding(suffix, account_registry):
    data = _read(FIXTURE_DIR / "valid" / "pairing-start-request.json")
    data["code_challenge"] += suffix
    with pytest.raises(ValidationError):
        _validate("pairing-start-request.schema.json", data, account_registry)


@pytest.mark.parametrize("url", (
    "http://accounts.example.test/connect",
    "https://accounts.example.test/connect#secret",
    "https://user@accounts.example.test/connect",
    "https://accounts.example.test/connect?code=1234",
    "https://accounts.example.test/connect\n",
))
def test_pairing_and_status_urls_reject_unsafe_wire_forms(url, account_registry):
    for schema, fixture, field in (
        ("pairing-start-response", "pairing-start-response", "verification_uri"),
        ("installation-status-response", "account-installation-status", "account_url"),
    ):
        data = _read(FIXTURE_DIR / "valid" / f"{fixture}.json")
        data[field] = url
        with pytest.raises(ValidationError):
            _validate(f"{schema}.schema.json", data, account_registry)


def test_lifecycle_valid_fixtures_cover_every_new_schema(account_registry):
    schemas, _ = account_registry
    lifecycle = {
        name for name in schemas
        if name.startswith(("account-", "pairing-", "credential-", "installation-"))
    }
    assert lifecycle == {f"{schema}.schema.json" for schema, _ in VALID_ACCOUNT_FIXTURES}


def test_reference_pairing_replacement_requires_a_fresh_installation_identity():
    model = _connected()
    model.stage_pairing("replacement", "installation-a", now=10, previous="installation-a")
    with pytest.raises(ValueError, match="new installation identity"):
        model.confirm("replacement", now=11)
    assert model.credential == "original"
    assert model.installation == "installation-a"
    assert not model.authenticated("replacement", now=11)


def test_reference_rotation_allows_only_one_unconfirmed_candidate():
    model = _connected()
    model.stage_rotation("first", now=10)
    with pytest.raises(ValueError, match="rotation pending"):
        model.stage_rotation("second", now=11)
    assert "second" not in model.candidates
    assert model.authenticated("original", now=11)


def test_reference_rotation_confirmation_releases_candidate_slot():
    model = _connected()
    model.stage_rotation("first", now=10)
    model.confirm("first", now=11)
    model.stage_rotation("second", now=12)
    assert model.candidates["first"].consumed
    assert not model.candidates["second"].consumed
    assert model.candidates["second"].previous == "first"
    assert model.authenticated("first", now=12)


def test_reference_rotation_expiry_releases_candidate_slot():
    model = _connected()
    model.stage_rotation("lost", now=10)
    with pytest.raises(ValueError, match="rotation pending"):
        model.stage_rotation("too-soon", now=609)
    model.stage_rotation("fresh", now=610)
    with pytest.raises(ValueError, match="expired"):
        model.confirm("lost", now=610)
    assert model.confirm("fresh", now=611) == "installation-a"
