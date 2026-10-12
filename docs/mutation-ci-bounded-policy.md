# Bounded PR mutation impact: candidate policy

This is **pre-production instrumentation** introduced after PR #124.
The required `Mutation score` check, its 95.22% threshold, and the
nightly/release mutation validation have **not** been replaced.

## Candidate classification

The shadow workflow writes `mutation-impact-plan.json` alongside
`mutation-shadow-plan.json`.

- `skip`: no mutation-relevant application inputs changed. This is an
  impact-plan classification, **not** a claim that a mutation score passed.
- `scoped`: changed Python application files unioned with application modules
  whose functions were exercised by added/edited tests, including fixture
  setup and teardown. A selected scope of at most **four** modules is eligible
  for a future bounded execution job.
- `coverage-required` / `needs-base-and-head-coverage`: coverage has not yet
  been collected. Such plans are **never** eligible for execution.
- `comprehensive-sharded`: broad or uncertain impact. This is an **escalation
  request**, not a completed validation. The sharded executor is a later PR.
- `error`: impact could not be planned; never interpreted as a pass.

For edited/deleted application tests, the planner measures the tests at their
actual merge-base commit in a temporary detached Git worktree, and also tests
that still exist on the PR head. It uses the same `pytest-cov` dynamic
test-context analysis as the merged shadow planner, and unions the head and
base application modules with changed production files. Failure in either
probe escalates instead of silently dropping the old coverage. Changed
shared fixtures/config, deleted application modules and non-frontend runtime
resources also escalate.

## Cost and score guarantees

The provisional PR work contract is **four selected modules** and a **20-minute
target execution budget**, but a module count cannot guarantee elapsed time.
`may_execute_mutmut: true` represents **candidate eligibility only**; this
PR does not execute a bounded mutmut job or check its score.

A candidate mutation execution job must still enforce a hard wall-clock
timeout, compare with an appropriate **same-scope baseline** (or another
separately justified scoped standard), and fail if work is incomplete.
The full-project 95.22% score must still be enforced by clean, complete,
non-overlapping comprehensive shards for nightlies and releases.

**Never turn this candidate plan into a required passing check** until the
comprehensive-sharded fallback, a scoped quality policy, and real cold
execution benchmarks exist. A cache miss must never cause a large unsharded
run. The existing workflow remains the rollback path.

## Verification

The test contracts cover test-only additions, modified/deleted tests, a real
Git merge-base/worktree, unknown impact, failure escalation, zero coverage,
and a work-budget overflow. The historical replays for PRs #120, #121 and
#123 are recorded on the implementation pull request. Their behavior is
intentionally conservative: broad changes escalate rather than appearing
as falsely fast passes.
