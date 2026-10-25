# Redacted diagnostics

Administrators can download Daylight's native Home Assistant diagnostics from the
integration's diagnostics menu. The integration-specific data contains only
operational aggregates and declared parser capabilities:

- Loaded and service-admission booleans.
- Pending import/event counts and pending/uncertain event-status counts.
- Retained lifecycle record counts, status counts and transition-type counts,
  including terminal `notification_delivery_failed` transitions.
- Retained classified provider-failure counts.
- Notification journal counts by delivery status.
- Whether an AI Task is configured, its declared text/image/PDF capabilities,
  attachment limits and a bounded capability-discovery error category.

The integration builds a whitelist projection. It exports no config-entry data
or options, source text, titles, descriptions, senders, attachment references,
credentials, mailbox details, entity identifiers, import/event identifiers,
fingerprints, timestamps, provider messages, warnings or lifecycle guidance.
Unrecognized stored categories are counted under the fixed `unknown` key rather
than copied into the download. Home Assistant supplies its own standard download
metadata separately.

The download reads current in-memory snapshots. It neither loads nor changes
storage, sends notifications, parses sources nor calls an external provider.
An unloaded entry reports no runtime records; a configured AI Task may still
provide capabilities through Home Assistant's local entity component. Capability
discovery errors are returned as safe categories. Capabilities describe the
adapter's supported inputs and do not prove provider connectivity or health.
Lifecycle totals describe retained history rather than lifetime statistics.
