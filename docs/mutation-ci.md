# Mutation testing in CI

## Policy

- Every PR gets an incremental mutmut 3.8.0 run and a required `Mutation score`
  check. The minimum score is defined by `mutation-baseline.json` (95.22%).
- Every night at 09:00 UTC, a clean run of `main` starts with no `mutants/`
  directory. It publishes detailed survivor reports, a GitHub Actions summary,
  a scheduled workflow badge, and a persistent **Mutation Health — Nightly Clean
  Validation** issue with daily result comments.
- **Update Integration Version** creates a release-preparation PR, dispatches
  ordinary Tests and HACS workflows (needed because `GITHUB_TOKEN`-created
  PR events do not launch Actions), and invokes the clean reusable workflow
  on the exact release candidate commit. The clean job adds a
  `Release Mutation Validation` status to that commit.
- **Publish HACS Release** rejects any release for which it cannot find an
  unexpired, clean proof artifact from a successful Update Integration
  Version run or manually dispatched Clean mutation validation on `main`,
  for the same release tag and Git source tree. It pins the release target to the verified checked-out commit.
  No incremental result or nightly result can satisfy release validation.

## Incremental cache behavior

Ordinary PRs first try caches scoped to that PR, then explicitly rebuilt
PR caches from the default branch, then compatible nightly baselines from `main`. GitHub Actions caches are immutable and PR caches
cannot generally be shared with another PR. Every successful PR run saves
a uniquely keyed state for its next update. The nightly clean job publishes
the default-branch baseline.

The cache validator first verifies the saved fingerprint against the exact
Git snapshot that produced it. A verified cached `main` snapshot is reusable
when a PR only adds independent production modules or `test_*.py` test files,
changes production function bodies (which mutmut hashes itself), or modifies
frontend JS/CSS assets. Because mutmut 3.8.0 collects new-test mappings but
does not automatically revisit decided mutants, newly added test files reset
cached **surviving, uncovered, timeout, and otherwise non-killed verdicts**
before mutmut runs. Kills by unchanged existing tests remain valid. New tests
are then collected and the invalidated mutants rerun using their refreshed
test associations. When a new production Python module is added, the validator
also discards only `mutmut-stats.json` so mutmut recollects test-to-function
associations from **all** tests. Cached non-killed verdicts are also reset
when new source modules appear, since old surviving/uncovered functions can
become newly reachable through the added module. Existing killed verdicts
remain reusable. This avoids incorrectly treating new functions as uncovered
when already-existing tests execute them. The validator also accepts strictly cache-only changes to the
mutation workflow while rejecting changes to its test runner or mutmut command.

**Existing** test files, `conftest.py`, fixtures, dependencies, non-Python
integration inputs, source imports/constants, and signatures remain
conservatively invalidating. Version-only changes to
`pyproject.toml`'s `[project].version` and the Home Assistant integration
`manifest.json`'s `version` do **not** invalidate cached results.
The validator parses both files and still invalidates on every other field,
including requirements, Python compatibility, mutation/test configuration,
and dependencies. Unknown changes or missing Git history invalidate
the cache. The environment fingerprint also includes the exact resolved
installed Python dependencies so a changed package version fails closed.
The first nightly baseline format remains readable; it does not need to
be rebuilt simply because this validator changed. Subsequent successful PR
runs also stamp a self-contained per-file manifest and environment
fingerprint, so their cached state does not depend on temporary GitHub
pull-request merge commits remaining reachable after the next push.

The PR job attempts its own prior cache first, then a separately restored
trusted `main` cache if the PR cache is stale or unavailable. GitHub checkout
fetches complete Git history for legacy cache verification.

**PR jobs never start a full mutation run on a cache miss.** They try
the previous PR cache, an explicitly rebuilt PR cache saved on the default
branch, and the shared nightly `main` cache, in that order. If none passes
provenance validation, the required `Mutation score` check fails immediately.

