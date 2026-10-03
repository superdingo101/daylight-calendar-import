"""Transport-neutral sender allowlisting for self-hosted email ingestion."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from email.parser import BytesParser
from email.utils import getaddresses

_ERR_ALLOWLIST = "sender_allowlist entries must be valid email addresses"


def _raw_header_lines(raw_message: bytes) -> tuple[bytes, ...]:
    """Return header lines with CRLF, LF, and bare CR normalized uniformly."""
    normalized = raw_message.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    header_block = normalized.partition(b"\n\n")[0]
    return tuple(header_block.split(b"\n"))


def _has_malformed_from_field_name(raw_message: bytes) -> bool:
    """Return whether a raw header line uses whitespace before the From colon."""
    for line in _raw_header_lines(raw_message):
        if line[:4].lower() != b"from":
            continue
        remainder = line[4:]
        if (
            remainder.startswith((b" ", b"\t"))
            and remainder.lstrip(b" \t").startswith(b":")
        ):
            return True
    return False


def _normalize_exact_mailbox(value: object) -> str:
    """Return the canonical form used for exact mailbox comparisons."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(_ERR_ALLOWLIST)
    try:
        address = Address(addr_spec=value.strip())
    except Exception as exc:
        raise ValueError(_ERR_ALLOWLIST) from exc
    if not address.username or not address.domain:
        raise ValueError(_ERR_ALLOWLIST)
    return address.addr_spec.casefold()


def normalize_sender_allowlist(values: Iterable[str]) -> tuple[str, ...]:
    """Return unique, case-normalized exact mailbox addresses."""
    if isinstance(values, (str, bytes)):
        raise ValueError(_ERR_ALLOWLIST)

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        canonical = _normalize_exact_mailbox(value)
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class ExactSenderAllowlist:
    """Require exactly one From mailbox matching an exact configured address."""

    senders: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.senders:
            raise ValueError("sender allowlist must contain at least one address")

    def allows(self, raw_message: bytes) -> bool:
        """Return whether one unambiguous From mailbox is allowlisted."""
        try:
            if _has_malformed_from_field_name(raw_message):
                return False

            message = BytesParser(policy=policy.default).parsebytes(raw_message)
            if getattr(message, "defects", ()):
                return False

            from_headers = list(message.get_all("from", []))
            if len(from_headers) != 1:
                return False

            header = from_headers[0]
            if getattr(header, "defects", ()):
                return False

            groups = tuple(getattr(header, "groups", ()))
            if len(groups) != 1:
                return False

            group = groups[0]
            if getattr(group, "display_name", None) is not None:
                return False

            addresses = tuple(getattr(group, "addresses", ()))
            if len(addresses) != 1:
                return False

            # HeaderRegistry is intentionally permissive and may normalize
            # malformed list punctuation away. Run the strict parser against
            # the stored raw From value, then require both parsers to identify
            # the same single mailbox rather than growing a custom RFC parser.
            raw_from_values = tuple(
                value
                for name, value in message.raw_items()
                if name.casefold() == "from"
            )
            strict_addresses = getaddresses(raw_from_values, strict=True)
            if len(strict_addresses) != 1 or not strict_addresses[0][1]:
                return False

            structured_sender = _normalize_exact_mailbox(
                addresses[0].addr_spec
            )
            strict_sender = _normalize_exact_mailbox(strict_addresses[0][1])
            if structured_sender != strict_sender:
                return False
        except Exception:
            return False
        return strict_sender in self.senders
