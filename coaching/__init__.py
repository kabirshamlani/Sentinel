"""Coaching — the agent that coaches the agents.

A deterministic review pipeline (LLM calls only where a mechanical check can't
reach) that closes the loop the learning/ package leaves open:

  EPISODES  -> every agent run's real input/output, captured at the one choke
               point (brain/claude_runner.run_claude) all agents flow through.
  RUBRICS   -> a git-versioned "definition of good" per agent
               (coaching/rubrics/*.yaml): binary checks first, anchored LLM
               judgments only for what no script can verify.
  REVIEW    -> grade episodes against rubrics; verdicts are ledger rows, and
               per-agent health feeds the existing observer degradation circuit.
  RCA       -> cluster failures (never patch test-by-test) and classify each
               issue with a fixed-order decision tree:
               transient -> spec-wrong -> missing-context -> tool-defect
               -> architecture-defect -> prompt-defect.
  KANBAN    -> issues land on a beads database (bd — typed dependency
               links, blocker-aware ready queue; ~/.sentinel/coaching/beads,
               JSON fallback), mirrored to GitHub via the approval broker
               when credentials exist.
  FIX       -> the 2-hourly cycle picks an issue: repro case first, fix branch
               cut deterministically in Python, code-bot writes the fix,
               before/after replay + test gate + fresh-context review, then a
               broker proposal (critical, held for the owner). Never auto-applied.
  REPORT    -> a daily yesterday-vs-today doc derived from verdict rows, never
               self-narrated. Skip-if-nothing is sacred.

Architecture decisions (and the alternatives rejected):
  * Own package, NOT inside learning/ — the coach grades the learning loop
    itself; nesting it there would let the subsystem grade itself structurally.
  * Deterministic pipeline, NOT one big coach-LLM — verify by reading, never by
    self-report (knowledge base #6); LLM judges are advisory and never the gate.
  * Local kanban as source of truth, GitHub as mirror — gh is unauthenticated
    today; the core loop must not have a network dependency.
  * The coach never edits agents in place — every fix is a branch + broker
    proposal; git is the rollback substrate (knowledge base laws).
"""