To recover, open **Actions → Rebuild PR Mutation Cache → Run workflow**.
Select `main` and enter the **open PR number**. The workflow accepts same-repo
PRs (including stacked PRs), pins GitHub's **exact PR merge commit**, and
performs one intentionally full, uncached 60-minute mutation run. It verifies
the normal score and completeness gates, confirms neither the PR head nor
its base/merge tree changed, and stamps a PR-specific recovery cache under
`mutmut-v2-linux-py314-v380-rebuild-pr-<PR number>-`. That cache is saved by
the default-branch dispatch so the selected PR's next run can restore it.
Other PRs cannot accidentally select it.

On success, the recovery workflow records a separate `PR Mutation Cache Rebuild`
status (it does **not** impersonate the required `Mutation score` check).
It attempts to rerun the latest failed mutation check for the selected PR.
If GitHub cannot rerun it automatically, open the failed check and select
**Re-run jobs** yourself. The ordinary check then restores the recovery
cache, validates it, and records the required passing `Mutation score`.

**Do not rerun a full main baseline repeatedly for a PR-specific mismatch.**
Changes to existing tests, fixtures and dependencies may require a new
PR-specific baseline, which the recovery workflow supplies. A recovery run
that fails its mutation score or detects a moving PR head never publishes
a cache. This design prevents routine commits from triggering 45–60-minute
clean mutation jobs while keeping merges blocked until mutation testing passes.

PR mutation jobs are capped at 25 minutes, including setup; clean nightly
and release validation retain a 60-minute timeout. No cache is saved if
the mutation quality gate fails. PR jobs have a read-only token and cannot
consume privileged release proof as a baseline.

The normal `python scripts/check_mutation_score.py` score enforcement
and complete survivor artifacts are retained. If GitHub cache storage or
restoration fails, discard state and run uncached rather than passing based
on stale metadata.

## Nightly results

Visit [Clean mutation validation](../.github/workflows/mutation-clean.yml)
or the repository Actions page (filter on scheduled runs). The README
displays the scheduled-job status badge. The open GitHub issue titled
`Mutation Health — Nightly Clean Validation` contains the latest result
and a comment per night; the workflow artifacts contain full survivor diffs.
A failed or absent nightly does not automatically block unrelated PR merges,
but publication always requires its own clean release validation.

## Release proof and exact source trees

The release-preparation action calls the clean workflow after creating the
release PR. On success, it uploads an attestation with the release tag,
tested candidate commit, full Git tree hash, mutmut statistics, and caller
run ID. Both `Release Mutation Validation` and the standard required `Mutation score`
commit statuses are recorded on the release candidate, because the
GITHUB_TOKEN-created PR does not fire the normal `pull_request` mutation job.

The publisher checks the **conclusion** and provenance of either a
successful `Update Integration Version` run or a manually dispatched
`Clean mutation validation` run on `main`, and downloads the successful
`release-clean-vX.Y.Z` artifact, checks its tag/run ID/schema/clean mode,
and compares its tested Git tree to `main` being published. A squash merge
can change the commit SHA but leave the Git tree identical; that is allowed.
Any source-tree difference, missing or expired artifact, failed workflow, or
invalid proof blocks publication. The publication target is pinned to the
verified checked-out SHA to avoid a moving-`main` race.

A version update run with no new release-preparation commit cannot create
new evidence. If the version is already on `main` but other commits changed
the source tree, manually dispatch **Clean mutation validation** from `main`,
supplying the current release tag and (optionally) exact commit SHA. This
runs the full uncached suite and creates fresh publishable evidence. New changes pushed to the release-preparation PR
invalidate its status and usually its source-tree proof.

## Repository settings

Require the stable **Mutation score** check, alongside normal test, HACS,
and Hassfest checks, under branch protection on `main`.
`Release Mutation Validation` is *release-specific* and should not be
required for every ordinary PR. Instead, the publisher enforces it from
first-party clean-run evidence. Review release-preparation PR statuses
before merging; the publisher still fails closed if the proof is stale.
Ensure GitHub Issues and Actions are enabled for nightly reporting.
