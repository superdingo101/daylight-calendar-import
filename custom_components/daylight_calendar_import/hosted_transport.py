"""Bounded P01 transport; callers own durable consent and credential recovery.

This module never retries. In particular, a failed POST can have committed remotely.
It must not be replayed without the P01 lifecycle reconciliation rules.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import aiohttp
import voluptuous as vol

MAX_BODY_BYTES = 16 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{16,128}")
_VERIFIER = re.compile(r"[A-Za-z0-9_-]{43}")
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{43,256}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}")
_TIMESTAMP = re.compile(
    r"\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)"
)


class HostedTransportError(Exception):
    """Sanitized failure; response text and secret-bearing request data are excluded."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


def new_pairing_verifier() -> str:
    """Generate a verifier which the caller must durably save BEFORE pairing."""
    return secrets.token_urlsafe(32)


def pairing_challenge(verifier: str) -> str:
    """Return the public S256 challenge without disclosing its verifier."""
    _match(_VERIFIER, verifier)
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _match(pattern: re.Pattern[str], value: Any) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise vol.Invalid("invalid value")
    return value


def _timestamp(value: Any) -> str:
    _match(_TIMESTAMP, value)
    try:
        datetime.fromisoformat(value.upper())
    except ValueError:
        raise vol.Invalid("invalid timestamp") from None
    return value


def _origin(value: str) -> str:
    """Require an explicit HTTPS origin, without userinfo, paths or URL tricks."""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(ord(c) <= 32 or c in "\\?#" for c in value)
        )
        port = parsed.port
    except ValueError, TypeError:
        raise ValueError("invalid trusted origin") from None
    if not valid or port == 0:
        raise ValueError("invalid trusted origin")
    return f"https://{parsed.netloc.lower()}"


