"""Scheduler — the always-on entrypoint. Decides WHEN to run work.

Invoked by launchd (StartInterval ~15 min) or manually. Applies the idle-gate:
heavy work (coding agents) only runs when the machine is genuinely free (user
idle, on AC or healthy battery, not thermally throttled). Light read-only
perception + reports always run. On the spare always-on Mac the idle-gate is
relaxed via SENTINEL_ALWAYS_ON=1.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import subprocess
import sys

logger = logging.getLogger("scheduler")


def _acquire_singleton(name: str):
    """flock-based singleton so overlapping launchd ticks never run concurrent
    cycles (gap P0-4). Returns the held file handle, or None if already running."""
    from config.paths import STATE_DIR
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / f"{name}.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None

IDLE_THRESHOLD_SEC = int(os.getenv("SENTINEL_IDLE_THRESHOLD", "300"))  # 5 min idle = "free"


def user_idle_seconds() -> float:
    """Seconds since last HID (keyboard/mouse) input, via ioreg."""
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5
        ).stdout
        for line in out.splitlines():
            if "HIDIdleTime" in line:
                ns = int(line.split("=")[-1].strip())
                return ns / 1_000_000_000
    except Exception as e:
        logger.debug("idle check failed: %s", e)
    return 0.0


def on_ac_power() -> bool:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        return "AC Power" in out
    except Exception:
        return True


def battery_percent() -> int:
    try:
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        import re
        m = re.search(r"(\d+)%", out)
        return int(m.group(1)) if m else 100
    except Exception:
        return 100


def is_free_for_heavy_work() -> tuple[bool, str]:
    """True if the machine is idle enough to run heavy coding agents."""
    if os.getenv("SENTINEL_ALWAYS_ON", "").lower() in ("1", "true", "yes"):
        return True, "always-on host"
    # The owner's explicit AFK override (/afk in chat): counts as free immediately,
    # bypassing both the HID-idle wait and the battery floor, until it expires.
    try:
        from datetime import datetime, timezone
        from conductor import kv
        fi = kv.get("force_idle") or {}
        if fi.get("until") and datetime.now(timezone.utc) < datetime.fromisoformat(fi["until"]):
            return True, "AFK override (The owner marked idle)"
    except Exception:
        pass
    idle = user_idle_seconds()
    if idle < IDLE_THRESHOLD_SEC:
        return False, f"user active ({idle:.0f}s idle < {IDLE_THRESHOLD_SEC}s)"
    if not on_ac_power() and battery_percent() < 40:
        return False, f"on battery at {battery_percent()}%"
    return True, f"idle {idle:.0f}s, power ok"


async def run_cycle(mode: str = "auto"):
    """Run one conductor cycle. mode: auto | light | heavy | report."""
    from conductor.loop import Conductor
    conductor = await Conductor.build()

    if mode == "report":
        await conductor.run_report()
        return
    if mode == "light":
        await conductor.run_perception_only()
        return

    free, why = is_free_for_heavy_work()
    logger.info("Cycle start (mode=%s): heavy_ok=%s (%s)", mode, free, why)
    if mode == "heavy" or (mode == "auto" and free):
        await conductor.run_full_cycle()
    else:
        # not free: only light read-only perception + surface urgent items
        await conductor.run_perception_only()


def main():
    logging.basicConfig(level=os.getenv("SENTINEL_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("mode", nargs="?", default="auto",
                   choices=["auto", "light", "heavy", "report", "cycle"])
    args = p.parse_args()
    mode = "auto" if args.mode == "cycle" else args.mode
    # report mode is cheap + read-only; everything else takes the singleton lock
    if mode != "report":
        lock = _acquire_singleton("cycle")
        if lock is None:
            logger.info("Another cycle is already running; skipping this tick.")
            sys.exit(0)
    asyncio.run(run_cycle(mode))


if __name__ == "__main__":
    main()
