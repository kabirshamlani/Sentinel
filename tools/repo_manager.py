"""Repo manager — clone/refresh repos and hand out isolated git worktrees.

The coding agent needs a clean, up-to-date checkout to work in, and parallel
tasks must not stomp each other. This owns:
  * a local cache of bare-ish clones under SENTINEL_HOME/repos/<owner>__<repo>
  * `worktree(owner, repo, branch)` -> a fresh, isolated working directory on a
    new branch off origin/main, so N issues can be worked concurrently
  * cleanup of finished worktrees

Everything here uses plain git via subprocess (the broker/conductor are trusted;
the coding agent runs INSIDE the worktree and is still denied push by the
deny-guard, so only the broker pushes).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from config.paths import SENTINEL_HOME

logger = logging.getLogger("repo-manager")

REPOS_DIR = SENTINEL_HOME / "repos"
WORKTREES_DIR = SENTINEL_HOME / "worktrees"

# serialize clone/worktree ops per repo so parallel tasks don't collide on the
# shared cache clone (git clone/fetch/worktree-add on one repo are not concurrent-safe)
_repo_locks: dict[str, asyncio.Lock] = {}


def _lock(owner: str, repo: str) -> asyncio.Lock:
    key = _slug(owner, repo)
    if key not in _repo_locks:
        _repo_locks[key] = asyncio.Lock()
    return _repo_locks[key]


def _slug(owner: str, repo: str) -> str:
    return f"{owner}__{repo}"


def _clone_dir(owner: str, repo: str) -> Path:
    return REPOS_DIR / _slug(owner, repo)


async def _git(*args, cwd: str | None = None, timeout: int = 180) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return proc.returncode, out.decode().strip(), err.decode().strip()


def _auth_url(owner: str, repo: str) -> str:
    """Token-in-URL clone URL. The token lives only in the local cache's remote,
    never committed; prefer this over embedding in a tracked file."""
    import os
    token = os.getenv("GITHUB_TOKEN", "")
    if token:
        return f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
    return f"https://github.com/{owner}/{repo}.git"


async def ensure_clone(owner: str, repo: str, default_branch: str = "main") -> Path:
    """Clone the repo into the local cache if missing, else fetch latest.

    Serialized per-repo so concurrent tasks don't race the shared clone.
    """
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    cdir = _clone_dir(owner, repo)
    async with _lock(owner, repo):
        if (cdir / ".git").exists():
            await _git("fetch", "origin", "--prune", cwd=str(cdir))
            logger.info("Fetched %s/%s", owner, repo)
        elif cdir.exists() and not (cdir / ".git").exists():
            # a half-created dir from a prior failed clone — remove and retry
            shutil.rmtree(cdir, ignore_errors=True)
            rc, _, err = await _git("clone", _auth_url(owner, repo), str(cdir))
            if rc != 0:
                raise RuntimeError(f"clone failed for {owner}/{repo}: {err[:200]}")
        else:
            rc, _, err = await _git("clone", _auth_url(owner, repo), str(cdir))
            if rc != 0:
                raise RuntimeError(f"clone failed for {owner}/{repo}: {err[:200]}")
            logger.info("Cloned %s/%s -> %s", owner, repo, cdir)
    return cdir


async def default_branch(owner: str, repo: str) -> str:
    cdir = _clone_dir(owner, repo)
    rc, out, _ = await _git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=str(cdir))
    if rc == 0 and "/" in out:
        return out.rsplit("/", 1)[-1]
    return "main"


async def worktree(owner: str, repo: str, branch: str) -> str:
    """Create an isolated worktree on a fresh `branch` off origin/<default>.

    Returns the worktree path. Safe for concurrent tasks — each gets its own dir.
    """
    cdir = await ensure_clone(owner, repo)
    base = await default_branch(owner, repo)
    WORKTREES_DIR.mkdir(parents=True, exist_ok=True)
    wt = WORKTREES_DIR / f"{_slug(owner, repo)}__{branch.replace('/', '-')}"

    # worktree add / branch -D on the shared clone must be serialized per-repo
    async with _lock(owner, repo):
        if wt.exists():
            await _git("worktree", "remove", "--force", str(wt), cwd=str(cdir))
            if wt.exists():
                shutil.rmtree(wt, ignore_errors=True)
        await _git("worktree", "prune", cwd=str(cdir))
        await _git("branch", "-D", branch, cwd=str(cdir))
        rc, _, err = await _git("worktree", "add", "-b", branch, str(wt),
                                f"origin/{base}", cwd=str(cdir))
        if rc != 0:
            raise RuntimeError(f"worktree add failed: {err[:200]}")
    logger.info("Worktree %s on %s (off origin/%s)", wt, branch, base)
    return str(wt)


async def push_branch(owner: str, repo: str, branch: str, worktree_path: str) -> tuple[int, str]:
    """Push a worktree's branch to origin (broker-only; the agent is denied push)."""
    rc, out, err = await _git("push", "-u", "origin", branch, cwd=worktree_path, timeout=120)
    return rc, (err or out)


async def cleanup(owner: str, repo: str, worktree_path: str) -> None:
    cdir = _clone_dir(owner, repo)
    await _git("worktree", "remove", "--force", worktree_path, cwd=str(cdir))
    if Path(worktree_path).exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
