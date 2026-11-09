# Daylight Calendar Import

Turn event emails, newsletters, photos, flyers, and PDFs into **reviewable Home Assistant calendar drafts**. Daylight uses your configured AI Task entity to extract dates, times, event titles, and locations. You review and edit drafts before approving them.

**No Daylight Cloud account or subscription is required.** Your selected AI provider may charge for requests and may receive submitted content.

## Requirements

- Home Assistant 2026.7.4 or newer
- A writable Home Assistant calendar
- A working [AI Task entity](https://www.home-assistant.io/integrations/ai_task); image and scanned-PDF imports need an attachment-capable provider
- Optional dedicated IMAP mailbox or folder for email pickup

## Install and try it

1. [Open the repository in HACS or Home Assistant Marketplace](https://my.home-assistant.io/redirect/hacs_repository/?owner=superdingo101&repository=daylight-calendar-import&category=integration). Until this project enters the default catalog, add it as a **custom integration repository** if prompted.
2. Install Daylight Calendar Import and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, select **Daylight Calendar Import**, and choose your AI Task entity and default/allowed calendars.
4. Open **Daylight Imports** in the sidebar. Select **New import**, paste an event or upload an image/PDF, and review the extracted drafts. Approve only the ones you want on your calendar.

The normal import workflow **never automatically approves calendar events**. Always verify AI-generated dates, times, and destinations.

## Privacy

Manual inputs and files can be sent to your configured AI provider. Review its privacy policy and billing terms.

**Direct IMAP is optional.** If enabled, Daylight can fetch every unread, undeleted message from the configured mailbox or folder—even unrelated messages—and send their contents or attachments to your selected AI provider. It may mark messages read after saving the result. Use a dedicated import mailbox or folder, **not your primary inbox**. [Read the IMAP privacy and setup guide](https://github.com/superdingo101/daylight-calendar-import/blob/main/docs/direct-imap.md).

## More information

- [Full documentation and setup](https://github.com/superdingo101/daylight-calendar-import#readme)
- [Manual import guide](https://github.com/superdingo101/daylight-calendar-import/blob/main/docs/manual-import.md)
- [Releases](https://github.com/superdingo101/daylight-calendar-import/releases)
- [Report a problem](https://github.com/superdingo101/daylight-calendar-import/issues)
