"""Triggers — entrypoints that wake the Conductor outside the launchd cadence.

Three ways work enters the system:
  1. launchd StartInterval  -> conductor.scheduler (the steady heartbeat)
  2. inbound channel message -> handled live by channels/router while a full
     cycle is running (approvals, corrections)
  3. explicit CLI kick      -> `python -m conductor.triggers <what>`

This module is the CLI kick + a small helper to run the channel listeners
standalone (e.g. a long-lived `listen` process on the always-on Mac so approvals
and "you made a mistake" messages are handled between cycles).
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logger = logging.getLogger("triggers")


async def listen_forever():
    """Run only the channel listeners (Telegram poll + inbound) indefinitely, so
    approvals/corrections are handled even when no cycle is active. Intended as a
    second launchd agent on the always-on host."""
    from conductor.loop import Conductor
    c = await Conductor.build()
    tasks = await c.start_channel_listeners()
    if not tasks:
        logger.warning("No channels configured; nothing to listen on. Exiting.")
        return
    logger.info("Listening on channels: %s", c.router.configured_names)
    await asyncio.gather(*tasks)


async def kick(target: str):
    from conductor.loop import Conductor
    c = await Conductor.build()
    if target == "github":
        await c._run_github()
    elif target == "inbox":
        await c._run_inbox(heavy=True)
    elif target == "meeting":
        await c._run_meeting()
    elif target == "report":
        await c.run_report()
    else:
        await c.run_full_cycle()


def main():
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("what", nargs="?", default="cycle",
                   choices=["cycle", "github", "inbox", "meeting", "report", "listen"])
    args = p.parse_args()
    if args.what == "listen":
        asyncio.run(listen_forever())
    else:
        asyncio.run(kick(args.what))


if __name__ == "__main__":
    main()
