# Import events without email

You can use Daylight Calendar Import without configuring an email account. Install the integration, choose a working Home Assistant AI Task entity, and configure a default writable calendar included in the allowed calendars. Open **Daylight imports → Review inbox**. The **Paste event details** and **Upload an image or PDF** forms remain visible with an empty inbox and with Direct IMAP disabled, including on mobile.

## Paste an announcement

1. Paste the announcement into **Event details**. Include explicit dates, start/end times and locations when available. A single submission can contain multiple events.
2. Optionally start the text with a configured routing hint, such as `Calendar: Kids`.
3. Select **Import events** and wait for the result.
4. Select **Review events** when drafts are ready. Verify every draft, resolve any routing warning, choose its destination calendar, then approve or reject it.

Submission only saves drafts. Approving a draft is the step that writes to your calendar.

## Upload a flyer, screenshot or PDF

1. Under **Upload an image or PDF**, choose one JPEG, PNG, WebP image or PDF.
2. Optionally add supporting text, including a calendar routing hint. Avoid repeating information that conflicts with the file.
3. Select **Import file** and wait for the result. Review and approve the extracted drafts as above.

The maximum file size is 10 MiB. PDFs may have up to 30 pages. The file's contents must match a supported format; renaming an unsupported file does not convert it. Images and scanned PDFs require an AI Task entity that supports attachments. PDFs with a usable text layer can be processed by a text-only AI Task entity. If a PDF needs visual interpretation, Daylight requires attachment support rather than silently ignoring the image content.

## Understand the result

- **Ready for review:** Drafts are saved. Check dates, time zones, meeting details and calendars before approval.
- **Duplicate:** The source or extracted events were already handled. Check existing drafts and activity; resubmitting does not create a second copy.
- **No events found:** The provider found no sufficiently complete events. Add explicit dates and start/end times or choose a clearer source before trying again.
- **Failed:** Read the error shown on the form. Provider or processing failures recorded after submission begins appear in **Recent activity**. Browser validation and upload failures, such as unsupported or oversized files or upload permission/network errors, happen before event processing and may have no activity record. Correct the file or upload problem, or repair the selected AI integration as appropriate, then explicitly retry. See [provider troubleshooting](provider-errors.md).

Daylight retains your text and file selection while you use the page so you can correct a submission. It does not automatically retry a failed request. A file retry uses a fresh temporary upload. If a connection loss leaves the outcome uncertain, check **Review inbox** and **Recent activity** before retrying; processing may still have succeeded. Do not repeatedly click Import while processing.

A failed calendar approval is separate from a failed submission. If approval reports an uncertain calendar write, inspect the actual calendar before choosing a recovery action. Do not blindly retry an uncertain write.

## Permissions and privacy

Manual submission requires control permission on the selected AI Task entity. Reading the private inbox and approving drafts also requires control permission on the configured calendars; duplicate-safe approval requires read permission on the destination. A read-only Home Assistant user cannot submit or approve drafts. If an action is denied, ask your Home Assistant administrator to review entity permissions.

Text and file contents are sent to the AI Task provider you configured. Review that provider's privacy and billing terms. Daylight uses Home Assistant's authenticated temporary upload transport; image/PDF staging files are removed after processing. Pending drafts and local activity history remain until resolved or removed by the configured retention policy. No Daylight Cloud account is needed.
