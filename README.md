# Daylight Calendar Import

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/superdingo101/daylight-calendar-import#install-with-hacs)
[![GitHub release](https://img.shields.io/github/v/release/superdingo101/daylight-calendar-import)](https://github.com/superdingo101/daylight-calendar-import/releases)
[![GitHub stars](https://img.shields.io/github/stars/superdingo101/daylight-calendar-import.svg)](https://github.com/superdingo101/daylight-calendar-import/stargazers)
[![GitHub issues](https://img.shields.io/github/issues/superdingo101/daylight-calendar-import.svg)](https://github.com/superdingo101/daylight-calendar-import/issues)
[![License](https://img.shields.io/github/license/superdingo101/daylight-calendar-import.svg)](LICENSE)

[![Tests](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/tests.yml)
[![HACS Validation](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/hacs.yaml/badge.svg?branch=main)](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/hacs.yaml)
[![Nightly clean mutation](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/mutation-clean.yml/badge.svg?event=schedule)](https://github.com/superdingo101/daylight-calendar-import/actions/workflows/mutation-clean.yml?query=event%3Aschedule)

[![Sponsor](https://img.shields.io/badge/Sponsor-GitHub%20Sponsors-pink?logo=github)](https://github.com/sponsors/superdingo101)
[![Donate](https://img.shields.io/badge/Donate-Buy%20me%20a%20beer-yellow?logo=buy-me-a-coffee)](https://buymeacoffee.com/superdingo101)

**Turn event emails, school newsletters, flyers, screenshots, and PDFs into Home Assistant calendar events — without entering every detail by hand.**

Daylight Calendar Import uses your existing Home Assistant **AI Task** provider to find dates, times, titles, and locations. It creates drafts for you to **review, edit, and approve** before adding anything to your calendar.

The integration runs in your own Home Assistant installation. **No Daylight Cloud account or subscription is required.** Your chosen AI provider may have its own charges and privacy policies.

## How it works

1. **Send in event information.** Connect a dedicated email mailbox, or submit text, an image, or a PDF through Home Assistant's Actions tools.
2. **Let AI identify the events.** A single email or newsletter can contain several events.
3. **Review the results.** Open **Daylight imports → Review inbox** to check dates, correct details, choose calendars, and inspect possible conflicts.
4. **Approve the events you want.** Daylight adds approved events to your selected Home Assistant calendars; rejected drafts are not added.

You stay in control: emails do **not** automatically create calendar events just because they arrive.

## Features

- **Email imports:** Watch one IMAP mailbox for event announcements and supported attachments.
- **Text, image, and PDF imports:** Extract events from copied announcements, photos, screenshots, flyers, and PDFs.
- **Review before adding:** Edit or approve events individually or in a group.
- **Multiple calendars:** Choose a default calendar, select a destination per event, and set up friendly calendar names such as `Kids` or `Family`.
- **Duplicate and conflict checks:** Avoid known duplicates and check other calendars for potential overlaps before approval.
- **Activity history:** See what was processed, approved, rejected, or failed in **Recent activity**.
- **Optional notifications:** Use a Home Assistant `notify` entity to receive selected updates, including events ready for review.
- **Home Assistant automations:** Two sensors expose the number of pending imports and pending events.

## What you'll need

- **Home Assistant 2026.7.4 or newer.**
- At least one writable Home Assistant calendar entity.
- A working **[Home Assistant AI Task](https://www.home-assistant.io/integrations/ai_task)** entity from an AI integration you have configured. The provider must support attachments if you want it to interpret images or scanned documents.
- **HACS** for the recommended installation method. Manual installation is also possible.
- Optionally, an IMAP-compatible email account or folder for automatic email pickup.

## Install with HACS

Daylight Calendar Import is currently added as a **HACS custom repository**, not through the HACS default catalog.

1. In Home Assistant, open **HACS**.
2. Select the **three-dot menu → Custom repositories**.
3. Enter `https://github.com/superdingo101/daylight-calendar-import` and choose **Integration** as the type.
4. Add the repository, find **Daylight Calendar Import** in HACS, and install it.
5. **Restart Home Assistant.**
6. Go to **Settings → Devices & services → Add integration** and select **Daylight Calendar Import**.
7. Select your AI Task entity, a **default calendar**, and the **allowed calendars** to which Daylight may write. Make sure the default calendar is also in the allowed list.

Once configured, open **Daylight imports** in your Home Assistant sidebar. Its **Review inbox**, **Recent activity**, and **Settings** screens are where you'll manage imports.

**Prefer manual installation?** Copy the `custom_components/daylight_calendar_import` directory into your Home Assistant `custom_components` folder, restart Home Assistant, and then add the integration under **Settings → Devices & services**.

## Try your first import

The simplest way to get started is with some copied text — no email setup needed.

1. Open **Developer Tools → Actions** in Home Assistant.
2. Choose **Daylight Calendar Import: Submit text for review** (`daylight_calendar_import.submit_text`).
3. Paste something like:

   ```text
   School movie night
   November 6, 2026, 6:30 PM
   School gym
   ```

4. Run the action, then open **Daylight imports → Review inbox**.
5. Open the new import, check its details, and choose **Approve** to create the calendar event — or **Reject** to discard it.

You can also submit an **image** or **PDF** from Developer Tools → Actions using **Submit image for review** or **Submit PDF for review**. Images must be PNG, JPEG, or WebP (up to 10 MiB); PDFs can be up to 10 MiB and 30 pages. The AI Task entity must support attachments when visual interpretation is required.

> **Always review AI-generated dates, times, and time zones.** Daylight shows some assumptions made during extraction, but AI may still misunderstand ambiguous announcements.

## Import events from email (optional)

For hands-off collection of announcements, Daylight can check a mailbox directly using IMAP. **We strongly recommend a dedicated mailbox or folder used only for calendar imports.**

1. In **Daylight imports → Settings → Email**, enable **Direct IMAP**.
2. Enter your IMAP server, port, username, password or app password, and mailbox/folder. The connection uses encrypted IMAP (typically port `993`).
3. Optionally add an **allowed senders** list, with one full email address per line.
4. Send or forward an event announcement to the mailbox.
5. Open **Review inbox** to check and approve any events Daylight extracts.

Daylight checks the mailbox immediately after enabling the connection and about every five minutes afterward. It processes **unread, undeleted** messages and attempts to mark messages as read after their result has been saved (including certain failed messages).

> **Important privacy note:** Email access is not limited to messages that actually contain events. When connected, Daylight can fetch every unread, undeleted email in its configured mailbox, including existing unread mail. Sender filtering is not a security boundary, and event content may be sent to your configured AI provider. **Do not connect your primary personal inbox without understanding this behavior.** Read the [Direct IMAP setup and privacy guide](docs/direct-imap.md) before enabling it.

The email connector supports one IMAP mailbox per integration configuration, with password/app-password authentication over implicit TLS. OAuth-only sign-in and hosted email forwarding are not currently supported.

## Use different calendars

Under **Daylight imports → Settings**, configure your default calendar, allowed writable calendars, and optional calendar aliases. For example, create an alias `Kids` for your children's calendar.

Then start an email body or a manual text submission with:

```text
Calendar: Kids
School concert on November 12, 2026 at 7:00 PM
```

Daylight uses that alias to suggest the destination calendar for the extracted events. You can still change individual events during review. An unknown or conflicting calendar hint must be explicitly resolved before approval.

You can also select **Check calendars** on a draft to look for exact duplicates, possible duplicates, and scheduling overlaps. These results are advisory and may become outdated. Daylight makes a fresh **exact-duplicate check against the destination calendar** before writing an approved event; it does not automatically block all overlapping events.

See the [v0.6 feature guide](docs/v0.6.md) for calendar routing, permissions, and queue sensors.

## Frequently asked questions

**Do I need Daylight Calendar Card?**  
No. [Daylight Calendar Card](https://github.com/superdingo101/daylight-calendar-card) is a separate Home Assistant dashboard card. Calendar Import works with your existing Home Assistant calendar integrations.

**Does Daylight send my information to an AI service?**  
Daylight uses the **AI Task entity you choose**. Depending on that entity and provider, submitted text, email content, or attachments may leave your Home Assistant instance. Review your AI provider's privacy and billing terms. Daylight does not require its own hosted service.

**Will Daylight add every event automatically?**  
No. The normal email and submission workflow saves drafts for your approval. There is a separate advanced immediate-import action for automations, but it bypasses the usual review workflow.

**What if an event looks wrong?**  
Edit or reject it in **Review inbox**. If a calendar write has an uncertain outcome, inspect your actual calendar before choosing a recovery action; Daylight will not blindly retry it.

**Where do I see errors or imports that didn't create events?**  
Open **Daylight imports → Recent activity**. For AI provider timeouts, authentication problems, and similar errors, see the [provider troubleshooting guide](docs/provider-errors.md).

## Help and documentation

- [Latest releases and updates](https://github.com/superdingo101/daylight-calendar-import/releases)
- [Report a bug or suggest a feature](https://github.com/superdingo101/daylight-calendar-import/issues)
- [Direct IMAP setup, privacy, and recovery](docs/direct-imap.md)
- [Calendar routing, conflicts, and sensors](docs/v0.6.md)
- [Advanced review actions and permissions](docs/review-api.md)
- [Settings API reference](docs/settings-api.md)
- [Safe reprocessing of pending text](docs/source-reprocessing.md)
- [Technical architecture and future hosted API](docs/hosted-api.md)
- [Mutation testing and CI policy](docs/mutation-ci.md)

## License

Daylight Calendar Import is open-source software under the [MIT License](LICENSE).
