"""Run the scenario bank in parallel, grade each, write a JSON report.

Usage: python -m tests.run_scenarios [N] [concurrency]
Reads run live; mutations are captured (dry-run) by the harness. Prints a summary and
writes tests/scenario_report.json with per-scenario pass/fail + reasons for RCA.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.scenario_harness import run_scenario
from tests.scenarios import expand_to_target


async def _run_one(sem, name, cat, prompt, expect, tr):
    async with sem:
        r = await run_scenario(name, prompt, tr)
        passed, fails = r.grade(expect)
        return {"name": name, "category": cat, "prompt": prompt, "passed": passed,
                "fails": fails, "reply": r.reply[:500], "latency_s": r.latency_s,
                "proposals": [p.get("action_type") for p in r.proposals],
                "error": r.error[:200]}


async def main(n: int, concurrency: int):
    scenarios = expand_to_target(n)
    sem = asyncio.Semaphore(concurrency)
    print(f"running {len(scenarios)} scenarios, concurrency={concurrency}…", flush=True)
    t0 = time.monotonic()
    tasks = [_run_one(sem, nm, cat, p, e, tr) for (nm, cat, p, e, tr) in scenarios]
    results = []
    done = 0
    for coro in asyncio.as_completed(tasks):
        res = await coro
        results.append(res)
        done += 1
        mark = "✓" if res["passed"] else "✗"
        print(f"  [{done}/{len(scenarios)}] {mark} {res['name']} ({res['latency_s']}s)"
              + ("" if res["passed"] else f"  <- {res['fails']}"), flush=True)

    elapsed = round(time.monotonic() - t0, 1)
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    # category breakdown
    from collections import Counter
    cat_pass = Counter(r["category"] for r in passed)
    cat_all = Counter(r["category"] for r in results)

    report = {"total": len(results), "passed": len(passed), "failed": len(failed),
              "pass_rate": round(len(passed) / len(results), 3) if results else 0,
              "elapsed_s": elapsed,
              "by_category": {c: f"{cat_pass[c]}/{cat_all[c]}" for c in sorted(cat_all)},
              "failures": [{"name": r["name"], "category": r["category"], "prompt": r["prompt"],
                            "fails": r["fails"], "reply": r["reply"][:300], "error": r["error"]}
                           for r in failed],
              "results": results}
    out = ROOT / "tests" / "scenario_report.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\n{'='*50}")
    print(f"PASS {len(passed)}/{len(results)} ({report['pass_rate']:.0%}) in {elapsed}s")
    print("by category:", report["by_category"])
    if failed:
        print(f"\n{len(failed)} FAILURES (see scenario_report.json):")
        for r in failed[:25]:
            print(f"  ✗ {r['name']}: {r['fails']}")
    print(f"\nreport: {out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    C = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    sys.exit(asyncio.run(main(N, C)))
