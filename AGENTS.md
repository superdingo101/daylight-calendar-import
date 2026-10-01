# AGENTS.md

## Pull request completion workflow

For same-repository pull requests, the full mutation suite is a **final pre-merge gate**, not an iterative-development check.

- Do **not** apply the `mutation-ready` label while implementation, normal CI, or review work is still in progress.
- First finish the implementation, get the normal fast CI checks green, and resolve all known review/Codex findings.
- Only when the pull request is otherwise ready to merge, apply the `mutation-ready` label. This triggers the full `Mutation score` workflow.
- If any commit is pushed after `mutation-ready` is applied, the label may remain in place; mutation testing automatically reruns against the new pull request head.
- Never treat an older successful mutation run as sufficient after the pull request head changes.
- Do **not** merge until `Mutation score` passes on the current pull request head.
- Fork/external pull requests are intentionally not eligible for this label-driven final mutation gate; do not try to work around that restriction by using a privileged workflow.

When acting as an implementation or review agent, do not apply `mutation-ready` merely to check progress. Apply it only at the final handoff to merge readiness.

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
