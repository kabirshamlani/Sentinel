# The Coaching Agent — how Sentinel coaches its own army

_Built + live-tested 2026-07-28. One review cycle over 659 real episodes, 19
issues filed, 1 verified fix PR raised in the same session._

## What it is

A deterministic review pipeline (`coaching/`) that closes the loop the
`learning/` package left open: every agent's **real input/output** is graded
against a git-versioned **definition of good**, deviations are **clustered and
root-caused**, issues land on a **kanban**, a heartbeat **fixes** the top one
test-first on a branch, **verifies before/after**, and raises a **PR held for
The owner's approval**. A daily **yesterday-vs-today** doc is derived from the
verdict ledger. Human judgment moves upstream (rubrics + one approval tap);
the loop runs itself.

```
episodes.jsonl ──┐   (tap at brain/claude_runner.run_claude — every agent flows through it)
legacy streams ──┤
                 ▼
   REVIEW  coaching/review.py    mechanical checks per rubric + advisory LLM judge
     │     verdicts.jsonl        every verdict is a ledger row; health → observer circuit
     ▼
    RCA    coaching/rca.py       cluster by (agent, check); fixed-order classes:
     │                           transient → spec-wrong → missing-context → tool-defect
     │                           → architecture-defect → prompt-defect
     ▼
   KANBAN  coaching/kanban.py    beads db (~/.sentinel/coaching/beads — typed deps,
                                 blocker-aware ready queue, Dolt history; JSON fallback)
     │                           + create_issue mirror via the broker when token exists
     ▼
    FIX    coaching/fixer.py     branch cut in Python → code-bot writes FAILING repro
     │                           → fix → repro passes → no new test failures → anti-gaming
     │                           → fresh-context review → broker PR proposal (critical)
     ▼
   REPORT  coaching/report.py    daily yesterday-vs-today, data-derived, skip-if-nothing
```

## The laws it obeys (from BRAINLIFT + research docs 03/07/09/11)

1. **Verify by reading, never self-report** — the failing agent is never asked
   what went wrong; the fixer's "done" is never the gate; every gate is a
   Python-run check.
2. **Boundaries are structural** — the coach has no write path to agents:
   fixes are branches + broker proposals, never in-place edits. If a fix could
   be circumvented by the model ignoring an instruction, it's classified
   architecture, not prompt.
3. **Judges are advisory, never the gate; never self-grading** — LLM judgments
   run on a different model with fresh context, sampled and bounded; merge
   gates are deterministic (repro + regression + anti-gaming + human).
4. **Anti-gaming** — a fix diff may not touch rubrics, checks, or evals
   (unless the issue class is `spec-wrong`, whose whole point is fixing the
   measure — and the human sees that diff).
5. **Cluster, don't whack-a-mole** — one issue per root cause; below a
   rubric's `min_occurrences`, failures stay in the journal (skip-if-nothing).
6. **Skip-if-nothing** — no data → no issues, no report, no noise.

## Definition of good = rubrics

`coaching/rubrics/*.yaml` — 15 rubrics covering the whole army: code-bot,
review-bot, comms-drafter, researcher, meeting_intel, agent-chat, triage,
day, market_brief, proactive, self_improve, and the orphans (planner, tester,
workspace-assistant, synthesizer). Each rubric: role, prose definition of
good, mechanical `episode_checks` (binary, from `coaching/checks.py`), static
`wiring_checks`, anchored `judged_dimensions`, budgets, `min_occurrences`, and
`non_goals` (so "did the spec, spec was wrong" is classifiable). Editing a
rubric is a reviewable git diff — the definition of good is itself versioned.

## Run it

```bash
python3 -m coaching.coach --review --since-hours 24     # review + file issues + report
python3 -m coaching.coach --fix                         # also fix the top kanban issue
python3 -m coaching.coach --board                       # print the kanban
python3 -m coaching.coach --report                      # regenerate the daily doc
python3 -m pytest tests/test_coaching.py -q             # 20 unit tests
```

Daemon schedule (conductor/daemon.py): review daily ~12:05 (one hour after
self_improve so the coach grades that run too); fix cycle every 2h behind the
same idle + budget gate as the work cycle.

## First live cycle — the receipts (2026-07-28)

**Reviewed:** 659 episodes across 14 agent streams (~16 days of history via
the legacy-stream collectors; all future episodes are exact via the new tap).

**Found, among 19 filed issues — every known seeded defect, independently:**

| Issue | Agent | Finding | RCA class |
|---|---|---|---|
| ci-5c480da5 | code-bot | 8× "agent produced no commit on sentinel/issue-None" | tool-defect |
| ci-564b18a7 | code-bot | literal `#None` flowed through the whole pipeline (7×) | tool-defect |
| ci-da8fd3ea | meeting_intel | invoked as `--agent meeting_intel` — agent doesn't exist | tool-defect |
| ci-03096f42 | self_improve | reads `e.get('error')`, journal writes `error_message` → clusters on empty strings | tool-defect |
| ci-0bae6db5 | agent-chat | turns end with "I'll check" promises instead of results | prompt-defect |
| ci-300fe0a8 | day | today.md shipped containing the model's "truncated input" complaint | missing-context |
| ci-428ef534 / ci-1cd3b680 | synthesizer / tester | definition files with no reachable call site | architecture-defect |
| 5× "unobservable" | triage, proactive, researcher, review-bot, day | decisions recorded nowhere a coach can read | architecture-defect (instrumentation) |

