# Comprehensive clean mutation sharding (rehearsal)

This is the third mutation-CI redesign PR, after shadow instrumentation
(#124) and the candidate bounded-impact planner (#125).

**The original required `Mutation score` check, Clean mutation validation
nightly job, 95.22% global threshold, and release-proof workflow are
unchanged.** The new workflow is informational until a full real run confirms
score equivalence with the unsharded `mutmut run` on the **same exact SHA**.

## Opt-in run

The new GitHub Actions workflow **Clean mutation shards (rehearsal)** runs
on `workflow_dispatch` after it reaches `main`, or on pull requests
explicitly labeled `mutation-shards-rehearsal`. The label is opt-in because
running eight full Python dependency environments consumes more total GitHub
Actions minutes than a single long job even if wall-clock duration falls.

A planning job scans every tracked integration Python source file (excluding
`frontend/`), fingerprints their contents and the mutmut configuration,
and assigns each module to one of eight shards. Greedy packing uses file
size as a **provisional proxy** for mutation runtime. Each shard has its own
clean `mutants/` directory and `only_mutate` list, and runs `mutmut run`
and `mutmut export-cicd-stats`. Jobs each have a 60-minute hard safety
limit during the initial rehearsal; the target faster wall-clock budget is
unproven, not promised.

Each successful shard records its exact source SHA, manifest fingerprint,
assigned module list, duration, and the raw mutmut status counts. The reducer
rejects missing/extra/duplicated results, failed/incomplete shards,
inconsistent source revisions, changed fingerprints, non-exhaustive module
coverage, and malformed counts. Only after validation does it sum
`killed`, `timeout`, `survived`, `skipped`, and failure categories and run
the **existing `check_mutation_score.py`** against the combined statistics.
No averaging of per-shard percentages. A failure or timeout cannot become a
partial passing score.

The aggregate artifact `mutation-sharded-aggregate` is explicitly
**not** the release mutation proof. The existing release publisher continues
to accept only its first-party clean proof; this PR deliberately creates no
release proof and posts no successful release validation statuses.

## Validation before production cutover

1. Verify all eight real shard jobs finish for the exact same candidate SHA.
2. Inspect the complete manifest, partition map, raw stats, and timings.
3. Compare aggregated mutant counts, statuses and score against an
   **unsharded clean run of the exact same SHA**, with equivalent dependencies.
   Fix any discrepancies before relying on it.
4. Exercise failure paths (missing artifact, wrong SHA, incomplete statistics
   and a failed/timed-out shard); all must fail.
5. Once verified, a **separate cutover** must route nightly/release validation
   through the reducer while preserving exact-SHA release-proof creation,
   the existing 95.22% threshold and required `Mutation score` status.
   The old workflow remains rollback until then.

Avoid guessing runtime based on file size; record actual per-shard times to
rebalance later. Source files with unusually many mutants may still dominate
one shard. Fine-grained splitting would require its own validation.
