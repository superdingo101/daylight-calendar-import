# Mutation testing in CI

## Pull requests: scoped mutation testing

The required `Mutation score` check no longer requires a compatible GitHub
mutation-results cache. It calculates changes between the PR's actual base SHA
and head SHA, supporting stacked pull requests.

- Production Python changes: mutate only the changed Python modules using
  mutmut's `only_mutate` configuration. All tests remain discoverable by mutmut
  when mapping tests to mutated functions.
- Changes limited to application test modules: execute the changed tests with
  coverage.py, then mutate production modules executed by those tests.
- Changes in shared fixtures, dependencies, pytest configuration or deleted tests:
  run a full uncached mutation check because narrowing would be unsafe.
- Documentation, frontend-only or CI-only changes: skip Python mutation testing;
  standard Python and frontend checks still apply.
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
every night at 09:00 UTC. It reports mutation health and surviving mutants.
A clean run of the exact release candidate remains mandatory before publishing,
with `Release Mutation Validation` and an exact source-tree proof.
Neither PR-scoped mutation scores nor cached incremental results can satisfy
the release quality gate.

The former PR baseline-rebuild workflow is no longer necessary for ordinary
PR checks. The GitHub Actions pip installation cache is separate and remains.
