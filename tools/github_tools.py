"""GitHub Tools -- REST API integration for repos, PRs, branches, CI.

Uses GITHUB_TOKEN from environment for authentication.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("github-tools")

GITHUB_API = "https://api.github.com"


_KEYCHAIN_TOKEN: str | None = None


def _keychain_token() -> str:
    """Fall back to the macOS keychain (where Sentinel keeps secrets — no
    tokens in files). Cached for the process lifetime."""
    global _KEYCHAIN_TOKEN
    if _KEYCHAIN_TOKEN is not None:
        return _KEYCHAIN_TOKEN
    import subprocess
    for service in ("SENTINEL_GITHUB_TOKEN", "GITHUB_TOKEN"):
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                _KEYCHAIN_TOKEN = out.stdout.strip()
                return _KEYCHAIN_TOKEN
        except Exception:
            continue
    _KEYCHAIN_TOKEN = ""
    return _KEYCHAIN_TOKEN


def _headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN", "") or _keychain_token()
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set in environment or keychain")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(path: str, params: dict | None = None) -> Any:
    with httpx.Client(timeout=15) as client:
        resp = client.get(f"{GITHUB_API}{path}", headers=_headers(), params=params)
        resp.raise_for_status()
        return resp.json()


def _post(path: str, json_body: dict | None = None) -> Any:
    with httpx.Client(timeout=15) as client:
        resp = client.post(f"{GITHUB_API}{path}", headers=_headers(), json=json_body)
        resp.raise_for_status()
        return resp.json()


def _put(path: str, json_body: dict | None = None) -> Any:
    with httpx.Client(timeout=15) as client:
        resp = client.put(f"{GITHUB_API}{path}", headers=_headers(), json=json_body)
        resp.raise_for_status()
        return resp.json()


def _patch(path: str, json_body: dict | None = None) -> Any:
    with httpx.Client(timeout=15) as client:
        resp = client.patch(f"{GITHUB_API}{path}", headers=_headers(), json=json_body)
        resp.raise_for_status()
        return resp.json()


def list_repos(max_results: int = 30) -> list[dict]:
    """List user's GitHub repositories."""
    try:
        repos = _get("/user/repos", {"per_page": max_results, "sort": "updated"})
        return [
            {
                "full_name": r["full_name"],
                "name": r["name"],
                "private": r["private"],
                "default_branch": r["default_branch"],
                "language": r.get("language"),
                "updated_at": r["updated_at"],
                "url": r["html_url"],
                "open_issues": r.get("open_issues_count", 0),
            }
            for r in repos
        ]
    except Exception as e:
        return [{"error": str(e)}]


def get_repo_info(owner: str, repo: str) -> dict:
    """Get detailed repository information."""
    try:
        r = _get(f"/repos/{owner}/{repo}")
        return {
            "full_name": r["full_name"],
            "description": r.get("description", ""),
            "default_branch": r["default_branch"],
            "language": r.get("language"),
            "private": r["private"],
            "open_issues": r.get("open_issues_count", 0),
            "forks": r.get("forks_count", 0),
            "stars": r.get("stargazers_count", 0),
            "url": r["html_url"],
            "updated_at": r["updated_at"],
        }
    except Exception as e:
        return {"error": str(e)}


def list_open_prs(owner: str, repo: str, max_results: int = 20) -> list[dict]:
    """List open pull requests with review status."""
    try:
        prs = _get(f"/repos/{owner}/{repo}/pulls", {
            "state": "open", "per_page": max_results, "sort": "updated",
        })
        results = []
        for pr in prs:
            results.append({
                "number": pr["number"],
                "title": pr["title"],
                "author": pr["user"]["login"],
                "branch": pr["head"]["ref"],
                "base": pr["base"]["ref"],
                "created_at": pr["created_at"],
                "updated_at": pr["updated_at"],
                "url": pr["html_url"],
                "draft": pr.get("draft", False),
                "labels": [l["name"] for l in pr.get("labels", [])],
            })
        return results
    except Exception as e:
        return [{"error": str(e)}]


def list_issues(owner: str, repo: str, state: str = "open", max_results: int = 20) -> list[dict]:
    """List issues in a repository."""
    try:
        issues = _get(f"/repos/{owner}/{repo}/issues", {
            "state": state, "per_page": max_results, "sort": "updated",
        })
        return [
            {
                "number": i["number"],
                "title": i["title"],
                "author": i["user"]["login"],
                "state": i["state"],
                "labels": [l["name"] for l in i.get("labels", [])],
                "created_at": i["created_at"],
                "url": i["html_url"],
            }
            for i in issues
            if "pull_request" not in i
        ]
    except Exception as e:
        return [{"error": str(e)}]


def get_issue_details(owner: str, repo: str, issue_number: int) -> dict:
    """Get full details of a specific issue including body and comments."""
    try:
        issue = _get(f"/repos/{owner}/{repo}/issues/{issue_number}")
        comments = []
        if issue.get("comments", 0) > 0:
            raw_comments = _get(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"per_page": 10})
            comments = [
                {"author": c["user"]["login"], "body": c["body"][:2000], "created_at": c["created_at"]}
                for c in raw_comments
            ]
        return {
            "number": issue["number"],
            "title": issue["title"],
            "body": (issue.get("body") or "")[:20000],
            "author": issue["user"]["login"],
            "state": issue["state"],
            "labels": [l["name"] for l in issue.get("labels", [])],
            "assignees": [a["login"] for a in issue.get("assignees", [])],
            "created_at": issue["created_at"],
            "updated_at": issue["updated_at"],
            "url": issue["html_url"],
            "comments": comments,
        }
    except Exception as e:
        return {"error": str(e)}


