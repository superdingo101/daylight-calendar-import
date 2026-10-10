# AGENTS.md

## Mutation testing and release gate

- Every pull request runs **incremental** mutation testing (stable `Mutation score` check).
  It restores a cache when available, but invalidates cached results on changes
  to existing tests, shared fixtures, dependencies, or behavior-changing
  configuration. Release-only version-number changes in manifest.json and
  pyproject.toml do not invalidate mutation results.
  If no safe PR/recovery/main cache exists, **fail quickly** with instructions
  to run **Rebuild PR Mutation Cache** from main with that PR number; never
  launch a 45-minute full run on a routine PR. The explicit rebuild checks
  the PR's exact merge tree, runs the full mutation suite once, publishes a
  PR-specific cache. After it succeeds, manually rerun the current PR's
  failed required `Mutation score` check to validate the saved cache.
  Never automatically rerun an earlier workflow run: GitHub would retain
  its old synthetic merge SHA if the PR base changed.
- Do not delete mutation state in an ordinary PR job: incremental mutmut
  invalidates changed production functions itself.
- The clean full mutation suite runs every night on `main`. The nightly
  result, diagnostics, and a persistent Mutation Health issue are visible.
- The **Update Integration Version** action creates the release-preparation PR,
  runs normal CI through explicit workflow dispatch, and calls the clean
  mutation workflow against the release candidate's exact SHA.
- **Publish HACS Release** fails closed unless a successful Update Integration
  Version run (or a manually dispatched clean revalidation on main) has an
  unexpired clean mutation artifact for the identical Git source tree and
  release version. Matching only a score, PR status, or old SHA
  is insufficient.
- If a release-preparation branch changes after testing, the earlier proof
  cannot authorize publishing unless the source trees still match. Repeat
  release preparation/clean validation when the proof is stale.
- The legacy `mutation-ready` label is no longer a trigger; do not apply it.
- The normal 100% coverage gate, minimum mutation score (95.22%), and complete
  survivor reports remain enforced. Do not bypass required checks or merge a
  PR with failed incremental mutation tests.
- Keep tests for CI scripts importable under mutmut's isolated `mutants/`
  directory. The `scripts/` helper directory is configured in `also_copy`.

See [mutation CI documentation](docs/mutation-ci.md).

## Code review instructions

When reviewing a pull request or any proposed code change in this repository, perform a **complete, exhaustive review of the entire change set before returning your response**.

Do not stop reviewing after finding the first bug, the first few bugs, or the first high-severity issue. Continue through **every changed file, every relevant diff hunk, and any directly affected surrounding code** until the full review pass is complete.

Your review must identify and report **all issues you can substantiate in a single response**, including:

- correctness bugs and regressions
- edge cases and failure modes
- security vulnerabilities and unsafe behavior
- data-loss, corruption, privacy, or reliability risks
- concurrency, lifecycle, state-management, and cleanup problems
- API, schema, compatibility, or migration issues
- error-handling and observability problems
- missing or inadequate tests for changed behavior
- performance problems when materially relevant
- repository style, lint, typing, documentation, and maintainability violations

Before responding:

1. Inspect every changed file.
2. Trace important changed code paths into directly related existing code when necessary to validate behavior.
3. Consider interactions between changed files, not just each file in isolation.
4. Check tests and CI-related changes for blind spots, false confidence, or missing coverage.
5. Re-scan the full diff after identifying issues to make sure no additional findings were skipped.
6. Only then produce the review response.

### Review response expectations

- Return the **complete set of findings from that review pass in one response**.
- Do **not** intentionally limit the response to one or two findings.
- Do **not** return early merely because a serious issue has already been found.
- Do **not** defer additional known findings to a later review pass.
- Keep findings specific, actionable, and tied to concrete code.
- Avoid speculative complaints that cannot be supported by the code.
- If no substantive issues remain after the exhaustive pass, say so explicitly.

The goal is that a single review pass should surface as many real issues as possible, rather than requiring repeated review cycles where each pass reveals only one or two additional problems.
