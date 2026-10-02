"""Transport-neutral sender allowlisting for self-hosted email ingestion."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from email.parser import BytesParser

_ERR_ALLOWLIST = "sender_allowlist entries must be valid email addresses"


def normalize_sender_allowlist(values: Iterable[str]) -> tuple[str, ...]:
    """Return unique, case-normalized exact mailbox addresses."""
    if isinstance(values, (str, bytes)):
        raise ValueError(_ERR_ALLOWLIST)

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(_ERR_ALLOWLIST)
        try:
            address = Address(addr_spec=value.strip())
        except Exception as exc:
            raise ValueError(_ERR_ALLOWLIST) from exc
        if not address.username or not address.domain:
            raise ValueError(_ERR_ALLOWLIST)
        canonical = address.addr_spec.casefold()
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class ExactSenderAllowlist:
    """Require exactly one From mailbox matching an exact configured address."""

    senders: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "senders",
            normalize_sender_allowlist(self.senders),
        )
        if not self.senders:
            raise ValueError("sender allowlist must contain at least one address")

    def allows(self, raw_message: bytes) -> bool:
        """Return whether one unambiguous From mailbox is allowlisted."""
        try:
            message = BytesParser(policy=policy.default).parsebytes(raw_message)
            from_headers = list(message.get_all("from", []))
            if len(from_headers) != 1:
                return False

            header = from_headers[0]
            if getattr(header, "defects", ()):
                return False

            addresses = tuple(getattr(header, "addresses", ()))
            if len(addresses) != 1:
                return False

            sender = addresses[0].addr_spec.strip().casefold()
        except Exception:
            return False
        return bool(sender) and sender in self.senders
