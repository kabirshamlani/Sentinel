"""Test runner — detect and run a repo's test command in a worktree.

The strongest single reliability technique (2026 research: TDAD −72% regressions):
gate the PR on a real, grounded "tests pass" signal instead of the agent's
self-assessment. Auto-detects the stack; returns pass/fail + output tail.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger("test-runner")


def detect_test_cmd(work_dir: str) -> list[str] | None:
    """Best-effort detection of the repo's test command."""
    p = Path(work_dir)
    if (p / "pyproject.toml").exists() or (p / "pytest.ini").exists() or \
       (p / "setup.cfg").exists() or any(p.glob("test_*.py")) or any(p.glob("**/test_*.py")):
        return ["python3", "-m", "pytest", "-q", "--no-header"]
    if (p / "package.json").exists():
        try:
            import json
            pkg = json.loads((p / "package.json").read_text())
            if "test" in pkg.get("scripts", {}):
                # avoid the npm default that exits 1 with no tests
                if "no test specified" not in pkg["scripts"]["test"]:
                    return ["npm", "test", "--silent"]
        except Exception:
            pass
    if (p / "Makefile").exists():
        txt = (p / "Makefile").read_text(errors="ignore")
        if "\ntest:" in txt or txt.startswith("test:"):
            return ["make", "test"]
    if (p / "go.mod").exists():
        return ["go", "test", "./..."]
    if (p / "Cargo.toml").exists():
        return ["cargo", "test"]
    return None


def _parse_failures(text: str) -> set[str]:
    """Extract failing test identifiers from test output (pytest/go/etc.), so we
    can compare against a baseline and block only NEW failures (regressions)."""
    import re
    fails = set()
    # pytest: "FAILED path::test_name - ..." and "path::test_name FAILED"
    for m in re.finditer(r"^FAILED\s+(\S+)", text, re.MULTILINE):
        fails.add(m.group(1).split(" ")[0])
    for m in re.finditer(r"^(\S+::\S+)\s+FAILED", text, re.MULTILINE):
        fails.add(m.group(1))
    # go test: "--- FAIL: TestName"
    for m in re.finditer(r"--- FAIL:\s+(\S+)", text):
        fails.add(m.group(1))
    return fails


async def regression_check(base_failures: set, work_dir: str, timeout: int = 600) -> dict:
    """Run tests after a fix and compare to a pre-fix baseline. Blocks only on
    NEW failures (a real regression), so a single fix in a repo with other known
    failing tests can still ship. Returns the run dict + {new_failures, fixed}."""
    after = await run_tests(work_dir, timeout=timeout)
    if not after.get("ran"):
        return {**after, "regression": False, "new_failures": [], "fixed": []}
    after_fails = after.get("failures", set())
    new_failures = sorted(after_fails - set(base_failures))
    fixed = sorted(set(base_failures) - after_fails)
    after["regression"] = bool(new_failures)
    after["new_failures"] = new_failures
    after["fixed"] = fixed
    return after


async def run_tests(work_dir: str, timeout: int = 600) -> dict:
    """Run the detected test command. Returns {ran, passed, cmd, output, failures}."""
    cmd = detect_test_cmd(work_dir)
    if not cmd:
        return {"ran": False, "passed": None, "cmd": None,
                "output": "no test command detected"}
    logger.info("Running tests in %s: %s", work_dir, " ".join(cmd))
    try:
        env = {**os.environ}
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=work_dir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = out.decode(errors="ignore")
        passed = proc.returncode == 0
        tail = text[-3000:] if len(text) > 3000 else text
        return {"ran": True, "passed": passed, "cmd": " ".join(cmd),
                "returncode": proc.returncode, "output": tail,
                "failures": _parse_failures(text)}
    except asyncio.TimeoutError:
        return {"ran": True, "passed": False, "cmd": " ".join(cmd),
                "output": f"tests timed out after {timeout}s"}
    except Exception as e:
        return {"ran": True, "passed": False, "cmd": " ".join(cmd),
                "output": f"test run error: {e}"}
