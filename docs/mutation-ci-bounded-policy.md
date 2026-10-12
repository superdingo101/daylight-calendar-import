# Bounded PR mutation policy

The candidate planner from PR #125 now selects the required execution tier.
Selection uses actual merge-base/head revisions and before/after function
coverage, including fixtures. Unknown/broad impact is visibly comprehensive;
no cache miss can turn a bounded job into an implicit single-runner full run.

Eligible scopes contain at most four existing production modules. Two cold
jobs test exactly those same module names at base/head, with a 20-minute hard
limit on each job. Results bind revisions, canonical plan, environments,
individual mutant identities and raw counts. The required score checks the
95.22% floor and a zero-regression same-scope comparison. Different source
contents can produce different mutant populations; base/head percentages are
compared only for identical module scopes, not claimed as identical mutants.
New modules and sources without mutable functions escalate to full validation.

Applying `mutation-comprehensive` explicitly requests the complete sharded
tier, including recovery from a scoped runtime-budget failure. It cannot
waive score, completeness, identity or error-category checks.

See [mutation CI](mutation-ci.md) for complete execution/release safeguards.
