"""Home Assistant runtime wiring for self-hosted Direct IMAP ingestion."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_EMAIL_ENABLED,
    CONF_EMAIL_HOST,
    CONF_EMAIL_MAILBOX,
    CONF_EMAIL_PASSWORD,
    CONF_EMAIL_PORT,
    CONF_EMAIL_USERNAME,
    CONF_EMAIL_VERIFY_SSL,
)
from .direct_imap import DirectImapError, DirectImapSettings, DirectImapSource
from .email_attachments import async_stage_email_attachments
from .email_polling import EmailDocumentProcessor, async_poll_email_source
from .email_source import EmailDisposition
from .sources import SourceDocument
from .storage import PendingImportStore

_LOGGER = logging.getLogger(__name__)

EMAIL_POLL_INTERVAL = timedelta(minutes=5)
DEFAULT_EMAIL_PORT = 993
DEFAULT_EMAIL_MAILBOX = "INBOX"


def direct_imap_settings_from_options(
    entry_id: str,
    options: Mapping[str, object],
) -> DirectImapSettings:
    """Build validated adapter settings from one config entry's options."""
    return DirectImapSettings(
        source_id=f"{entry_id}:direct-imap",
        host=options[CONF_EMAIL_HOST],
        port=options.get(CONF_EMAIL_PORT, DEFAULT_EMAIL_PORT),
        username=options[CONF_EMAIL_USERNAME],
        password=options[CONF_EMAIL_PASSWORD],
        mailbox=options.get(CONF_EMAIL_MAILBOX, DEFAULT_EMAIL_MAILBOX),
        verify_ssl=options.get(CONF_EMAIL_VERIFY_SSL, True),
        disposition=EmailDisposition(mark_seen=True),
    )


def email_review_source_text(document: SourceDocument) -> str:
    """Build private review text without retaining attachment bytes."""
    parts: list[str] = []
    if document.text:
        parts.append(document.text)
    for attachment in document.attachments:
        label = attachment.filename or attachment.media_type
        digest = (
            f" (SHA-256: {attachment.sha256})"
            if attachment.sha256
            else ""
        )
        parts.append(f"Email attachment: {label}{digest}")
    return "\n\n".join(parts)


class EmailPollingRuntime:
    """Own one non-overlapping periodic poll loop for a config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        source: DirectImapSource,
        store: PendingImportStore,
        processor: EmailDocumentProcessor,
        *,
        interval: timedelta = EMAIL_POLL_INTERVAL,
    ) -> None:
        self._hass = hass
        self._source = source
        self._store = store
        self._processor = processor
        self._interval = interval
        self._cancel_interval: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None

    async def async_start(self) -> None:
        """Schedule polling and immediately run one collection cycle."""
        self._cancel_interval = async_track_time_interval(
            self._hass,
            self._schedule_poll,
            self._interval,
        )
        self._schedule_poll(None)

    def on_entry_ready(self) -> None:
        """Launch the first poll once service admission opens after setup."""
        self._schedule_poll(None)

    async def async_stop(self) -> None:
        """Stop future polls and finish cancellation of an active poll."""
        if self._cancel_interval is not None:
            self._cancel_interval()
            self._cancel_interval = None
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @callback
    def _schedule_poll(self, _now: object) -> None:
        """Start a poll unless one is already active."""
        if not getattr(self._store, "accepting_services", True):
            return
        if self._task is not None and not self._task.done():
            return
        self._task = self._hass.async_create_task(
            self._async_poll(),
            "daylight_calendar_import_email_poll",
        )

    async def _async_poll(self) -> None:
        """Run one poll cycle while keeping transient failures retryable."""
        try:
            result = await async_poll_email_source(
                self._source,
                self._store,
                self._processor,
                attachment_stager=self._async_stage_attachments,
            )
            if (
                result.normalization_failures
                or result.processing_failures
                or result.acknowledgement_failures
            ):
                _LOGGER.warning(
                    "Direct IMAP poll completed with retryable failures: "
                    "normalization=%d processing=%d acknowledgement=%d",
                    result.normalization_failures,
                    result.processing_failures,
                    result.acknowledgement_failures,
                )
            if result.terminal_failures:
                _LOGGER.warning(
                    "Direct IMAP poll permanently rejected %d message(s); "
                    "their source identities were recorded as handled",
                    result.terminal_failures,
                )
        except asyncio.CancelledError:
            raise
        except DirectImapError as exc:
            _LOGGER.warning("Direct IMAP poll failed: %s", exc)
        except Exception:
            _LOGGER.exception("Email poll failed")

    async def _async_stage_attachments(self, envelope, document):
        return await async_stage_email_attachments(
            self._hass,
            envelope,
            document,
        )


async def async_setup_email_runtime(
    hass: HomeAssistant,
    entry: ConfigEntry,
    store: PendingImportStore,
    processor: EmailDocumentProcessor,
) -> EmailPollingRuntime | None:
    """Start Direct IMAP polling when email ingestion is enabled."""
    options = getattr(entry, "options", {})
    if not options.get(CONF_EMAIL_ENABLED, False):
        return None

    source = DirectImapSource(
        direct_imap_settings_from_options(entry.entry_id, options)
    )
    runtime = EmailPollingRuntime(hass, source, store, processor)
    await runtime.async_start()
    return runtime
