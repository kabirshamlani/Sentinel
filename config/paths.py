"""Single source of truth for all Sentinel storage paths.

Every runtime artifact (learning journals, memory vault, job queue, approval
state, work logs, evals) lives under SENTINEL_HOME. Override with the
SENTINEL_HOME env var (used when deploying to the spare Mac).
"""

from __future__ import annotations

import os
from pathlib import Path

SENTINEL_HOME = Path(os.getenv("SENTINEL_HOME", str(Path.home() / ".sentinel")))

# Learning stack
ERROR_JOURNAL_PATH = SENTINEL_HOME / "error_journal.jsonl"
SKILLBOOK_PATH = SENTINEL_HOME / "skillbook.json"
LEARNING_LOG_PATH = SENTINEL_HOME / "learning_log.jsonl"

# Approval / actions
ACTION_QUEUE_PATH = SENTINEL_HOME / "action_queue.json"
REJECTION_LOG_PATH = SENTINEL_HOME / "rejection_log.jsonl"
COMPLETION_LOG_PATH = SENTINEL_HOME / "completion_log.jsonl"
PROJECT_SCOPE_PATH = SENTINEL_HOME / "project_scope.json"

# Jobs & work
JOBS_PATH = SENTINEL_HOME / "jobs.json"
WORK_LOG_PATH = SENTINEL_HOME / "autonomous_work_log.jsonl"

# Memory vault
MEMORY_ROOT = SENTINEL_HOME / "memory"

# Edits / proposals
EDIT_PROPOSALS_PATH = SENTINEL_HOME / "edit_proposals.json"

# Evals & evolution
EVALS_DIR = SENTINEL_HOME / "evals"

# Runtime state
STATE_DIR = SENTINEL_HOME / "state"
SCREENSHOTS_DIR = SENTINEL_HOME / "screenshots"

# The sentinel repo root (this package's parent). Working dir for code tasks.
REPO_ROOT = Path(__file__).resolve().parent.parent


def ensure_home() -> None:
    """Create SENTINEL_HOME and standard subdirs if missing."""
    for p in (SENTINEL_HOME, MEMORY_ROOT, EVALS_DIR, STATE_DIR, SCREENSHOTS_DIR):
        p.mkdir(parents=True, exist_ok=True)
