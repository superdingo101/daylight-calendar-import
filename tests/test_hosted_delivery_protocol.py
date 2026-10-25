"""Reference wire semantics, not production Cloud or HA storage conformance.

Runtime crash, atomic journal and review-completion tests are activation gates
owned by their implementation PRs. No fabricated persistence snapshots here.
"""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from tests.test_hosted_delivery_schemas import (
    FIXTURE_DIR, _read, _source_evidence_sha256, _validate,
    _validate_delivery_page_semantics, _validate_source_retention_window,
    schema_registry,
)


def _time(value):
    return datetime.fromisoformat(value)


def _lease_window(item, granted_at):
    expiry = _time(item['source_expires_at'])
    lease = _time(item['lease_expires_at'])
    if not granted_at < lease <= min(granted_at + timedelta(seconds=120), expiry):
        raise ValueError('invalid lease window')


def _ack_result(*, owner, now, expiry, token, current, lease_end, confirmed=None):
    """Small normative decision table; does not simulate a database."""
    if not owner or now >= expiry:
        return 404
    if confirmed == token:
        return 200
    return 200 if token == current and now < lease_end else 409


@pytest.mark.parametrize('seconds,valid', [(0, False), (1, True), (120, True), (121, False)])
def test_lease_window(seconds, valid):
    item = _read(FIXTURE_DIR / 'valid/delivery-page-text.json')['deliveries'][0]
    grant = _time('2026-10-08T16:00:00Z')
    item['lease_expires_at'] = (grant + timedelta(seconds=seconds)).isoformat()
    if valid:
        _lease_window(item, grant)
    else:
        with pytest.raises(ValueError):
            _lease_window(item, grant)
    item['source_expires_at'] = grant.isoformat()
    with pytest.raises(ValueError):
        _lease_window(item, grant)


def test_lost_claim_response_expires_then_redelivers(schema_registry):
    schemas, registry = schema_registry
    trace = _read(FIXTURE_DIR / 'valid/delivery-lost-claim-trace.json')
    previous = None
    for step in trace['claims']:
        _validate('delivery-claim-request.schema.json', step['request'], schemas, registry)
        _validate('delivery-page.schema.json', step['response'], schemas, registry)
        _validate_delivery_page_semantics(step['response'], request=step['request'])
        for item in step['response']['deliveries']:
            _lease_window(item, _time(step['at']))
            _validate_source_retention_window(
                enqueued_at=_time(trace['enqueued_at']),
                source_expires_at=_time(item['source_expires_at']),
            )
            if previous:
                assert _time(step['at']) >= _time(previous['lease_expires_at'])
                assert item['lease_token'] != previous['lease_token']
                for field in ('delivery_id', 'source_id', 'source_expires_at', 'source'):
                    assert item[field] == previous[field]
                assert _source_evidence_sha256(item['source']) == _source_evidence_sha256(previous['source'])
            previous = item
    assert trace['claims'][0]['response_lost'] is True
    assert trace['claims'][1]['response']['deliveries'] == []
    assert len(trace['claims'][2]['response']['deliveries']) == 1


def test_normative_ack_tombstone_trace(schema_registry):
    schemas, registry = schema_registry
    trace = _read(FIXTURE_DIR / 'valid/delivery-ack-tombstone-trace.json')
    confirmed = trace['steps'][0]
    expiry = _time(trace['source_expires_at'])
    for step in trace['steps']:
        if step['op'] != 'retry_ack':
            continue
        expected = _ack_result(owner=True, now=_time(step['at']), expiry=expiry,
                               token=step['token'], current=None, lease_end=_time(confirmed['acknowledged_at']),
                               confirmed=confirmed['token'])
        assert step['status'] == expected
        if expected == 200:
            assert step['acknowledged_at'] == confirmed['acknowledged_at']
            _validate('delivery-ack-response.schema.json', {
                'schema_version': 1, 'delivery_id': trace['delivery_id'],
                'status': 'acknowledged', 'acknowledged_at': step['acknowledged_at'],
            }, schemas, registry)
        else:
            assert step['code'] == 'not_found'


@pytest.mark.parametrize('owner,offset,token,confirmed,expected', [
    (True, 0, 'new', None, 200),
    (True, 0, 'old', None, 409),
    (True, 120, 'new', None, 409),
    (True, 120, 'new', 'new', 200),
    (True, 604800, 'new', 'new', 404),
    (False, 0, 'new', 'new', 404),
])
def test_ack_ownership_rotation_and_expiry(owner, offset, token, confirmed, expected):
    start = _time('2026-10-08T16:00:00Z')
    assert _ack_result(owner=owner, now=start+timedelta(seconds=offset),
                       expiry=start+timedelta(days=7), token=token, current='new',
                       lease_end=start+timedelta(seconds=120), confirmed=confirmed) == expected


def test_stale_ack_fixture():
    trace = _read(FIXTURE_DIR / 'valid/delivery-stale-ack-trace.json')
    first, _, second, stale, accepted, retry = trace['steps']
    assert first['token'] != second['token']
    assert first['source'] == second['source']
    assert stale['token'] == first['token'] and stale['status'] == 409
    assert stale['code'] == 'lease_not_current'
    assert accepted['token'] == retry['token'] == second['token']
    assert accepted['acknowledged_at'] == retry['acknowledged_at']


def test_source_evidence_detects_tampering():
    item = _read(FIXTURE_DIR / 'valid/delivery-page-attachment.json')['deliveries'][0]
    original = _source_evidence_sha256(item['source'])
    for field, value in [('title', 'changed'), ('metadata', {}), ('attachments', [])]:
        changed = deepcopy(item['source'])
        changed[field] = value
        assert _source_evidence_sha256(changed) != original
    # Wire IDs are independent of upstream dedup keys; this contract cannot
    # establish server ID allocation. Real PostgreSQL tests are required.


def _validate_unchanged_duplicate(original, returned, *, now):
    """Reference invariant for unchanged upstream ingestion, not queue code."""
    if now < _time(original['source_expires_at']):
        for field in ('delivery_id', 'source_id', 'source_expires_at', 'source'):
            if returned[field] != original[field]:
                raise ValueError('pre-expiry duplicate must reuse immutable delivery')
    elif (returned['delivery_id'] == original['delivery_id']
          or returned['source_id'] == original['source_id']):
        raise ValueError('post-expiry ingestion requires fresh identities')


def test_unchanged_duplicate_cannot_bypass_identity_or_retention():
    original = _read(FIXTURE_DIR / 'valid/delivery-page-text.json')['deliveries'][0]
    before = _time('2026-10-09T16:00:00Z')
    _validate_unchanged_duplicate(original, deepcopy(original), now=before)
    for field, value in [('delivery_id', 'delivery_fresh_000000000001'),
                         ('source_id', 'source_fresh_00000000000001'),
                         ('source_expires_at', '2026-10-16T16:00:00Z')]:
        returned = deepcopy(original)
        returned[field] = value
        with pytest.raises(ValueError, match='pre-expiry'):
            _validate_unchanged_duplicate(original, returned, now=before)
    at_expiry = _time(original['source_expires_at'])
    with pytest.raises(ValueError, match='fresh identities'):
        _validate_unchanged_duplicate(original, original, now=at_expiry)
    new = dict(deepcopy(original), delivery_id='delivery_fresh_000000000001',
               source_id='source_fresh_00000000000001', source_expires_at='2026-10-22T16:00:00Z')
    _validate_unchanged_duplicate(original, new, now=at_expiry)
