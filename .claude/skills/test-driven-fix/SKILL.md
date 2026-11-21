---
name: test-driven-fix
description: >-
  Fix a bug or implement a change in a repo that has tests. Use whenever the
  task is to change behavior and a test suite exists — write the failing test
  first, then the minimal fix.
---
# Test-Driven Fix

The strongest single reliability technique measured in 2026 (TDFlow: 94.3% SWE-bench). It works because it gives you an **external, grounded signal** — a red-then-green test — instead of self-judged "looks right."

## Critical caveat first: scope the tests

Naive "do TDD" WITHOUT knowing which tests matter regressed *below* baseline in research. Before anything else:

- Identify WHICH tests are relevant to this change (the impact scope): the module under change, its direct callers, and any suite that already covers this behavior.
- Run those relevant tests first to establish a baseline (what's green now).
- Do not run/chase the entire suite blindly — target the impacted area, then a final broad pass.

## The loop

1. **Reproduce with a FAILING test.** Write a test that captures the exact desired behavior (or the bug). Run it. Confirm it is RED for the expected reason — read the failure, don't assume. A test that passes immediately proves nothing; a test that fails for the wrong reason is worse than none.
2. **Commit the failing test.** Lock the target in place so you cannot silently drift it later. (e.g. `git add <test> && git commit -m "test: failing repro for <bug>"`.)
3. **Implement the minimal fix.** Smallest change that could make the test pass. No refactors, no unrelated cleanup, no speculative extras.
4. **Run tests until green — WITHOUT editing the tests you wrote.** Iterate on the *implementation* only. The committed test is the fixed target; if you find yourself wanting to change it, that's a signal your understanding (or the fix) is wrong, not the test.
5. **Run the broader relevant scope.** Once your new test is green, run the impact-scoped suite to confirm no neighbor regressed.

## Stop conditions (hard cap)

- If a test won't pass after **2-3 genuine attempts**, STOP. Report what you tried, the current failure, and your best hypothesis. Do not keep grinding — retry only buys value on *new grounded signal*, not repetition.
- Escalate rather than hack. A stuck test usually means the requirement, the fix, or the test's assumption is wrong — that needs a decision, not more attempts.

## Never

- **Never delete, skip, weaken, or loosen a test to make it pass.** Changing an assertion to match broken behavior turns a reliability tool into a lie. If a *pre-existing* test is genuinely wrong, say so explicitly and get confirmation — don't silently edit it.
- Never edit the failing test you just wrote to force green (step 4).
- Never claim done on "the code looks correct." Green output from the scoped tests is the evidence; show it.
