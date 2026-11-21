"""Install/refresh the Hermes-as-main-harness wiring. Idempotent.

Run after editing the doctrine (prompts/mission/harness_scan.md) or the bridge:

    python3 -m bridge.install_harness

What it does (all inside the locked-down `sentinel` Hermes profile):
  1. writes the sentinel MCP bridge into the profile config DIRECTLY (the
     `hermes mcp add` CLI runs interactive prompts that silently default to
     "No" without a TTY — a rerun once wiped the registration that way)
  2. ensures the TFY custom provider + model mapping (provider snapshot must
     be final BEFORE the cron job is created — hermes's drift-guard refuses
     unpinned jobs when provider changed, which silently killed every
     scheduled run on 2026-08-14)
  3. (re)creates the `mission-scan` cron job from the doctrine prompt file
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
PROFILE = "sentinel"
CONFIG = Path.home() / ".hermes" / "profiles" / PROFILE / "config.yaml"

TFY_BASE = __import__("os").environ.get("TFY_BASE_URL", "https://llm-gateway.example.com")
TFY_MODEL = "claude-group/claude-sonnet-5"


def _hermes(*args: str, timeout: int = 90) -> tuple[int, str]:
    p = subprocess.run(["hermes", "-p", PROFILE, *args],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> int:
    cfg = yaml.safe_load(CONFIG.read_text()) or {}

    # 1. MCP bridge — direct config write (prompt-proof, idempotent)
    cfg.setdefault("mcp_servers", {})["sentinel"] = {
        "command": PY,
        "args": ["-m", "bridge.mcp_server"],
        "env": {"PYTHONPATH": str(REPO)},
        "enabled": True,
    }

    # 2. TFY custom provider + model mapping (key comes from GATEWAY_API_KEY,
    #    injected per-spawn by brain/hermes_adapter._child_env — never on disk)
    cfg["custom_providers"] = [{
        "name": "tfy",
        "base_url": TFY_BASE,
        "key_env": "GATEWAY_API_KEY",
        "model": TFY_MODEL,
        "default_model": TFY_MODEL,
    }]
    cfg["model"] = {"provider": "custom:tfy", "default": TFY_MODEL}
    CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))
    print(f"config written: mcp bridge + tfy provider + model {TFY_MODEL}")

    # 3. mission-scan job — remove existing by id (remove-by-name unsupported),
    #    recreate AFTER provider/model are final. 'every 9m' (bare '9m' = once).
    doctrine = (REPO / "prompts" / "mission" / "harness_scan.md").read_text().strip()
    _, listing = _hermes("cron", "list")
    for block in re.split(r"\n\n+", listing):
        if "mission-scan" in block:
            m = re.search(r"([0-9a-f]{12})", block)
            if m:
                _hermes("cron", "remove", m.group(1))
    rc, out = _hermes("cron", "create", "every 9m", doctrine,
                      "--name", "mission-scan", "--deliver", "local")
    print(f"cron create: rc={rc} {out[-120:]}")

    # verify, don't assume
    _, mcp_check = _hermes("mcp", "list")
    _, cron_check = _hermes("cron", "list")
    ok = ("sentinel" in mcp_check and "No MCP servers" not in mcp_check
          and "mission-scan" in cron_check)
    print("VERIFIED" if ok else "VERIFY FAILED — inspect mcp/cron list output")
    return 0 if (rc == 0 and ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
