"""Transport-neutral sender allowlisting for self-hosted email ingestion."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from email import policy
from email.headerregistry import Address
from email.parser import BytesHeaderParser
from email.utils import getaddresses

_ERR_ALLOWLIST = "sender_allowlist entries must be valid email addresses"
_HEADER_SEPARATORS = (b"\r\n\r\n", b"\n\n", b"\r\r")


def _raw_header_block(raw_message: bytes) -> bytes | None:
    """Return only the bytes before the first supported header/body boundary."""
    offsets = tuple(
        offset
        for separator in _HEADER_SEPARATORS
        if (offset := raw_message.find(separator)) >= 0
    )
    if not offsets:
        return None
    return raw_message[: min(offsets)]


def _raw_header_lines(header_block: bytes) -> tuple[bytes, ...]:
    """Return header lines with CRLF, LF, and bare CR normalized uniformly."""
    normalized = header_block.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return tuple(normalized.split(b"\n"))


def _has_invalid_header_field_name(header_block: bytes) -> bool:
    """Return whether any non-continuation header line has invalid field syntax."""
    saw_field = False
    for line in _raw_header_lines(header_block):
        if line.startswith((b" ", b"\t")):
            if not saw_field:
                return True
            continue

        field_name, separator, _value = line.partition(b":")
        if not separator or not field_name:
            return True
        if any(
            byte < 33 or byte > 126 or byte == ord(":")
            for byte in field_name
        ):
            return True
        saw_field = True
    return False


def _canonical_header_bytes(header_block: bytes) -> bytes:
    """Return a header-only message with canonical CRLF line endings."""
    return b"\r\n".join(_raw_header_lines(header_block)) + b"\r\n\r\n"


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
            header_block = _raw_header_block(raw_message)
            if (
                header_block is None
                or _has_invalid_header_field_name(header_block)
            ):
                return False

            message = BytesHeaderParser(policy=policy.default).parsebytes(
                _canonical_header_bytes(header_block)
            )
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
