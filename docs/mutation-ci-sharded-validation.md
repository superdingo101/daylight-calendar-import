# Comprehensive cold mutation validation

The reusable comprehensive executor is shared by PR, nightly and release
validation. Each tracked integration Python module belongs to exactly one of
eight source-size-balanced shards. Every shard starts without cached verdicts.

Results bind the exact source SHA/config manifest, assigned module list,
duration, raw counts and canonical mutant-identity/outcome map to a resolved
environment fingerprint. Missing metadata, incomplete or duplicate identities,
and counts that disagree with outcomes are rejected. Fingerprints include Python,
OS/architecture, the digest-pinned execution image, actual Node version and every
installed Python distribution version. Container runs bind the executed userland
rather than GitHub's rolling host-image stamp. Local/legacy host-only runs still
bind their runner image and version. The engines install dependencies without a
configured pip cache; no mutant verdicts are cached.

The reducer requires every shard job/result and matching environments, sums raw
counts, and calls the original 95.22% score/error-category checker. Survivor
diagnostics are preserved per shard and joined only when all expected files
exist. Partial results cannot create a release proof.

A manual dispatch can set `compare_unsharded: true` to repeat the exact-SHA
comparison. Both engines share the same run's target and manifest. The dependent
comparison requires identical identities and individual scored outcomes;
killed/timeout equivalence retains the existing quality policy. Matching total
counts alone never establish equivalence.

PR #129 proved equality on all 9,465 individual mutants at 95.52%: 15m 50s
sharded versus 58m unsharded, including setup. See [CI policy](mutation-ci.md)
for bounded scope decisions, budgets, release proof semantics and rollback.
No 15-minute ordinary-PR SLA is claimed until real cutover rehearsals prove it.
