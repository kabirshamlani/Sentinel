---
name: systematic-debugging
description: >-
  Debug an error, test failure, crash, or unexpected behavior. Use when
  something is broken and you need to find the root cause instead of
  guess-editing.
---
# Systematic Debugging

Debugging fails when it becomes guessing. Every step here is grounded in **observed output**, not speculation. Research is blunt: pure self-reflection ("let me reconsider…") is net-negative on strong models — progress comes only from *external signal* (the actual error, a probe's output, a bisect result).

## Two rules that govern everything

- **Read the actual error and stack trace. Don't pattern-match.** The real message names the file, line, and value. Skimming it and jumping to a "usual suspect" is the #1 way debugging goes sideways.
- **Change ONE thing at a time.** Multiple simultaneous edits destroy your ability to attribute cause. One change, one observation, repeat.

## The loop

1. **Reproduce reliably + capture the exact error.** Get a command that fails the same way every time. Copy the full error, stack, and the values involved. If you can't reproduce it, you can't confirm you fixed it — invest here first.
2. **Form ONE hypothesis + a cheap test for it.** State plainly what you believe is happening and the smallest observation that would confirm or kill it. A hypothesis you can't cheaply test is not yet useful — sharpen it.
3. **Add a targeted probe — don't guess-edit.** Insert a log line, an assert, a breakpoint, or a print at the exact spot your hypothesis predicts. Let the program tell you the real state (actual values, actual branch taken). Do not "fix" code you haven't yet confirmed is wrong.
4. **Bisect to localize.** When the fault could be in many places, halve the search space: `git bisect` to find the introducing commit, or comment out / disable regions to isolate the failing unit. Localize before you theorize about mechanism.
5. **Confirm root cause before fixing.** You should be able to say: "the failure happens because X, shown by observation Y." If you can't point to the observation, you don't have the root cause yet — you have another guess. Go back to step 2.
6. **Verify the fix.** Re-run the original repro: it must now pass. Then run the neighbors — the callers and sibling behavior the change could touch — to confirm you didn't trade one bug for another. Remove your probes.

## Anti-patterns to catch yourself doing

- Editing based on a hunch without a probe confirming the hypothesis.
- Stacking several changes, then losing track of which one mattered.
- "Reasoning harder" in place of adding an observation — introspection is not evidence.
- Declaring it fixed without re-running the exact repro from step 1.
- Leaving debug probes/logging in the final change.
