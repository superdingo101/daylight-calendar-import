# Mutation testing in CI

The required **Mutation score** check has two explicit cold execution tiers.
It never restores mutant verdicts or silently falls back to one full-project
job. Ordinary pytest (100% coverage), frontend and HACS checks remain required.

## Bounded PR validation

The planner compares the PR head against its actual merge base, including
stacked PRs. New tests contribute function execution, including fixture setup
and teardown, instead of import-only coverage. Edited/deleted tests are probed
on both revisions. Shared fixtures/helpers, collection-time execution, runtime
resources, dependencies, deletions, new production modules and scopes exceeding
four modules require comprehensive validation. Sources without mutable
functions also use comprehensive validation rather than an empty passing run.

An eligible scope runs cold at both base and head on independent runners.
Each job has a **20-minute hard timeout**, including dependency installation.
Both results must be complete, use identical resolved environments and module
sets, and account for every individual mutant. The head must meet the existing
**95.22% floor** and cannot regress against the clean same-scope base percentage.
No skipped, no-tests, suspicious or segfault results can pass. New-module scores
are never compared to unrelated base percentages: new modules use full
validation until a separately rehearsed new-module policy exists.

The plan artifact explains selected modules and escalation. If an eligible
scope exceeds its budget, apply **mutation-comprehensive** to the PR to request
the explicit sharded tier. A timeout never counts as success. Four modules is
an eligibility heuristic, not a proven 15-minute SLA; measured cold runs should
guide adjustments. CI-only changes explicitly report no applicable scope.

## Comprehensive validation

Broad/ambiguous PRs, nightly runs and release candidates run eight cold,
non-overlapping mutmut shards. The reducer requires every expected result,
exact SHA/source/config manifest, identical resolved environments, complete
individual identities and consistent raw counts. It sums counts before
applying the **95.22% project-wide threshold**. Shard percentages are never
averaged. Missing, failed, timed-out or cancelled shards fail the required gate.

Each shard preserves survivor diffs and available diagnostics on failure.
Successful aggregation requires all expected diagnostic files. Mutation engines
install dependencies afresh in their pinned container without a configured pip
cache. Bounded planning may use a pip cache for its host-side coverage measurement;
it contains no mutant verdicts.

## Nightly and releases

Clean mutation validation still runs every night at 09:00 UTC. Failures create
or update the Mutation Health issue; the next successful run closes it. A
separate reporter catches timeouts and missing artifacts.

Release preparation calls Clean mutation validation for the exact candidate
SHA. Only a successful first-party main-dispatched workflow with an unexpired
proof for the identical Git source tree/version can authorize publishing.
Proof creation independently rechecks every shard and enforces the full score.
PR-scoped scores and rehearsal workflows cannot authorize a release.

The manual **Unsharded mutation rollback** workflow retains the prior cold
engine for explicit comparison. It does not publish a release proof or supply
PR status. A required-workflow rollback is a reviewed revert of this cutover;
do not restore the retired custom cache engine.

## Verified comprehensive benchmark

Bounded base/head jobs, comprehensive shards and the optional unsharded
comparison run inside the same digest-pinned Python container with an exact Node
version. Their environment
evidence binds the immutable container, actual Python/Node versions and complete
resolved Python package set. GitHub may roll the host runner image during a run;
the host provisioning stamp does not identify the container's userland. Host-only
local/legacy runs still bind their runner image and version. Environment equality,
artifact integrity, complete mutant coverage and the 95.22% score gate remain
required. Runtime-image and Node upgrades require a reviewed workflow change.

On `ae000de` (PR #129), the cold eight-shard path completed in **15m 50s**,
compared with **58m** unsharded. Both produced exactly the same individual
outcomes for **9,465 mutants across 33 modules**, scoring **95.52%** in the same
resolved environment. Sharding used about 70 total runner-minutes versus 58;
shorter elapsed time does not imply fewer runner-minutes.

Evidence: https://github.com/superdingo101/daylight-calendar-import/actions/runs/38266200306
