# Daylight Calendar Card connection

Daylight Calendar Card can open the file-import and pending-review workflows when the Import integration advertises support. Import can still be used on its own. The card does not submit or approve events; review and calendar writes remain in Import.

## Integration contract, version 1

The authenticated `daylight_calendar_import/capabilities` WebSocket command reports active `entry_ids` and a `capabilities` object. `queue_metadata`, `direct_routes`, and `return_sessions` identify the supported versions of those features. Callers must check each capability they use. The pending-events and pending-imports sensors expose `daylight_import_kind`, `daylight_import_contract_version`, and `daylight_import_config_entry_id`; their state remains the count. Uncertain calendar writes remain in the pending queue and need confirmation.

The supported panel paths are `/daylight-calendar-import/new/file`, `/daylight-calendar-import/review`, and `/daylight-calendar-import/review/<import-id>`. Opening a path does not approve or create an event. Malformed or unavailable imports leave the user in Import with a path back to the inbox.

A Card-launched workflow may include `dl_return`, an opaque session ID. The corresponding `daylight:return:v1:<id>` record lives in same-origin session storage in the originating tab, expires after at most 24 hours, and contains the originating dashboard path and card state. Import validates the session and destination before using them. It writes a completed or cancelled result before returning. The card consumes the result and refreshes its calendar. The URL and session contain no flyer text or event details.

If a completed review leaves other imports, the user can return to the calendar or continue reviewing. Errors, uncertain writes, missing sessions, and expired sessions never trigger an automatic return. A direct sidebar visit has no return session.
