# AGENTS.md

## Hosted development and public-release isolation

Until the maintainer explicitly authorizes a hosted release:

- \`main\` is the public-only release branch. Do not merge new hosted-service
  runtime code, dormant transport clients, onboarding, settings, translations,
  credentials, parser selection, or feature activation into \`main\`.
- \`integration/hosted-v1\` is the shared Home Assistant hosted-integration branch.
  Start focused hosted feature branches from it and target hosted PRs to it,
  **not** to \`main\`. Verify every hosted PR's base branch before opening it.
- Public-only features and bug fixes still target \`main\`. Periodically bring
  public changes from \`main\` into \`integration/hosted-v1\`, not the reverse.
- Private account, provider, billing, portal, deployment, and infrastructure
  implementation belongs in the designated hosted repositories. Keep the
  Home Assistant integration limited to its public-facing client/interface.
- Existing contract and documentation files already present on \`main\` do not
  authorize additional hosted runtime code or public release notes. New
  hosted-specific contracts and docs should also target the hosted branch
  unless explicitly authorized otherwise.
- Every hosted PR uses the existing exhaustive Codex review, coverage,
  CI, and mutation rules. An integration branch is not a testing exemption.
- Public release candidates must pass the \`Public release isolation\` CI
  check, which builds and scans the real HACS distribution ZIP for recognizable
  hosted-service paths and text. Keep this status check required on \`main\`.
  It is defense in depth, not a substitute for full review; visual assets
  and arbitrarily disguised implementations cannot be verified by text scan.
- Hosted development must not block independent public releases.
  Never merge the complete hosted branch into \`main\` without an explicit
  hosted release authorization, review, and separate release decision.
- If a requested PR target or operation conflicts with these rules, raise the
  conflict instead of silently changing branches or weakening the guard.

## Mutation testing and release gate

- Ordinary PRs use the bounded impact planner and cold base/head execution
  of identical module scopes with a stable `Mutation score` check. Each
  scoped job has a 20-minute hard timeout, including dependency setup. Unknown,
  broad, shared test/resource/dependency changes and new production modules
  use complete comprehensive shards. There is no cached-verdict dependency
  and no automatic single-runner full fallback. The scoped floor is 95.22%,
  with no same-scope regression against the merge-base score. Do not restore
  the retired custom cache-provenance or PR-baseline rebuild system.
- Documentation, frontend or CI-only PRs legitimately have no Python mutations;
  record the no-op explicitly in the check summary. Keep normal tests required.
- Full nightly and release validation remain authoritative for the project-wide
  mutation score; a PR-scoped score only describes its selected modules.
- The clean full mutation suite runs in eight cold shards every night on `main`. The nightly
  results and diagnostics stay in Actions. Nightly failures create or update
  a Mutation Health issue; the next successful run closes it. Successful runs
  without an open failure alert do not create an issue.
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
- The normal 100% coverage gate, minimum mutation score (95.22%) within
  the measured mutation scope, and survivor diagnostics remain enforced for
  mutation-relevant PRs. Do not bypass required checks or merge a PR with failures.
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
