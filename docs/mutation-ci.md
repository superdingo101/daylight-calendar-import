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
- No compatible cache is necessary. On every PR check, mutation state starts
  clean, avoiding silent use of stale verdicts.
- The existing 95.22% threshold, no-tests, suspicious and segfault guards apply
  to the selected mutation scope. Scoped results **are not project-wide scores**.

The job summary and downloadable `.pr-mutation-scope.json` show the exact
selection. The temporary `only_mutate` setting is restored after the job.
A scoped mutation run can still be expensive for large shared modules and mutmut
must still collect test-to-function associations.

## Nightly and release validation

`Clean mutation validation` continues to run the entire suite from scratch
every night at 09:00 UTC. Run artifacts retain mutation health and survivor
reports. Nightly failures create or update a Mutation Health issue; the next
successful run closes that issue. Passing runs without open alerts are quiet.
A clean run of the exact release candidate remains mandatory before publishing,
with `Release Mutation Validation` and an exact source-tree proof.
Neither PR-scoped mutation scores nor cached incremental results can satisfy
the release quality gate.

The former PR baseline-rebuild workflow is no longer necessary for ordinary
PR checks. The GitHub Actions pip installation cache is separate and remains.
