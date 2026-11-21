"""System Tools -- full filesystem and command access for agents.

Provides tools to read any file, list directories, run shell commands,
search files by pattern, and read images for vision models.
"""

from __future__ import annotations

import asyncio
import base64
import glob as glob_module
import logging
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Any

from config.paths import REPO_ROOT

logger = logging.getLogger("system-tools")

DANGEROUS_COMMANDS = [
    "rm -rf", "rm -r /", "mkfs", "dd if=", "format",
    "shutdown", "reboot", "halt", "init 0", "init 6",
    "> /dev/sd", "chmod -R 777 /",
    "git push --force", "git push -f", "rm -rf /", ":(){",
    "npm publish",
]

# Regex patterns for dangerous commands that need pattern matching.
DANGEROUS_COMMAND_PATTERNS = [
    r"aws.* (delete|terminate|rm|remove-)",
    r"pip.* upload",
]

MAX_OUTPUT_BYTES = 100_000
DEFAULT_TIMEOUT = 30
MAX_FILE_CHARS = 50_000
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


AGENT_ARMY_ROOT = REPO_ROOT


def read_any_file(path: str, max_chars: int = MAX_FILE_CHARS) -> dict:
    """Read any text file from the filesystem."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = AGENT_ARMY_ROOT / p
    p = p.resolve()
    if not p.exists():
        return {"error": f"File not found: {path}"}
    if not p.is_file():
        return {"error": f"Not a file: {path}"}

    size = p.stat().st_size
    if size > 5_000_000:
        return {"error": f"File too large: {size} bytes (max 5MB)"}

    try:
        content = p.read_text(encoding="utf-8", errors="replace")[:max_chars]
        return {
            "path": str(p),
            "size": size,
            "content": content,
            "truncated": size > max_chars,
        }
    except Exception as e:
        return {"error": f"Failed to read: {e}"}


def read_image_base64(path: str) -> dict:
    """Read an image file and return base64-encoded content for vision models."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"error": f"Image not found: {path}"}

    suffix = p.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        return {"error": f"Not a supported image format: {suffix}"}

    size = p.stat().st_size
    if size > 20_000_000:
        return {"error": f"Image too large: {size} bytes (max 20MB)"}

    try:
        data = p.read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "path": str(p),
            "size": size,
            "mime_type": mime,
            "base64": b64,
        }
    except Exception as e:
        return {"error": f"Failed to read image: {e}"}


