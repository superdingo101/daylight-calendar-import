"""Shared strict wire decoding and canonical evidence for contract examples.

Reference validation only; production adapters must enforce the same rules.
"""
from decimal import Decimal
from hashlib import sha256
import json
import math



def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"non-finite JSON constant: {value}")


def _reject_surrogates(value):
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("unpaired Unicode surrogate")
    elif isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("non-finite JSON number")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_surrogates(key)
            _reject_surrogates(item)
    elif isinstance(value, list):
        for item in value:
            _reject_surrogates(item)


def _parse_decimal(raw):
    value = Decimal(raw)
    # Every v1 integer field is bounded by the 10 MiB attachment maximum.
    # Convert only exact integral values in that range, preserving ordinary
    # Draft 2020-12 integer semantics even when a $ref changes validators.
    # Leave other values exact for schema rejection, without constructing
    # enormous Python integers from attacker-controlled exponents.
    if value.is_finite() and -10485760 <= value <= 10485760 and value == value.to_integral_value():
        return int(value)
    return value


def _decode_strict(raw):
    result = json.loads(raw, object_pairs_hook=_strict_pairs, parse_constant=_reject_constant, parse_float=_parse_decimal)
    _reject_surrogates(result)
    return result


def _source_evidence_sha256(source: dict) -> str:
    """Canonical complete-source evidence independent of namespaced dedup ID."""
    def integer_values(value):
        # JSON Schema permits 1024.0 for an integer field; normalize its value.
        if isinstance(value, (float, Decimal)):
            if value != int(value):
                raise ValueError("source numbers must be integers")
            return int(value)
        if isinstance(value, dict):
            return {key: integer_values(item) for key, item in value.items()}
        if isinstance(value, list):
            return [integer_values(item) for item in value]
        return value

    _reject_surrogates(source)
    canonical = json.dumps(
        integer_values(source), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(canonical).hexdigest()

