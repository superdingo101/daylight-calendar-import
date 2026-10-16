# Synthetic historical upgrade fixtures

These fixed JSON records use historical serializer shapes, rather than the
current `as_dict()` methods. All names, credentials, messages, and appointments
are synthetic. They contain no user mailbox data.

- `storage-v1.json`: original flat event schema accepted by `_migrate_v1`,
  with the import-wide interrupted-approval checkpoint. This is a schema
  fixture, not a claim that a published v0.1 release shipped storage.
- `v0.5.3.json`: schema 2 nested event records with stable per-event identity,
  calendar destination and uncertain-write attempt; no notification journal.
- `v0.7.1.json`: schema 2 plus a notification delivery retry journal.
- `config_entries.json`: entry version 1 with a default-calendar option override,
  and entry version 2 with multi-calendar/email/routing settings.

Storage shapes were checked against the v0.5.3 and v0.7.1 release serializers
and the schema 1 migrator. Fixtures deliberately omit newer optional fields.
The test freezes time so retention cannot age its notification state out.
