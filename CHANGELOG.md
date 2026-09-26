# Changelog

## 0.2.0

- Persist stable per-event IDs, statuses, and calendar destinations with migration of existing configuration and pending storage.
- List, inspect, edit, approve, or reject individual pending events; retain batch approval and rejection with per-event write checkpoints.
- Resolve uncertain calendar writes explicitly as created, not created, or discarded without automatic duplicate retries.
- Preserve source and event deduplication through edits and per-event decisions.
- Keep valid drafts when an AI response contains malformed siblings, and return indexed warnings.
- Maintain 100% branch coverage across current and minimum supported Home Assistant versions, plus the mutation-score gate.
