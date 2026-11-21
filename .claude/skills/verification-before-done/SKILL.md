---
name: verification-before-done
description: >-
  Gate before claiming any coding task is complete, fixed, or done. Use before
  saying "done", "fixed", "ready", or opening a PR — verify with evidence
  instead of asserting success.
---
# Verification Before Done

"Done" is a claim about reality, so back it with evidence, not confidence. Self-assessed success without an external check is exactly the failure mode research flags: strong models over-trust their own output. Before you say the task is complete, pass every gate below — and **show the evidence**, don't assert it.

## The gate (every item must be a real "yes")

1. **Did the tests actually pass?** Run them and SHOW the output — the command and its result. "Tests should pass" and "I believe it works" are not passing tests. If there are no tests for this change, say so explicitly rather than implying coverage.
2. **Does the change do what was asked?** Re-read the original request word for word. Map each thing asked to the change that satisfies it. Catch the "solved a nearby problem, not the stated one" drift.
3. **Any obvious edge cases or neighbors broken?** Consider empty/null inputs, boundaries, error paths, and the callers/siblings of what you touched. Run the impact-scoped suite, not just your one new test.
4. **Did you leave cruft?** No leftover debug logging, commented-out experiments, stray prints, temporary files, or dangling `TODO`/`FIXME` you introduced.
5. **Is the diff minimal and scoped?** Review the full diff. Every hunk should trace to the request. Remove opportunistic refactors and unrelated reformatting — out-of-scope edits are a top measured reliability risk.

## Show, don't assert

When reporting done, include the grounding evidence:

- The test command and its actual output (the green run, or the relevant excerpt).
- The diff, or a tight summary of exactly what changed and where.
- For behavior changes: the before/after you observed, not "this should now work."

## If any gate is a "no"

Do not claim done. Either fix it, or report the specific gap plainly: "tests pass but I didn't cover <edge case>", "the request also asked for X which I haven't done." An honest partial report beats a false completion — a wrong "done" costs far more downstream than a clear "not yet."