def get_ci_status(owner: str, repo: str, ref: str = "main") -> dict:
    """Get CI/CD check runs status for a ref (branch or SHA)."""
    try:
        data = _get(f"/repos/{owner}/{repo}/commits/{ref}/check-runs")
        runs = data.get("check_runs", [])
        return {
            "ref": ref,
            "total_count": data.get("total_count", 0),
            "checks": [
                {
                    "name": r["name"],
                    "status": r["status"],
                    "conclusion": r.get("conclusion"),
                    "started_at": r.get("started_at"),
                    "completed_at": r.get("completed_at"),
                }
                for r in runs
            ],
        }
    except Exception as e:
        return {"error": str(e)}


def create_branch(owner: str, repo: str, branch_name: str, from_ref: str = "main") -> dict:
    """Create a new branch from a ref."""
    try:
        ref_data = _get(f"/repos/{owner}/{repo}/git/ref/heads/{from_ref}")
        sha = ref_data["object"]["sha"]
        result = _post(f"/repos/{owner}/{repo}/git/refs", {
            "ref": f"refs/heads/{branch_name}",
            "sha": sha,
        })
        return {
            "branch": branch_name,
            "sha": sha,
            "created": True,
            "url": f"https://github.com/{owner}/{repo}/tree/{branch_name}",
        }
    except Exception as e:
        return {"error": str(e)}


def create_pr(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str = "main",
) -> dict:
    """Create a pull request."""
    try:
        pr = _post(f"/repos/{owner}/{repo}/pulls", {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
        })
        return {
            "number": pr["number"],
            "title": pr["title"],
            "url": pr["html_url"],
            "created": True,
        }
    except Exception as e:
        return {"error": str(e)}


def create_issue(owner: str, repo: str, title: str, body: str,
                 labels: list[str] | None = None) -> dict:
    """Create an issue (used by the coaching loop to file findings on the board)."""
    try:
        issue = _post(f"/repos/{owner}/{repo}/issues", {
            "title": title,
            "body": body,
            "labels": labels or [],
        })
        return {
            "number": issue["number"],
            "title": issue["title"],
            "url": issue["html_url"],
            "created": True,
        }
    except Exception as e:
        return {"error": str(e)}


def update_issue(owner: str, repo: str, number: int, *, state: str = "",
                 labels: list[str] | None = None, body: str = "") -> dict:
    """Update an issue's state/labels/body (coaching board sync)."""
    try:
        payload: dict = {}
        if state:
            payload["state"] = state
        if labels is not None:
            payload["labels"] = labels
        if body:
            payload["body"] = body
        issue = _patch(f"/repos/{owner}/{repo}/issues/{number}", payload)
        return {"number": issue["number"], "state": issue["state"],
                "url": issue["html_url"], "updated": True}
    except Exception as e:
        return {"error": str(e)}


def graphql(query: str, variables: dict | None = None) -> dict:
    """GraphQL call (Projects v2 has no REST API). Returns the `data` dict or
    {'error': ...}."""
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post("https://api.github.com/graphql",
                               headers=_headers(),
                               json={"query": query, "variables": variables or {}})
            data = resp.json()
        if resp.status_code != 200 or data.get("errors"):
            return {"error": str(data.get("errors") or data)[:400]}
        return data.get("data", {})
    except Exception as e:
        return {"error": str(e)}


def create_repo(name: str, private: bool = True, description: str = "") -> dict:
    """Create a new GitHub repository."""
    try:
        repo = _post("/user/repos", {
            "name": name,
            "private": private,
            "description": description,
            "auto_init": True,
        })
        return {
            "full_name": repo["full_name"],
            "url": repo["html_url"],
            "clone_url": repo["clone_url"],
            "created": True,
        }
    except Exception as e:
        return {"error": str(e)}


def push_file(
    owner: str,
    repo: str,
    file_path: str,
    content: str,
    message: str,
    branch: str = "main",
) -> dict:
    """Create or update a file in a repo via the Contents API."""
    import base64 as b64

    try:
        existing_sha = None
        try:
            existing = _get(f"/repos/{owner}/{repo}/contents/{file_path}", {"ref": branch})
            existing_sha = existing.get("sha")
        except httpx.HTTPStatusError:
            pass

        body: dict[str, Any] = {
            "message": message,
            "content": b64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if existing_sha:
            body["sha"] = existing_sha

        result = _put(f"/repos/{owner}/{repo}/contents/{file_path}", body)
        return {
            "path": file_path,
            "sha": result["content"]["sha"],
            "committed": True,
            "url": result["content"]["html_url"],
        }
    except Exception as e:
        return {"error": str(e)}


def get_pr_comments(owner: str, repo: str, pr_number: int) -> list[dict]:
    """Get review comments on a PR."""
    try:
        comments = _get(f"/repos/{owner}/{repo}/pulls/{pr_number}/comments")
        return [
            {
                "author": c["user"]["login"],
                "body": c["body"][:500],
                "path": c.get("path", ""),
                "created_at": c["created_at"],
            }
            for c in comments
        ]
    except Exception as e:
        return [{"error": str(e)}]
