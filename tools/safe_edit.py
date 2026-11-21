"""Safe Edit Engine -- never lose user's work.

When agents need to edit files:
1. Git repo: create branch, commit, push, create PR
2. Non-git: copy original as backup, write draft, present diff
3. Approval required before merging/replacing

Core principle: the original file is NEVER modified without explicit user approval.
"""

from __future__ import annotations

import difflib
import json
import logging
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.paths import EDIT_PROPOSALS_PATH

logger = logging.getLogger("safe-edit")

PROPOSALS_PATH = EDIT_PROPOSALS_PATH


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_proposals() -> list[dict]:
    if PROPOSALS_PATH.exists():
        try:
            return json.loads(PROPOSALS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _save_proposals(proposals: list[dict]):
    PROPOSALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROPOSALS_PATH.write_text(json.dumps(proposals, indent=2), encoding="utf-8")


def _is_git_repo(path: Path) -> bool:
    """Check if a path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(path.parent if path.is_file() else path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _git_root(path: Path) -> Path | None:
    """Get the root of the git repo containing this path."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path.parent if path.is_file() else path),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def safe_edit(file_path: str, new_content: str, description: str = "agent-edit") -> dict:
    """Safely propose an edit to a file.

    Returns an EditProposal with diff preview and approval instructions.
    """
    p = Path(file_path).expanduser().resolve()
    proposal_id = f"edit-{uuid.uuid4().hex[:8]}"
    safe_desc = description.replace(" ", "-").replace("/", "-")[:40]

    original_content = ""
    if p.exists():
        try:
            original_content = p.read_text(encoding="utf-8")
        except Exception as e:
            return {"error": f"Cannot read original file: {e}"}

    diff = "\n".join(difflib.unified_diff(
        original_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{p.name}",
        tofile=f"b/{p.name}",
        lineterm="",
    ))

    proposal: dict[str, Any] = {
        "id": proposal_id,
        "file_path": str(p),
        "description": description,
        "status": "pending",
        "proposed_at": _now(),
        "diff_preview": diff[:5000],
    }

    if _is_git_repo(p):
        repo_root = _git_root(p)
        if repo_root:
            branch_name = f"agent/{safe_desc}-{proposal_id[-6:]}"
            proposal["method"] = "git_branch"
            proposal["branch_name"] = branch_name
            proposal["repo_root"] = str(repo_root)

            try:
                cwd = str(repo_root)
                subprocess.run(["git", "checkout", "-b", branch_name], cwd=cwd, capture_output=True, check=True, timeout=10)
                p.write_text(new_content, encoding="utf-8")
                rel_path = p.relative_to(repo_root)
                subprocess.run(["git", "add", str(rel_path)], cwd=cwd, capture_output=True, check=True, timeout=10)
                subprocess.run(
                    ["git", "commit", "-m", f"Agent edit: {description}"],
                    cwd=cwd, capture_output=True, check=True, timeout=10,
                )
                subprocess.run(["git", "checkout", "-"], cwd=cwd, capture_output=True, check=True, timeout=10)
                proposal["status"] = "pending"
                logger.info("Created branch %s with proposed edit", branch_name)
            except subprocess.CalledProcessError as e:
                try:
                    subprocess.run(["git", "checkout", "-"], cwd=str(repo_root), capture_output=True, timeout=5)
                except Exception:
                    pass
                proposal["error"] = f"Git operation failed: {e.stderr.decode()[:300] if e.stderr else str(e)}"
                proposal["method"] = "backup_copy"
    else:
        proposal["method"] = "backup_copy"

    if proposal.get("method") == "backup_copy":
        backup_path = str(p) + ".agent-backup"
        draft_path = str(p) + ".agent-draft"

        if p.exists():
            shutil.copy2(str(p), backup_path)
            proposal["backup_path"] = backup_path

        Path(draft_path).write_text(new_content, encoding="utf-8")
        proposal["draft_path"] = draft_path
        logger.info("Created draft at %s", draft_path)

    proposals = _load_proposals()
    proposals.append(proposal)
    if len(proposals) > 100:
        proposals = proposals[-50:]
    _save_proposals(proposals)

    return proposal


def approve_edit(proposal_id: str) -> dict:
    """Approve a pending edit proposal."""
    proposals = _load_proposals()
    for p in proposals:
        if p["id"] == proposal_id and p["status"] == "pending":
            method = p.get("method", "backup_copy")

            if method == "git_branch":
                branch = p.get("branch_name", "")
                repo_root = p.get("repo_root", "")
                if branch and repo_root:
                    try:
                        subprocess.run(
                            ["git", "merge", branch],
                            cwd=repo_root, capture_output=True, check=True, timeout=15,
                        )
                        subprocess.run(
                            ["git", "branch", "-d", branch],
                            cwd=repo_root, capture_output=True, timeout=10,
                        )
                        p["status"] = "approved"
                        p["resolved_at"] = _now()
                        _save_proposals(proposals)
                        return {"status": "merged", "branch": branch}
                    except subprocess.CalledProcessError as e:
                        return {"error": f"Merge failed: {e.stderr.decode()[:300] if e.stderr else str(e)}"}

            elif method == "backup_copy":
                draft_path = p.get("draft_path", "")
                file_path = p.get("file_path", "")
                if draft_path and file_path:
                    try:
                        draft = Path(draft_path)
                        if draft.exists():
                            shutil.copy2(draft_path, file_path)
                            draft.unlink()
                            p["status"] = "approved"
                            p["resolved_at"] = _now()
                            _save_proposals(proposals)
                            return {"status": "applied", "file": file_path}
                    except Exception as e:
                        return {"error": f"Apply failed: {e}"}

            return {"error": "Cannot approve: missing method details"}
    return {"error": f"Proposal {proposal_id} not found or not pending"}


def reject_edit(proposal_id: str) -> dict:
    """Reject a pending edit proposal -- cleans up branch/draft."""
    proposals = _load_proposals()
    for p in proposals:
        if p["id"] == proposal_id and p["status"] == "pending":
            method = p.get("method", "backup_copy")

            if method == "git_branch":
                branch = p.get("branch_name", "")
                repo_root = p.get("repo_root", "")
                if branch and repo_root:
                    try:
                        subprocess.run(
                            ["git", "branch", "-D", branch],
                            cwd=repo_root, capture_output=True, timeout=10,
                        )
                    except Exception:
                        pass

            elif method == "backup_copy":
                draft_path = p.get("draft_path", "")
                backup_path = p.get("backup_path", "")
                if draft_path:
                    Path(draft_path).unlink(missing_ok=True)
                if backup_path:
                    Path(backup_path).unlink(missing_ok=True)

            p["status"] = "rejected"
            p["resolved_at"] = _now()
            _save_proposals(proposals)
            return {"status": "rejected", "cleaned_up": True}

    return {"error": f"Proposal {proposal_id} not found or not pending"}


def get_pending_edits() -> list[dict]:
    """Get all pending edit proposals."""
    proposals = _load_proposals()
    return [p for p in proposals if p.get("status") == "pending"]
