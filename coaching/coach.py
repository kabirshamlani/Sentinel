"""Coach — orchestration entrypoint + CLI.

One cycle = collect episodes → grade against rubrics → cluster + RCA →
file kanban issues (broker-mirrored to GitHub when possible) → optionally run
one fix → write the daily report. Deterministic spine; every mutation gated.

Daemon wiring (conductor/daemon.py): daily review at ~12:05, fix cycle rides
the 2h idle+budget gate. CLI for manual/demo runs:

    python3 -m coaching.coach --review --since-hours 400   # backfill review
    python3 -m coaching.coach --fix --dry-run              # fix top issue, no proposal
    python3 -m coaching.coach --report
    python3 -m coaching.coach --board
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger("coach")


async def run_coaching(broker=None, router=None, *, since_hours: float = 24,
                       judge: bool = True, fix: bool = False,
                       dry_run: bool = False, mirror: bool = True) -> dict:
    """The full coaching cycle. Safe to run without a broker (review-only)."""
    from coaching import review as review_mod, rca, kanban, report
    from coaching.rubrics import load_rubrics

    rubrics = load_rubrics()

    # close the loop from the last cycle first: approved fixes verified-merged
    # -> done; rejected proposals -> rejected (evidence-gated, never a flag)
    reconciled = kanban.reconcile()

    rev = await review_mod.run_review(since_hours=since_hours, judge=judge)

    issues, transients = rca.build_issues(rev["verdicts"], rev["static_findings"], rubrics)
    rca.log_transients(transients)

    filed, duplicates = [], 0
    for issue in issues:
        res = kanban.file_issue(issue)
        if res["status"] == "filed":
            filed.append(res["id"])
            if mirror and broker is not None:
                await kanban.mirror_to_github(kanban.get(res["id"]) or issue, broker)
        else:
            duplicates += 1

    # enrich: every issue becomes a researched ticket (analyst dossier +
    # external patterns + execution plan) before any fix — bounded per cycle
    enriched = None
    if broker is not None:
        try:
            from coaching import enrich as enrich_mod
            enriched = await enrich_mod.enrich_backlog(broker, max_issues=2)
        except Exception as e:
            logger.warning("enrichment failed: %s", e)

    fix_result = None
    if fix and broker is not None:
        from coaching import fixer
        fix_result = await fixer.run_fix_cycle(broker, max_issues=1, dry_run=dry_run)

    # keep the GitHub kanban view current (local board stays source of truth)
    board_sync = None
    if mirror and broker is not None:
        try:
            from coaching import github_board
            board_sync = await github_board.sync(broker)
        except Exception as e:
            logger.warning("github board sync failed: %s", e)

    rep = report.write_daily()

    summary = {
        "episodes_reviewed": rev["episodes"],
        "episode_counts": rev["episode_counts"],
        "verdicts": len(rev["verdicts"]),
        "failing_verdicts": sum(1 for v in rev["verdicts"] if not v.get("ok")),
        "static_findings": len(rev["static_findings"]),
        "issues_filed": filed,
        "issues_duplicate": duplicates,
        "transients": len(transients),
        "health": rev["health"],
        "fix": fix_result,
        "reconciled": reconciled,
        "enriched": enriched,
        "github_board": board_sync,
        "report": rep,
        "board": kanban.board_summary(),
    }
    try:
        from conductor.loop import log_work
        log_work("coach", f"reviewed {rev['episodes']} episodes across "
                          f"{len(rev['episode_counts'])} agents; filed {len(filed)} issues "
                          f"({duplicates} dup)", "ok",
                 {k: v for k, v in summary.items() if k not in ("health",)})
    except Exception:
        pass
    return summary


def _main() -> None:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description="Sentinel coaching agent")
    p.add_argument("--review", action="store_true", help="run the review cycle")
    p.add_argument("--fix", action="store_true", help="also fix the top kanban issue")
    p.add_argument("--enrich", type=int, default=0, metavar="N",
                   help="backfill researched plans for N unplanned backlog issues")
    p.add_argument("--report", action="store_true", help="write the daily report only")
    p.add_argument("--board", action="store_true", help="print the kanban board")
    p.add_argument("--since-hours", type=float, default=24)
    p.add_argument("--no-judge", action="store_true", help="skip LLM judge (mechanical only)")
    p.add_argument("--no-mirror", action="store_true", help="skip GitHub issue mirroring")
    p.add_argument("--dry-run", action="store_true", help="fixer: verify but do not propose")
    args = p.parse_args()

    if args.board:
        from coaching import kanban
        print(json.dumps(kanban.board_summary(), indent=1))
        for i in kanban.issues():
            print(f"  [{i.get('status'):10s}] {i.get('id')} {i.get('severity'):6s} "
                  f"{i.get('agent','?'):16s} {i.get('title','')[:80]}")
        return
    if args.report and not (args.review or args.fix):
        from coaching import report
        print(json.dumps(report.write_daily(), indent=1))
        return
    if args.enrich and not (args.review or args.fix):
        async def _enrich():
            from coaching import enrich as enrich_mod
            from approval.broker import ApprovalBroker
            return await enrich_mod.enrich_backlog(ApprovalBroker(), max_issues=args.enrich)
        print(json.dumps(asyncio.run(_enrich()), indent=1))
        return

    async def _run():
        broker = None
        if args.fix or not args.no_mirror:
            try:
                from approval.broker import ApprovalBroker
                broker = ApprovalBroker()
            except Exception as e:
                logger.warning("broker unavailable (%s) — review-only", e)
        return await run_coaching(
            broker, since_hours=args.since_hours, judge=not args.no_judge,
            fix=args.fix, dry_run=args.dry_run, mirror=not args.no_mirror)

    out = asyncio.run(_run())
    out.pop("health", None)
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    _main()