**Fixed in the same session (the demo PR):** the fixer picked ci-5c480da5,
cut `sentinel/coach/ci-5c480da5`, and code-bot worked test-first:

- `52c7ca8` — failing repro `tests/coaching_repro/test_ci_5c480da5.py`
  (malformed issue without a `number` reaches the coding agent)
- `da58e4e` — fix: `fix_issue()` validates the issue number up front and
  fails fast, before any worktree/agent spend

Verified (all Python-run, recorded on the approval card `action-07dafc5a`):

```
BEFORE (master @ 25195cc):  1 failed  — fix_issue() invoked the agent on a malformed issue
AFTER  (fix branch):        1 passed
regression gate:            no new failures
anti-gaming:                clean (only verticals/ + its own repro test touched)
fresh review-bot:           LGTM — return shape verified against both call sites
```

Approve with `approve 07dafc5a` (Telegram) — the trusted executor merges the
branch (`git merge --no-ff`); `git revert` is the rollback.

**Daily doc:** `~/.sentinel/coaching/reports/2026-07-28.md` — per-agent
yesterday-vs-today pass rates, agents coached, issues by RCA class, fixes
awaiting approval. Regenerated every cycle, skip-if-nothing.

## Issue enrichment — every issue is a researched ticket (added 2026-07-29)

Before any fix, `coaching/enrich.py` staffs the issue out to the army's own
specialists: **analyst** writes the root-cause dossier (why it matters,
mechanism with file:line, fix plan, testable acceptance criteria, risks);
**researcher** adds external best-practice patterns with real sources
(prompt/architecture classes only, never padded); **planner** produces the
ordered execution plan with owners for multi-step instrumentation work. The
plan lives on the kanban row, feeds the fixer's brief, and is synced into
the GitHub issue body — the board reads like an engineering backlog, not
one-liners. Runs in the daily cycle (2/cycle) + `--enrich N` backfill.

## PM autopilot — the owner's delegated approver (added 2026-07-29)

The owner's standing instruction: a PM agent takes the approve/hold decision on
coach fix PRs on his behalf. `coaching/pm.py` + `.claude/agents/pm.md`,
swept by the daemon every ~15 min.

A pending coach `self_modify` card auto-merges ONLY when ALL hold:
1. **Deterministic gates re-verified at decision time** — branch unchanged
   since its evidence commit, repro test passes at the tip (throwaway
   worktree), diff ≤ 10 files / ≤ 500 lines, merge is conflict-free against
   current HEAD, and the merge is pinned to the exact reviewed SHA (never
   the mutable branch ref);
2. **Independent PM verdict = APPROVE** — opus-class, fresh context,
   read-only, judges the actual diff against the evidence; default HOLD;
3. **3h of TRUE silence, measured from when the owner was notified** of the PM
   verdict — and ANY chat message from him restarts the countdown, so a
   prose objection the parser can't resolve still holds the merge;
4. **Daily cap** (3, durable kv counter) not exhausted;
5. **Autopilot enabled** — it self-disables the first time the owner rejects a
   PM-approved card (trust earned slowly, lost instantly).

**Whitelist, not blacklist** (adversarial review caught `config/paths.py`
repointing the state every rail reads): auto-merge is allowed only for diffs
entirely within `verticals/`, `learning/`, `prompts/`, `docs/`, `memory/`
(minus `gateway.py`), the coach's non-gate modules, `.claude/agents/` (minus
`pm.md`), `.claude/skills/`, and repro tests. Everything else — `config/`,
`conductor/`, `brain/`, `approval/`, `channels/`, hooks, the PM and its
gates — always holds for the owner. A failed merge is aborted cleanly.

**Self-restart:** the daemon records HEAD at boot; when merged code lands
(PM or manual approval), it announces on Telegram and re-execs itself
between jobs. State is externalized, so nothing is lost.

**Chat updates:** PM recommendation + countdown on first review; merge +
undo hint on auto-approval; restart notice. Config in
`~/.sentinel/state/pm_autopilot.json` ({enabled, window_hours, daily_cap}).

## What the coach changed about observability (the tap)

`brain/claude_runner.run_claude` now records every run's clean pre-injection
prompt, final output, model, cost, duration, and `claude_session_id` to
`~/.sentinel/coaching/episodes.jsonl` (scrubbed, capped, never raises). The
old logs kept only `task_prompt[:500]` (usually the injected Learned-Rules
prefix) and `result_length` — which is why 217/221 historical rows could claim
success=true on garbage output. The five "unobservable" issues on the board
are the coach asking for the same treatment for the paths that don't go
through run_claude (triage labels, proactive verdicts, market_brief scores,
outbound broadcasts).
