"""Bounded at-least-once HA submissions from the durable notification journal."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from .ha_notification_sink import async_send_ha_notification

DELIVERY_TIMEOUT_SECONDS = 15
RETRY_SECONDS = 60
MAX_ATTEMPTS = 3
MAX_ACTIVE_TASKS = 4


async def async_deliver_notifications(hass, store, registry) -> None:
    """Resume durable requests; persist attempts before invoking an external sink.

    Admission and policy are rechecked for every request. Provider tasks remain
    registry-owned across reloads, including providers that ignore cancellation.
    A timed-out submission can still complete remotely: no exactly-once claim.
    """
    async with store.notification_delivery_lock:
        for record in store.list_notifications():
            if not store.accepting_services:
                return
            if record.status != "pending":
                continue
            policy = store.notification_preferences()
            if not policy.permits(record.event.type) or policy.target != record.target:
                await store.async_update_notification(
                    record, replace(record, status="cancelled", failure="policy_changed"),
                )
                continue
            if record.attempts >= MAX_ATTEMPTS:
                await store.async_update_notification(
                    record, replace(record, status="failed", failure="delivery_interrupted"),
                )
                continue
            now = datetime.now(UTC)
            if record.next_attempt_at is not None and datetime.fromisoformat(record.next_attempt_at) > now:
                continue
            name = f"Daylight notify {record.event.idempotency_key}"
            if any(task.get_name() == name and not task.done()
                   for tasks in registry.values() for task in tasks):
                continue
            if sum(len(tasks) for tasks in registry.values()) >= MAX_ACTIVE_TASKS:
                return
            attempt = replace(record, attempts=record.attempts + 1,
                              next_attempt_at=(now + timedelta(seconds=RETRY_SECONDS)).isoformat())
            if not await store.async_update_notification(record, attempt):
                continue
            # A settings change/admission closure while persistence awaited
            # must not start a call under the superseded consent snapshot.
            if not store.accepting_services or store.notification_preferences() != policy:
                return
            delivery = asyncio.create_task(
                async_send_ha_notification(hass, attempt.event, policy), name=name,
            )
            store.notification_tasks.add(delivery)
            delivery.add_done_callback(store.notification_tasks.discard)

            def consume(task):
                if not task.cancelled():
                    task.exception()

            delivery.add_done_callback(consume)
            sent = False
            try:
                done, _ = await asyncio.wait({delivery}, timeout=DELIVERY_TIMEOUT_SECONDS)
                if done:
                    sent = delivery.result() is True
            except Exception:
                # Persist only a bounded code, never provider details.
                pass
            finally:
                if not delivery.done():
                    delivery.cancel()
            updated = replace(
                attempt, status="sent" if sent else (
                    "failed" if attempt.attempts == MAX_ATTEMPTS else "pending"),
                failure=None if sent else "delivery_failed",
            )
            await store.async_update_notification(attempt, updated)
