# Mutation testing in CI

## Pull requests: scoped mutation testing

The required `Mutation score` check no longer requires a compatible GitHub
mutation-results cache. It uses a merge-base (three-dot) diff between the PR's actual base SHA
and head SHA, supporting stacked pull requests even when their base advances.

- Production Python changes: mutate only the changed Python modules using
  mutmut's `only_mutate` configuration. All tests remain discoverable by mutmut
  when mapping tests to mutated functions.
- Newly added application tests: run the new tests under coverage.py and include
  the production modules they exercise **alongside** any changed source.
- Edited or deleted existing application tests: use a full clean fallback,
  because post-edit coverage alone cannot show which modules lost tests.
- Changes in shared helpers (including `tests/hosted_contract.py`), fixtures,
  dependencies or pytest configuration also require a full uncached run.
- Documentation, frontend-only or CI-only changes: explicitly record that
  there is no applicable Python mutation scope; ordinary tests still apply.
- Mutation state is *optionally* reused from an earlier run of the **same PR**.
  CI validates mutation scope, all tracked non-test inputs, installed dependency
  versions and Python interpreter. If any differ, it starts completely clean.
- Test changes are eligible for warm reuse only when their previous Python AST
  is unchanged and the only additions are undecorated, top-level `test_*`
  functions appended at the end of existing test files. Previous killed mutants
  remain killed; survivors, no-tests, timeouts and other non-killed verdicts
  are explicitly reset and retested. Replaced assertions, fixtures, imports,
  modified/deleted tests, and newly created test modules force a clean run.
- The workflow saves completed `mutants/` state **even if the 95.22% gate
  fails**. A later PR commit may reuse this state; interrupted, timed-out or
  incomplete mutation runs are not published. Cache misses never bypass
  mutation execution or score enforcement.
- The existing 95.22% threshold, no-tests, suspicious and segfault guards apply
  to the selected mutation scope. Scoped results **are not project-wide scores**.

The job summary and downloadable `.pr-mutation-scope.json` show the exact
selection. The temporary `only_mutate` setting is restored after the job.
A scoped mutation run can still be expensive for large shared modules and mutmut
must still collect test-to-function associations.

## GitHub Actions cache behavior

The pull-request workflow uses `actions/cache/restore@v4` and
`actions/cache/save@v4` with PR-scoped, immutable, per-run keys. GitHub's
PR-ref cache isolation prevents unrelated PRs from sharing mutant verdicts.
The cache contains generated `mutants/` output and a provenance marker;
the marker is removed before a warm run and re-created only after `mutmut run`
returns successfully. The test tree copied under `mutants/tests/` is replaced
from the current checkout before reuse, because mutmut 3.8.0 otherwise keeps
old copies.

If safe reuse is impossible (or GitHub evicts the cache), the job simply
runs mutmut from scratch. Because the existing selector intentionally chooses
a full scope for modified application tests, this can still be expensive; the
cache reduces rechecking previously killed mutants on strictly additive fixes,
but does not alter the scope-selection safety rules.

GitHub's dependency (`pip`) cache is independent from the mutant-result cache.
The 95.22% requirement and the fail-closed status guards are unchanged.

## Nightly and release validation

`Clean mutation validation` continues to run the entire suite from scratch
every night at 09:00 UTC. Run artifacts retain mutation health and survivor
reports. Nightly failures create or update a Mutation Health issue; the next
successful run closes that issue. Passing runs without open alerts are quiet.
A separate dependent reporting job also catches clean-job timeouts, even when
mutation statistics cannot be uploaded.
A clean run of the exact release candidate remains mandatory before publishing,
with `Release Mutation Validation` and an exact source-tree proof.
Neither PR-scoped mutation scores nor cached incremental results can satisfy
the release quality gate.

The former PR baseline-rebuild workflow is no longer necessary for ordinary
PR checks. The GitHub Actions pip installation cache is separate and remains.
