"""Background Job Queue -- persistent async job execution with progress tracking.

Agents can submit background jobs (research, indexing, code generation, analysis).
Jobs run asynchronously, report progress, and persist across restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from config.paths import JOBS_PATH

logger = logging.getLogger("job-queue")

MAX_CONCURRENT = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if JOBS_PATH.exists():
        try:
            return json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"jobs": []}


def _save(data: dict):
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOBS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


class JobQueue:
    """Manages background job execution."""

    def __init__(self):
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._progress: dict[str, int] = {}

    def submit(
        self,
        job_type: str,
        description: str,
        func: Callable[..., Coroutine] | None = None,
        args: dict[str, Any] | None = None,
    ) -> dict:
        """Submit a new background job."""
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        job = {
            "id": job_id,
            "type": job_type,
            "description": description,
            "status": "queued",
            "progress": 0,
            "result": None,
            "error": None,
            "logs": [],
            "created_at": _now(),
            "started_at": None,
            "completed_at": None,
        }

        data = _load()
        data["jobs"].append(job)
        if len(data["jobs"]) > 200:
            done = [j for j in data["jobs"] if j["status"] in ("completed", "failed", "cancelled")]
            if len(done) > 100:
                data["jobs"] = [j for j in data["jobs"] if j not in done[:50]]
        _save(data)

        if func:
            asyncio.ensure_future(self._run_job(job_id, func, args or {}))

        return job

    async def _run_job(self, job_id: str, func: Callable, args: dict):
        """Execute a job function and track status."""
        running = len([t for t in self._running_tasks.values() if not t.done()])
        while running >= MAX_CONCURRENT:
            await asyncio.sleep(2)
            running = len([t for t in self._running_tasks.values() if not t.done()])

        self._update_status(job_id, "running", started_at=_now())
        try:
            if asyncio.iscoroutinefunction(func):
                result = await func(job_id=job_id, queue=self, **args)
            else:
                result = func(job_id=job_id, queue=self, **args)
            self._update_status(job_id, "completed", result=result, progress=100)
        except Exception as e:
            logger.error("Job %s failed: %s", job_id, e)
            self._update_status(job_id, "failed", error=str(e))

    def update_progress(self, job_id: str, progress: int, log_message: str = ""):
        """Update job progress (0-100) and optionally add a log entry."""
        self._progress[job_id] = progress
        data = _load()
        for j in data["jobs"]:
            if j["id"] == job_id:
                j["progress"] = progress
                if log_message:
                    j["logs"].append({"time": _now(), "message": log_message})
                    if len(j["logs"]) > 50:
                        j["logs"] = j["logs"][-30:]
                _save(data)
                break

    def _update_status(self, job_id: str, status: str, **kwargs):
        data = _load()
        for j in data["jobs"]:
            if j["id"] == job_id:
                j["status"] = status
                for k, v in kwargs.items():
                    j[k] = v
                if status in ("completed", "failed"):
                    j["completed_at"] = _now()
                _save(data)
                break

    def cancel(self, job_id: str) -> bool:
        """Cancel a running or queued job."""
        task = self._running_tasks.get(job_id)
        if task and not task.done():
            task.cancel()

        self._update_status(job_id, "cancelled")
        return True

    def get_job(self, job_id: str) -> dict | None:
        data = _load()
        for j in data["jobs"]:
            if j["id"] == job_id:
                return j
        return None

    def list_jobs(self, status: str | None = None) -> list[dict]:
        data = _load()
        jobs = data.get("jobs", [])
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        return sorted(jobs, key=lambda j: j.get("created_at", ""), reverse=True)


_queue: JobQueue | None = None


def get_queue() -> JobQueue:
    global _queue
    if _queue is None:
        _queue = JobQueue()
    return _queue