def list_directory(path: str, recursive: bool = False, max_items: int = 200) -> dict:
    """List files and directories at a path."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = AGENT_ARMY_ROOT / p
    p = p.resolve()
    if not p.exists():
        return {"error": f"Directory not found: {path}"}
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}

    items = []
    try:
        if recursive:
            for root, dirs, files in os.walk(str(p)):
                dirs[:] = [d for d in dirs if d not in {
                    "node_modules", ".git", "__pycache__", ".next", ".venv", "venv",
                }]
                for name in files[:50]:
                    fp = Path(root) / name
                    try:
                        st = fp.stat()
                        items.append({
                            "path": str(fp),
                            "name": name,
                            "type": "file",
                            "size": st.st_size,
                            "modified": st.st_mtime,
                        })
                    except OSError:
                        pass
                if len(items) >= max_items:
                    break
        else:
            for entry in sorted(p.iterdir()):
                try:
                    st = entry.stat()
                    items.append({
                        "path": str(entry),
                        "name": entry.name,
                        "type": "dir" if entry.is_dir() else "file",
                        "size": st.st_size if entry.is_file() else 0,
                        "modified": st.st_mtime,
                    })
                except OSError:
                    pass
                if len(items) >= max_items:
                    break

        return {
            "path": str(p),
            "count": len(items),
            "items": items,
        }
    except Exception as e:
        return {"error": f"Failed to list directory: {e}"}


def run_command(command: str, cwd: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Execute a shell command with safety checks."""
    import re

    cmd_lower = command.lower().strip()
    for dangerous in DANGEROUS_COMMANDS:
        if dangerous in cmd_lower:
            return {"error": f"Blocked dangerous command containing '{dangerous}'"}
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, cmd_lower):
            return {"error": f"Blocked dangerous command matching '{pattern}'"}

    work_dir = cwd or str(REPO_ROOT)
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "TERM": "dumb"},
        )
        stdout = result.stdout[:MAX_OUTPUT_BYTES] if result.stdout else ""
        stderr = result.stderr[:MAX_OUTPUT_BYTES] if result.stderr else ""
        return {
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": len(result.stdout or "") > MAX_OUTPUT_BYTES,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"Command execution failed: {e}"}


def search_files(pattern: str, directory: str | None = None) -> dict:
    """Search for files matching a glob pattern."""
    base = directory or str(AGENT_ARMY_ROOT)
    try:
        matches = glob_module.glob(
            os.path.join(base, pattern),
            recursive=True,
        )
        results = []
        for m in sorted(matches)[:100]:
            p = Path(m)
            try:
                st = p.stat()
                results.append({
                    "path": str(p),
                    "name": p.name,
                    "size": st.st_size,
                    "modified": st.st_mtime,
                })
            except OSError:
                pass

        return {
            "pattern": pattern,
            "directory": base,
            "count": len(results),
            "files": results,
        }
    except Exception as e:
        return {"error": f"Search failed: {e}"}


def get_file_info(path: str) -> dict:
    """Get detailed file info -- size, type, permissions, git status."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"error": f"File not found: {path}"}

    st = p.stat()
    info: dict[str, Any] = {
        "path": str(p),
        "name": p.name,
        "type": "dir" if p.is_dir() else "file",
        "size": st.st_size,
        "modified": st.st_mtime,
        "permissions": oct(st.st_mode)[-3:],
        "extension": p.suffix,
    }

    git_dir = p.parent if p.is_file() else p
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", str(p)],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            info["git_status"] = result.stdout.strip() or "clean"
            info["in_git_repo"] = True
        else:
            info["in_git_repo"] = False
    except Exception:
        info["in_git_repo"] = False

    return info


# --------------------------------------------------------------------------- #
# Safe read-only helpers (Tier R). No mutation, no secrets leaked.
# --------------------------------------------------------------------------- #

# Any env var whose NAME contains one of these (case-insensitive) is treated as
# secret and never surfaced by env_summary — neither its value NOR its name.
# Belt-and-suspenders: the spec requires KEY/TOKEN/SECRET/PASSWORD; we add more.
_SECRET_NAME_SUBSTRINGS = (
    "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "PWD",
    "CREDENTIAL", "PRIVATE", "AUTH", "SESSION", "COOKIE", "SIGNATURE",
    "CERT", "SALT", "PASSPHRASE", "APIKEY", "ACCESS", "BEARER",
)


def _is_secret_env_name(name: str) -> bool:
    """True if an env var name looks like it holds a secret."""
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_NAME_SUBSTRINGS)


def which(cmd: str) -> dict:
    """Locate an executable on PATH (like `which`), read-only.

    Returns {"cmd", "path", "found"}; never raises. Only the command NAME is
    accepted — no arguments — so this can't be used to smuggle a shell command.
    """
    import shutil

    name = (cmd or "").strip().split()[0] if (cmd or "").strip() else ""
    if not name:
        return {"cmd": cmd, "path": None, "found": False, "error": "empty command"}
    try:
        resolved = shutil.which(name)
        return {"cmd": name, "path": resolved, "found": resolved is not None}
    except Exception as e:
        return {"cmd": name, "path": None, "found": False, "error": str(e)}


def env_summary() -> list[str]:
    """Return the sorted NAMES of NON-secret environment variables ONLY.

    SECURITY: this function NEVER returns any value, and NEVER returns the name
    of a variable that looks secret (anything whose name contains KEY / TOKEN /
    SECRET / PASSWORD, plus a wider denylist of credential-ish markers). It is a
    read helper for the reporter/health surface to see *what kind* of config is
    present (e.g. "SENTINEL_HOME is set") without ever exposing credentials.

    Returns a plain list[str] of variable names. Never raises.
    """
    names: list[str] = []
    try:
        for name in os.environ:
            if not name:
                continue
            if _is_secret_env_name(name):
                continue
            names.append(name)
    except Exception as e:  # extremely unlikely; stay defensive
        logger.warning("env_summary failed: %s", e)
        return []
    return sorted(names)


def disk_and_mem() -> dict:
    """Return a disk + memory summary for the reporter / health checks (read-only).

    Runs `df -h` and (on macOS) `vm_stat` / (on Linux) `free -h`, capturing their
    output. Purely observational; no mutation. Never raises — on any failure the
    corresponding field carries an "error"/None.
    """
    import platform

    result: dict[str, Any] = {"platform": platform.system()}

    # Disk usage — df -h is portable across macOS/Linux.
    try:
        df = subprocess.run(
            ["df", "-h"], capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
        )
        result["disk"] = df.stdout.strip()[:MAX_OUTPUT_BYTES] if df.returncode == 0 else None
        if df.returncode != 0:
            result["disk_error"] = df.stderr.strip()[:500]
    except Exception as e:
        result["disk"] = None
        result["disk_error"] = str(e)

    # Memory — vm_stat on macOS (Darwin), free -h on Linux.
    mem_cmd = ["vm_stat"] if platform.system() == "Darwin" else ["free", "-h"]
    try:
        mem = subprocess.run(
            mem_cmd, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
        )
        result["memory"] = mem.stdout.strip()[:MAX_OUTPUT_BYTES] if mem.returncode == 0 else None
        result["memory_tool"] = mem_cmd[0]
        if mem.returncode != 0:
            result["memory_error"] = mem.stderr.strip()[:500]
    except FileNotFoundError:
        result["memory"] = None
        result["memory_error"] = f"{mem_cmd[0]} not available"
    except Exception as e:
        result["memory"] = None
        result["memory_error"] = str(e)

    return result
