"""Unit tests for the evidence-gated action ledger.

Run: python -m memory.test_ledger
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory import ledger


def _c(name, got, want):
    ok = got == want
    print(f"  [{'OK' if ok else 'FAIL'}] {name}: {got!r}" + ("" if ok else f" (want {want!r})"))
    return ok


def test_evidence_gate():
    """done REQUIRES a concrete evidence token; empty evidence must NOT reach done."""
    aid = ledger.open_action("send payslip", "send_file", target="p.pdf")
    ledger.close_done(aid, "")                       # refused -> attempted
    row1 = [r for r in ledger.recent(days=1, limit=20) if r["id"] == aid][0]
    ok = _c("empty-evidence stays attempted", row1["status"], "attempted")
    ledger.close_done(aid, "delivered:p.pdf")        # real evidence -> done
    row2 = [r for r in ledger.recent(days=1, limit=20) if r["id"] == aid][0]
    ok &= _c("evidence closes done", row2["status"], "done")
    ok &= _c("evidence stored", row2["evidence"], "delivered:p.pdf")
    return ok


def test_search_and_recent():
    ledger.record("meeting recap TI demo", "read_meeting", evidence="msg-xyz")
    ledger.record("draft to Jordan", "send_email", target="jordan@example.com", status="proposed")
    s = ledger.search("jordan", days=1)
    ok = _c("search finds by target", any("jordan" in (x["target"] or "") for x in s), True)
    r = ledger.recent(days=1, limit=50)
    ok &= _c("recent returns rows", len(r) > 0, True)
    return ok


def test_secret_scrub():
    """A secret-shaped token in the request must be scrubbed at the write boundary."""
    aid = ledger.record("rotate key sk-ant-SECRET12345 for the api", "run_command", evidence="done")
    row = [r for r in ledger.recent(days=1, limit=50) if r["id"] == aid][0]
    ok = _c("secret scrubbed from request", "sk-ant-SECRET12345" not in row["request"], True)
    return ok


def main():
    print("action ledger unit tests")
    results = [("evidence_gate", test_evidence_gate()),
               ("search_and_recent", test_search_and_recent()),
               ("secret_scrub", test_secret_scrub())]
    passed = sum(1 for _, r in results if r)
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