def _trusted_url(value: Any, origin: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise vol.Invalid("invalid account URL")
    try:
        parsed = urlsplit(value)
        actual = _origin(f"{parsed.scheme}://{parsed.netloc}")
    except ValueError:
        raise vol.Invalid("invalid account URL") from None
    if (
        actual != origin
        or parsed.query
        or parsed.fragment
        or any(ord(c) <= 32 or c in "\\?#" for c in value)
    ):
        raise vol.Invalid("untrusted account URL")
    return value


def _exact(kind: type, value: Any) -> Any:
    if type(value) is not kind:
        raise vol.Invalid("invalid type")
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _strict_strings(value: Any) -> None:
    if isinstance(value, str):
        value.encode("utf-8", "strict")
    elif isinstance(value, dict):
        for key, item in value.items():
            _strict_strings(key)
            _strict_strings(item)
    elif isinstance(value, list):
        for item in value:
            _strict_strings(item)


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON")


class HostedLifecycleTransport:
    """Send fixed installation endpoints through an HA-owned aiohttp session.

    Trusted origins are local service configuration, never values from a server
    response. The session remains owned and closed by Home Assistant.
    """

    def __init__(
        self, session: aiohttp.ClientSession, *, api_origin: str, account_origin: str
    ) -> None:
        self._session = session
        self._api_origin = _origin(api_origin)
        self._account_origin = _origin(account_origin)

    async def start_pairing(
        self, verifier: str, installation_label: str
    ) -> dict[str, Any]:
        """Start ONCE after the caller has saved verifier and processing consent."""
        challenge = pairing_challenge(verifier)
        if (
            not isinstance(installation_label, str)
            or not 1 <= len(installation_label) <= 128
        ):
            raise ValueError("invalid installation label")
        try:
            installation_label.encode("utf-8", "strict")
        except UnicodeError:
            raise ValueError("invalid installation label") from None
        return await self._request(
            "POST",
            "/v1/pairings",
            "pairing-start",
            201,
            body={
                "schema_version": 1,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "installation_label": installation_label,
            },
        )

    async def pairing_status(self, pairing_id: str, verifier: str) -> dict[str, Any]:
        """Poll only at the server's advertised interval (scheduling is caller-owned)."""
        return await self._pairing("GET", pairing_id, verifier)

    async def exchange(self, pairing_id: str, verifier: str) -> dict[str, Any]:
        """Exchange ONCE; an uncertain result requires a fresh pairing."""
        return await self._pairing("POST", pairing_id, verifier)

    async def _pairing(
        self, method: str, pairing_id: str, verifier: str
    ) -> dict[str, Any]:
        _match(_IDENTIFIER, pairing_id)
        _match(_VERIFIER, verifier)
        exchange = method == "POST"
        result = await self._request(
            method,
            f"/v1/pairings/{pairing_id}" + ("/exchange" if exchange else ""),
            "issued" if exchange else "pairing-status",
            200,
            headers={"X-Daylight-Pairing-Verifier": verifier},
            body={"schema_version": 1} if exchange else None,
        )
        if not exchange and result["pairing_id"] != pairing_id:
            raise HostedTransportError("invalid_response")
        return result

    async def installation_status(self, token: str) -> dict[str, Any]:
        """Read a snapshot; callers must compare the bound account/installation IDs."""
        return await self._credential("GET", "/v1/installation", "installation", token)

    async def confirm(self, candidate_token: str) -> dict[str, Any]:
        """Confirm a securely persisted candidate; do not discard previous on timeout."""
        return await self._credential(
            "POST", "/v1/installation/confirm", "confirmed", candidate_token
        )

    async def rotate(self, token: str) -> dict[str, Any]:
        """Stage rotation ONCE; a lost response never authorizes an automatic retry."""
        return await self._credential(
            "POST", "/v1/installation/credentials", "issued", token
        )

    async def disconnect(self, token: str) -> dict[str, Any]:
        """401/403 after an uncertain request does not confirm remote disconnect."""
        return await self._credential(
            "POST", "/v1/installation/disconnect", "disconnected", token
        )

    async def _credential(
        self, method: str, path: str, response: str, token: str
    ) -> dict[str, Any]:
        _match(_TOKEN, token)
        return await self._request(
            method,
            path,
            response,
            200,
            headers={"Authorization": f"Bearer {token}"},
            body={"schema_version": 1} if method == "POST" else None,
        )

    def _validate(self, value: Any, response: str) -> dict[str, Any]:
        """Validate the closed P01 response fields without a new runtime dependency."""
        identifier = lambda v: _match(_IDENTIFIER, v)
        url = lambda v: _trusted_url(v, self._account_origin)
        fields: dict[str, Any] = {
            "schema_version": vol.All(lambda v: _exact(int, v), 1)
        }
        if response == "pairing-start":
            fields.update(
                pairing_id=identifier,
                user_code=lambda v: _match(re.compile(r"[A-Z0-9]{12}"), v),
                verification_uri=url,
                expires_at=_timestamp,
                poll_interval_seconds=vol.All(
                    lambda v: _exact(int, v), vol.Range(min=5, max=30)
                ),
            )
        elif response == "pairing-status":
            fields.update(
                pairing_id=identifier,
                status=vol.In(("pending", "approved", "exchanged")),
                expires_at=_timestamp,
            )
        elif response == "issued":
            fields.update(
                installation_id=identifier,
                installation_token=lambda v: _match(_TOKEN, v),
                expires_at=_timestamp,
                confirmation_expires_at=_timestamp,
            )
        elif response == "confirmed":
            fields.update(
                installation_id=identifier, status="active", expires_at=_timestamp
            )
        elif response == "disconnected":
            fields.update(installation_id=identifier, status="disconnected")
        else:
            fields.update(
                account_id=identifier,
                installation_id=identifier,
                account_status="active",
                capabilities=vol.Schema(
                    {
                        vol.Required(k): lambda v: _exact(bool, v)
                        for k in ("managed_ai", "email_intake", "source_delivery")
                    }
                ),
                credential_expires_at=_timestamp,
                credential_status=vol.In(("current", "overlap")),
                forwarding_address=vol.Any(
                    None,
                    vol.All(
                        str,
                        vol.Length(min=3, max=254),
                        lambda v: _match(re.compile(r"[^\s@]+@[^\s@]+"), v),
                    ),
                ),
                account_url=url,
            )
        return vol.Schema({vol.Required(k): v for k, v in fields.items()})(value)

    async def _request(
        self,
        method: str,
        path: str,
        schema: str,
        expected: int,
        *,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(15):
                async with self._session.request(
                    method,
                    self._api_origin + path,
                    headers={"Accept": "application/json", **(headers or {})},
                    json=body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                    max_line_size=4096,
                    max_field_size=4096,
                ) as response:
                    if 300 <= response.status < 400:
                        raise HostedTransportError(
                            "invalid_response", status=response.status
                        )
                    directives = (
                        response.headers.get("Cache-Control", "").lower().split(",")
                    )
                    if (
                        "no-store" not in (part.strip() for part in directives)
                        or response.content_type != "application/json"
                    ):
                        raise HostedTransportError(
                            "invalid_response", status=response.status
                        )
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(4096):
                        raw.extend(chunk)
                        if len(raw) > MAX_BODY_BYTES:
                            raise HostedTransportError(
                                "invalid_response", status=response.status
                            )
                    value = json.loads(
                        raw.decode("utf-8"),
                        object_pairs_hook=_pairs,
                        parse_constant=_reject_constant,
                    )
                    _strict_strings(value)
                    if response.status != expected:
                        self._raise_api_error(
                            value, response.status, response.headers.get("Retry-After")
                        )
                    return self._validate(value, schema)
        except aiohttp.ClientError, TimeoutError:
            raise HostedTransportError("service_unavailable", retryable=True) from None
        except ValueError, UnicodeError, RecursionError, vol.Invalid:
            raise HostedTransportError("invalid_response") from None

    @staticmethod
    def _raise_api_error(value: Any, status: int, retry_header: str | None) -> None:
        value = vol.Schema(
            {
                vol.Required("schema_version"): vol.All(lambda v: _exact(int, v), 1),
                vol.Optional("request_id"): vol.Any(
                    None, vol.All(str, vol.Length(min=1, max=128))
                ),
                vol.Required("error"): {
                    vol.Required("code"): lambda v: _match(_CODE, v),
                    vol.Required("message"): vol.All(str, vol.Length(min=1, max=2048)),
                    vol.Required("retryable"): lambda v: _exact(bool, v),
                },
            }
        )(value)
        retry_after = None
        if status == 429:
            if (
                retry_header is None
                or re.fullmatch(r"[0-9]{1,3}", retry_header) is None
                or not 1 <= int(retry_header) <= 300
            ):
                raise HostedTransportError("invalid_response", status=status)
            retry_after = int(retry_header)
        error = value["error"]
        # Preserve unknown canonical codes and retryability, never server message text.
        raise HostedTransportError(
            error["code"],
            status=status,
            retryable=error["retryable"],
            retry_after=retry_after,
        )
