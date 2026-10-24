"""Private, immediate credential checkpoints; no lifecycle or network activation.

The future controller owns authorization proofs and journal reconciliation. A
checkpoint records recovery material, never evidence of remote activation.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .hosted_transport import HostedServiceBinding

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{16,128}")
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{43,256}")
_VERIFIER = re.compile(r"[A-Za-z0-9_-]{43}")
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:[Zz]|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
_MAX_BYTES = 16 * 1024
_ERROR = "Hosted credential storage needs repair; preserve the original file."


class HostedCredentialStorageError(RuntimeError):
    """Persistence is unavailable or uncertain; stop hosted lifecycle work."""


class HostedCredentialConflict(ValueError):
    """A concurrent checkpoint replaced the caller's snapshot."""


def _text(value: Any, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError("invalid credential checkpoint")
    return value


def _deadline(value: Any) -> str:
    if type(value) is not str or len(value) > _MAX_BYTES or _TIMESTAMP.fullmatch(value) is None:
        raise ValueError("invalid credential deadline")
    # Normalize schema-valid spelling/precision to bounded durable bytes; no
    # comparison or scheduling depends on the original string representation.
    return datetime.fromisoformat(value.upper()).isoformat()


@dataclass(frozen=True, slots=True)
class BoundCredential:
    """A token and the previously verified account/installation binding."""

    account_id: str
    installation_id: str
    token: str = field(repr=False)
    expires_at: str

    def __post_init__(self) -> None:
        _text(self.account_id, _IDENTIFIER)
        _text(self.installation_id, _IDENTIFIER)
        _text(self.token, _TOKEN)
        object.__setattr__(self, "expires_at", _deadline(self.expires_at))

    @property
    def namespace(self) -> tuple[str, str]:
        """The only journal namespace this bound credential can belong to."""
        return self.account_id, self.installation_id


@dataclass(frozen=True, slots=True)
class CandidateCredential:
    """Persist before confirmation; this token has no established namespace."""

    installation_id: str
    token: str = field(repr=False)
    expires_at: str
    confirmation_expires_at: str
    confirmation_attempts: int = 0

    def __post_init__(self) -> None:
        _text(self.installation_id, _IDENTIFIER)
        _text(self.token, _TOKEN)
        if type(self.confirmation_attempts) is not int or not 0 <= self.confirmation_attempts <= 2:
            raise ValueError("invalid confirmation budget")
        object.__setattr__(self, "expires_at", _deadline(self.expires_at))
        object.__setattr__(self, "confirmation_expires_at", _deadline(self.confirmation_expires_at))
        if datetime.fromisoformat(self.confirmation_expires_at) > datetime.fromisoformat(self.expires_at):
            raise ValueError("invalid credential deadline ordering")


@dataclass(frozen=True, slots=True)
class PairingAttempt:
    """Private recovery metadata; approval display data is transient only."""

    pairing_id: str
    expires_at: str
    poll_interval_seconds: int
    next_poll_at: str
    exchange_attempted: bool = False

    def __post_init__(self) -> None:
        _text(self.pairing_id, _IDENTIFIER)
        object.__setattr__(self, "expires_at", _deadline(self.expires_at))
        object.__setattr__(self, "next_poll_at", _deadline(self.next_poll_at))
        if (type(self.poll_interval_seconds) is not int
                or not 5 <= self.poll_interval_seconds <= 30
                or type(self.exchange_attempted) is not bool):
            raise ValueError("invalid pairing recovery metadata")


@dataclass(frozen=True, slots=True)
class AutomationAttempt:
    """A durable request budget; its scope digest conveys no remote authority."""

    scope: str
    attempts: int
    not_before: str

    def __post_init__(self) -> None:
        _text(self.scope, re.compile(r"[0-9a-f]{64}"))
        if type(self.attempts) is not int or not 0 <= self.attempts <= 32:
            raise ValueError("invalid automatic attempt budget")
        object.__setattr__(self, "not_before", _deadline(self.not_before))


@dataclass(frozen=True, slots=True)
class AutomationState:
    """Independent bounded budgets so changing phases cannot reset retries."""

    status: AutomationAttempt | None = None
    pairing: AutomationAttempt | None = None
    candidate: AutomationAttempt | None = None
    rotation: AutomationAttempt | None = None

    def __post_init__(self) -> None:
        if any(value is not None and type(value) is not AutomationAttempt for value in (
            self.status, self.pairing, self.candidate, self.rotation,
        )):
            raise ValueError("invalid automatic state")

    @classmethod
    def decode(cls, value: Any) -> AutomationState:
        if type(value) is not dict or set(value) != {"status", "pairing", "candidate", "rotation"}:
            raise ValueError("invalid automatic state")
        parsed = {}
        for key, record in value.items():
            if record is not None:
                if type(record) is not dict:
                    raise ValueError("invalid automatic budget")
                record = AutomationAttempt(**record)
            parsed[key] = record
        return cls(**parsed)


@dataclass(frozen=True, slots=True)
class CredentialCheckpoint:
    """Immutable recovery material; repr never exposes tokens or verifiers."""

    service_binding: HostedServiceBinding | None = None
    revision: int = 0
    processing_consent: bool = False
    local_disconnect: str = "none"
    rotation_not_before: str | None = None
    pairing: PairingAttempt | None = None
    automation: AutomationState | None = field(default=None, repr=False)
    pairing_verifier: str | None = field(default=None, repr=False)
    current: BoundCredential | None = field(default=None, repr=False)
    candidate: CandidateCredential | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.service_binding is not None and type(self.service_binding) is not HostedServiceBinding:
            raise ValueError("invalid service binding")
        if type(self.revision) is not int or not 0 <= self.revision < 2**63:
            raise ValueError("invalid checkpoint revision")
        if type(self.processing_consent) is not bool:
            raise ValueError("invalid processing consent")
        if self.local_disconnect not in ("none", "unconfirmed", "confirmed"):
            raise ValueError("invalid local disconnect state")
        if self.automation is not None and (type(self.automation) is not AutomationState or not self.processing_consent):
            raise ValueError("automatic budgets require processing consent")
        if self.local_disconnect == "confirmed" and any((
            self.processing_consent, self.pairing_verifier, self.current, self.candidate,
            self.rotation_not_before,
            self.pairing, self.automation,
        )):
            raise ValueError("confirmed disconnect retains credential material")
        if self.pairing_verifier is not None:
            _text(self.pairing_verifier, _VERIFIER)
        if self.pairing is not None and (
            type(self.pairing) is not PairingAttempt or self.pairing_verifier is None
        ):
            raise ValueError("pairing metadata requires a verifier")
        if self.rotation_not_before is not None:
            object.__setattr__(self, "rotation_not_before", _deadline(self.rotation_not_before))
            if not self.processing_consent or self.current is None:
                raise ValueError("rotation intent requires a current credential")
        if self.current is not None and type(self.current) is not BoundCredential:
            raise ValueError("invalid bound credential")
        if self.candidate is not None and type(self.candidate) is not CandidateCredential:
            raise ValueError("invalid candidate credential")
        if not self.processing_consent and any((self.pairing_verifier, self.current, self.candidate)):
            raise ValueError("credential recovery material requires processing consent")


def _decode(data: Any) -> CredentialCheckpoint:
    fields = set(CredentialCheckpoint.__dataclass_fields__)
    optional = {"rotation_not_before", "local_disconnect", "pairing", "service_binding", "automation"}
    if (type(data) is not dict or set(data) - fields
            or fields - set(data) - optional):
        raise ValueError("invalid credential checkpoint")
    values = dict(data)
    if values.get("automation") is not None:
        values["automation"] = AutomationState.decode(values["automation"])
    for key, kind in (
        ("current", BoundCredential), ("candidate", CandidateCredential),
        ("pairing", PairingAttempt), ("service_binding", HostedServiceBinding),
    ):
        if values.get(key) is not None:
            if type(values[key]) is not dict:
                raise ValueError("invalid credential bundle")
            bundle = dict(values[key])
            if key == "candidate":
                # Legacy recovery does not prove how many confirmations were sent.
                bundle.setdefault("confirmation_attempts", 2)
            values[key] = kind(**bundle)
    return CredentialCheckpoint(**values)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate checkpoint key")
        result[key] = value
    return result


def _read(path: str, key: str) -> dict[str, Any] | None:
    """Read bounded original bytes; never rename corrupt secret-bearing files."""
    try:
        with Path(path).open("rb") as source:
            raw = source.read(_MAX_BYTES + 1)
    except FileNotFoundError:
        if any(Path(path).parent.glob(f"{Path(path).name}.corrupt.*")):
            raise ValueError("missing credential checkpoint") from None
        return None
    if len(raw) > _MAX_BYTES:
        raise ValueError("oversized credential checkpoint")
    value = json.loads(raw, object_pairs_hook=_unique)
    if (type(value) is not dict or set(value) != {"version", "minor_version", "key", "data"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["minor_version"]) is not int or value["minor_version"] != 1
            or value["key"] != key):
        raise ValueError("invalid credential envelope")
    _decode(value["data"])
    return value["data"]


class _ImmediatePrivateStore(Store[dict[str, Any]]):
    """HA's atomic private writer, with errors propagated and no delayed saves."""

    async def _async_load_data(self) -> dict[str, Any] | None:
        return await self.hass.async_add_executor_job(_read, self.path, self.key)

    async def async_save(self, data: dict[str, Any]) -> None:
        # Store.async_save may defer during shutdown and its default write
        # handler logs/swallow WriteError. Neither is a durable checkpoint.
        async with self._write_lock:
            if self._read_only:
                raise HostedCredentialStorageError(_ERROR)
            self._manager.async_invalidate(self.key)
            await self._async_write_data({
                "version": 1, "minor_version": 1, "key": self.key, "data": data,
            })


class HostedCredentialStore:
    """One controller-owned store per entry, loaded once before any network use.

    The controller must supply its last snapshot to compare-and-swap. Candidate
    staging preserves the current credential. Promotion/removal and namespace
    reconciliation require controller proofs; this primitive makes no requests.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        _text(entry_id, re.compile(r"[A-Za-z0-9_-]{1,128}"))
        self._hass = hass
        self._store = _ImmediatePrivateStore(
            hass, 1, f"{DOMAIN}.hosted_credentials.{entry_id}",
            private=True, atomic_writes=True,
        )
        # A replacement instance must not overtake an uncertain executor write
        # still owned by the previous instance in this HA process.
        locks = hass.data.setdefault(f"{DOMAIN}.hosted_credential_locks", {})
        self._lock = locks.setdefault(self._store.key, asyncio.Lock())
        self._uncertain_entries = hass.data.setdefault(f"{DOMAIN}.hosted_credential_uncertain", set())
        self._snapshot: CredentialCheckpoint | None = None
        self._blocked = False
        self._pending_write: asyncio.Task | None = None

    @property
    def snapshot(self) -> CredentialCheckpoint:
        """Internal controller state; never serialize this into UI or diagnostics."""
        if self._blocked or self._store.key in self._uncertain_entries or self._snapshot is None:
            raise HostedCredentialStorageError(_ERROR)
        return self._snapshot

    async def async_load(self) -> CredentialCheckpoint:
        """Load exactly once; create a new store for explicit repair/recovery."""
        async with self._lock:
            if self._snapshot is not None or self._blocked or self._store.key in self._uncertain_entries:
                raise HostedCredentialStorageError(_ERROR)
            try:
                data = await self._store.async_load()
                self._snapshot = CredentialCheckpoint() if data is None else _decode(data)
            except (Exception, asyncio.CancelledError):  # noqa: BLE001 -- sanitize all private storage failures
                self._blocked = True
                raise HostedCredentialStorageError(_ERROR) from None
            return self._snapshot

    async def async_checkpoint(
        self, expected: CredentialCheckpoint, proposed: CredentialCheckpoint,
    ) -> CredentialCheckpoint:
        """Publish only committed state; cancellation blocks until explicit reload.

        Caller cancellation returns promptly without cancelling the owned write.
        Its task retains the per-entry lock until persistence settles, so a new
        instance cannot read or overwrite ahead of the uncertain write.
        """
        # HA classifies executor futures by their parent task. Register the
        # transaction so shutdown does not cancel its Future while the atomic
        # writer thread still owns the file and serialization lock.
        task = self._hass.async_create_task(
            self._async_checkpoint(expected, proposed), "Daylight credential checkpoint",
        )
        self._pending_write = task
        try:
            await asyncio.wait({task})
            return task.result()
        except asyncio.CancelledError:
            self._blocked = True
            # Retrieve eventual exceptions without logging secret-bearing IO
            # details. The task itself still owns the shared serialization lock.
            task.add_done_callback(_consume_result)
            raise

    async def _async_checkpoint(
        self, expected: CredentialCheckpoint, proposed: CredentialCheckpoint,
    ) -> CredentialCheckpoint:
        async with self._lock:
            current = self.snapshot
            if expected is not current:
                raise HostedCredentialConflict("stale credential checkpoint")
            if type(proposed) is not CredentialCheckpoint or proposed.revision != current.revision:
                raise HostedCredentialConflict("invalid checkpoint revision")
            if proposed.candidate is not None and proposed.current != current.current:
                raise HostedCredentialConflict("retain current credential while staging candidate")
            updated = replace(proposed, revision=current.revision + 1)
            try:
                disk = await self._store.async_load()
                persisted = CredentialCheckpoint() if disk is None else _decode(disk)
                if persisted != current:
                    raise HostedCredentialConflict("stale persisted credential checkpoint")
                await self._store.async_save(asdict(updated))
            except HostedCredentialConflict:
                raise
            except asyncio.CancelledError:
                # Cancelling an executor Future does not stop its thread. HA's
                # final shutdown stage can cancel even tracked critical tasks.
                # No instance may overtake that write in this process: only a
                # process restart can safely re-read the eventual atomic file.
                self._uncertain_entries.add(self._store.key)
                self._blocked = True
                raise HostedCredentialStorageError(_ERROR) from None
            except Exception:  # noqa: BLE001 -- sanitize all private storage failures
                self._blocked = True
                raise HostedCredentialStorageError(_ERROR) from None
            if not self._blocked:
                self._snapshot = updated
            return updated


def _consume_result(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()
