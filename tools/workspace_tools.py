"""Workspace tools -- Gmail, Calendar, Drive, Chat operations."""

from __future__ import annotations

import base64
import email
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from config.paths import REPO_ROOT

logger = logging.getLogger("workspace-tools")


def _get_services():
    from tools.google_auth import (
        get_gmail_service, get_calendar_service, get_drive_service, get_chat_service,
    )
    return {
        "gmail": get_gmail_service(),
        "calendar": get_calendar_service(),
        "drive": get_drive_service(),
        "chat": get_chat_service(),
    }


# ─── Gmail ────────────────────────────────────────────────

def search_emails(query: str = "is:unread", max_results: int = 20) -> list[dict]:
    """Search emails using Gmail search syntax."""
    svc = _get_services()["gmail"]
    resp = svc.users().messages().list(
        userId="me", q=query, maxResults=max_results,
    ).execute()

    messages = []
    for msg_meta in resp.get("messages", []):
        msg = svc.users().messages().get(
            userId="me", id=msg_meta["id"], format="metadata",
            metadataHeaders=["From", "To", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append({
            "id": msg["id"],
            "thread_id": msg["threadId"],
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "snippet": msg.get("snippet", ""),
            "labels": msg.get("labelIds", []),
            "message_link": f"https://mail.google.com/mail/u/0/#inbox/{msg['id']}",
        })
    return messages


def read_email(msg_id: str) -> dict:
    """Read full email content by message ID."""
    svc = _get_services()["gmail"]
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}

    body = _extract_body(msg.get("payload", {}))

    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "body": body,
        "labels": msg.get("labelIds", []),
        "message_link": f"https://mail.google.com/mail/u/0/#inbox/{msg['id']}",
    }


def draft_email(to: str, subject: str, body: str) -> dict:
    """Create a draft email. Does NOT send -- draft only."""
    import base64
    from email.mime.text import MIMEText

    svc = _get_services()["gmail"]
    message = MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    draft = svc.users().drafts().create(
        userId="me", body={"message": {"raw": raw}},
    ).execute()

    return {
        "draft_id": draft["id"],
        "message_id": draft["message"]["id"],
        "status": "drafted (NOT sent)",
    }


def _extract_body(payload: dict) -> str:
    """Extract text body from email payload."""
    if payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
        if part.get("parts"):
            result = _extract_body(part)
            if result:
                return result

    for part in payload.get("parts", []):
        if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")

    return ""


# ─── Calendar ─────────────────────────────────────────────

def get_todays_events() -> list[dict]:
    """Get today's calendar events."""
    svc = _get_services()["calendar"]
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)

    events = svc.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    return [_format_event(ev) for ev in events.get("items", [])]


def get_upcoming_events(hours: int = 24, max_results: int = 20) -> list[dict]:
    """Get upcoming events within the next N hours."""
    svc = _get_services()["calendar"]
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours)

    events = svc.events().list(
        calendarId="primary",
        timeMin=now.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()

    return [_format_event(ev) for ev in events.get("items", [])]


def get_event_details(event_id: str) -> dict:
    """Get full details of a calendar event."""
    svc = _get_services()["calendar"]
    ev = svc.events().get(calendarId="primary", eventId=event_id).execute()
    return _format_event(ev)


def _format_event(ev: dict) -> dict:
    start = ev.get("start", {})
    end = ev.get("end", {})
    attendees = ev.get("attendees", [])

    return {
        "id": ev.get("id", ""),
        "summary": ev.get("summary", "(no title)"),
        "description": ev.get("description", ""),
        "start": start.get("dateTime", start.get("date", "")),
        "end": end.get("dateTime", end.get("date", "")),
        "location": ev.get("location", ""),
        "attendees": [
            {"email": a.get("email", ""), "name": a.get("displayName", ""), "status": a.get("responseStatus", "")}
            for a in attendees
        ],
        "hangout_link": ev.get("hangoutLink", ""),
        "html_link": ev.get("htmlLink", ""),
        "calendar_link": ev.get("htmlLink", ""),
        "meet_link": ev.get("hangoutLink", ""),
        "attachments": [
            {"title": att.get("title", ""), "url": att.get("fileUrl", "")}
            for att in ev.get("attachments", [])
        ],
    }


# ─── Drive ────────────────────────────────────────────────

def search_drive(query: str = "", max_results: int = 20) -> list[dict]:
    """Search Google Drive files."""
    svc = _get_services()["drive"]
    q = query or "trashed=false"
    resp = svc.files().list(
        q=q, pageSize=max_results,
        fields="files(id,name,mimeType,modifiedTime,webViewLink,owners)",
    ).execute()

    return [
        {
            "id": f["id"],
            "name": f["name"],
            "type": f["mimeType"],
            "modified": f.get("modifiedTime", ""),
            "url": f.get("webViewLink", ""),
            "doc_link": f.get("webViewLink", ""),
            "owner": f.get("owners", [{}])[0].get("emailAddress", "") if f.get("owners") else "",
        }
        for f in resp.get("files", [])
    ]


def read_drive_file(file_id: str) -> dict:
    """Read content of a Google Drive file using Drive export API (no Docs API needed)."""
    svc = _get_services()["drive"]
    try:
        meta = svc.files().get(
            fileId=file_id, fields="id,name,mimeType,webViewLink",
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        logger.warning("Drive file metadata fetch failed for %s: %s", file_id, e)
        return {"id": file_id, "error": f"Could not access file: {e}", "content": ""}

    mime = meta.get("mimeType", "")

    content = ""
    export_map = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
        "application/vnd.google-apps.drawing": "image/png",
    }

    if mime in export_map:
        try:
            resp = svc.files().export(fileId=file_id, mimeType=export_map[mime]).execute()
            if isinstance(resp, bytes):
                content = resp.decode("utf-8", errors="replace")
            else:
                content = str(resp)
            if len(content) > 50000:
                content = content[:50000] + "\n\n... [truncated at 50K chars] ..."
        except Exception as e:
            logger.warning("Drive export failed for %s: %s", file_id, e)
            content = f"[Failed to export: {e}]"
    elif mime and not mime.startswith("application/vnd.google-apps"):
        try:
            resp = svc.files().get_media(fileId=file_id).execute()
            content = resp.decode("utf-8", errors="replace")[:50000]
        except Exception:
            content = f"[Binary file: {meta['name']}]"
    else:
        content = f"[Unsupported Google Apps type: {mime}]"

    return {
        "id": file_id,
        "name": meta["name"],
        "type": mime,
        "content": content,
        "doc_link": meta.get("webViewLink", ""),
    }


def extract_drive_id_from_url(url: str) -> str | None:
    """Extract Google Drive/Docs/Sheets/Slides file ID from a URL."""
    import re
    patterns = [
        r'/document/d/([a-zA-Z0-9_-]+)',
        r'/spreadsheets/d/([a-zA-Z0-9_-]+)',
        r'/presentation/d/([a-zA-Z0-9_-]+)',
        r'/file/d/([a-zA-Z0-9_-]+)',
        r'id=([a-zA-Z0-9_-]+)',
        r'/folders/([a-zA-Z0-9_-]+)',
    ]
    for pat in patterns:
        match = re.search(pat, url)
        if match:
            return match.group(1)
    return None


def read_drive_url(url: str) -> dict:
    """Read a Google Doc/Sheet/Slide by its full URL. Falls back to Drive search if 404."""
    file_id = extract_drive_id_from_url(url)
    if not file_id:
        return {"error": f"Could not extract file ID from URL: {url}"}

    result = read_drive_file(file_id)
    if result.get("error") and "not found" in str(result["error"]).lower():
        logger.info("File ID %s not found, attempting fuzzy search in Drive...", file_id)
        try:
            files = search_drive(query="trashed=false", max_results=50)
            for f in files:
                if f["id"] != file_id and _ids_similar(f["id"], file_id):
                    logger.info("Found similar file: %s (%s)", f["name"], f["id"])
                    return read_drive_file(f["id"])
        except Exception as e:
            logger.warning("Drive fallback search failed: %s", e)
    return result


def _ids_similar(id1: str, id2: str) -> bool:
    """Check if two Drive file IDs are visually similar (l/I/1 confusion)."""
    def normalize(s: str) -> str:
        return s.lower().replace("1", "l").replace("i", "l")
    return normalize(id1) == normalize(id2)


# ─── GChat ────────────────────────────────────────────────

_sender_cache: dict[str, str] = {}


def resolve_chat_sender(user_id: str) -> str:
    """Chat 'users/<id>' -> real display name.

    Roster first (tools.people, already-correlated ids), then a live People API
    lookup via gog (people/<id>), persisting the correlation back to the roster
    so each person costs one lookup ever. Returns '' when unresolvable."""
    uid = (user_id or "").strip()
    if not uid.startswith("users/"):
        return ""
    if uid in _sender_cache:
        return _sender_cache[uid]
    name = ""
    try:
        from tools import people
        name = people.name_for_chat_id(uid)
    except Exception:
        people = None
    if not name:
        try:
            import json as _json
            import subprocess
            from tools.gog_workspace import ACCOUNT
            out = subprocess.run(
                ["gog", "-a", ACCOUNT, "people", "get",
                 "people/" + uid.split("/", 1)[1], "-j"],
                capture_output=True, text=True, timeout=20)
            if out.returncode == 0 and out.stdout.strip():
                p = _json.loads(out.stdout).get("person", {})
                names = [n.get("displayName") for n in p.get("names", []) if n.get("displayName")]
                emails = [e.get("value") for e in p.get("emailAddresses", []) if e.get("value")]
                name = names[0] if names else ""
                if name and people is not None:
                    try:
                        people.link_chat_id(uid, email=(emails[0] if emails else ""))
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("sender resolve failed for %s: %s", uid, e)
    _sender_cache[uid] = name   # cache misses too, so we don't re-hit the API
    return name


def list_spaces(max_results: int = 50) -> list[dict]:
    """List all available GChat spaces."""
    svc = _get_services()["chat"]
    resp = svc.spaces().list(pageSize=max_results).execute()
    return [
        {
            "id": sp["name"],
            "display_name": sp.get("displayName", "(unnamed)"),
            "type": sp.get("type", ""),
            "space_link": f"https://mail.google.com/mail/u/0/#chat/space/{sp['name'].split('/')[-1]}" if sp.get("name") else "",
        }
        for sp in resp.get("spaces", [])
    ]


def read_space_messages(
    space_id: str,
    max_results: int = 20,
    after: str | None = None,
    lookback_days: int = 5,
) -> list[dict]:
    """Read messages from a GChat space, defaulting to last 5 days."""
    svc = _get_services()["chat"]
    kwargs: dict[str, Any] = {"parent": space_id, "pageSize": max_results}

    def _to_chat_ts(dt_str: str) -> str:
        """Normalize a datetime string to the format Google Chat API expects."""
        from datetime import datetime as _dt
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                    "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = _dt.strptime(dt_str.replace("+00:00Z", "Z").replace("+00:00", "Z"), fmt)
                return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
        return dt_str

    if after:
        kwargs["filter"] = f'createTime > "{_to_chat_ts(after)}"'
    elif lookback_days > 0:
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        kwargs["filter"] = f'createTime > "{cutoff}"'

    try:
        resp = svc.spaces().messages().list(**kwargs).execute()
    except Exception as e:
        logger.warning("Failed to read messages from %s: %s", space_id, e)
        return []

    space_id_part = space_id.split("/")[-1] if "/" in space_id else space_id
    messages = []
    for msg in resp.get("messages", []):
        sender = msg.get("sender", {})
        raw_id = sender.get("name", "")
        # Chat API rarely includes displayName; resolve users/<id> to a real
        # name via the roster/People API so downstream never sees opaque ids.
        display = sender.get("displayName", "") or resolve_chat_sender(raw_id) or raw_id
        messages.append({
            "id": msg.get("name", ""),
            "text": msg.get("text", ""),
            "sender": display,
            "sender_id": raw_id,
            "sender_type": sender.get("type", ""),
            "created": msg.get("createTime", ""),
            "space": space_id,
            "space_link": f"https://mail.google.com/mail/u/0/#chat/space/{space_id_part}",
        })
    return messages


# ─── Meeting Transcripts & Drive ──────────────────────────

def search_meeting_transcripts(days: int = 5) -> list[dict]:
    """Search Gmail for Google Meet notes/transcripts from the last N days. Fetches FULL email bodies."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_str = cutoff.strftime("%Y/%m/%d")

    query = f'(subject:"Notes:" OR subject:"Meeting transcript" OR subject:"Action items from") after:{cutoff_str}'
    emails_meta = search_emails(query, max_results=20)

    transcripts = []
    for em in emails_meta:
        try:
            full = read_email(em["id"])
            body = full.get("body", "")
        except Exception:
            body = em.get("snippet", "")

        transcripts.append({
            "id": em.get("id", ""),
            "subject": em.get("subject", ""),
            "from": em.get("from", ""),
            "date": em.get("date", ""),
            "snippet": em.get("snippet", ""),
            "body": body,
            "message_link": f"https://mail.google.com/mail/u/0/#inbox/{em.get('id', '')}",
        })
    return transcripts


def get_user_sent_emails(hours: int = 48) -> list[dict]:
    """Get emails the user has sent recently. Useful for understanding reply patterns and tone."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y/%m/%d")

    query = f"in:sent after:{cutoff_str}"
    sent_meta = search_emails(query, max_results=20)

    results = []
    for em in sent_meta:
        try:
            full = read_email(em["id"])
            body = full.get("body", "")[:2000]
        except Exception:
            body = em.get("snippet", "")

        results.append({
            "id": em.get("id", ""),
            "to": em.get("to", ""),
            "subject": em.get("subject", ""),
            "date": em.get("date", ""),
            "body_preview": body,
            "message_link": f"https://mail.google.com/mail/u/0/#sent/{em.get('id', '')}",
        })
    return results


def get_recent_drive_edits(hours: int = 24) -> list[dict]:
    """Get recently modified files from Google Drive."""
    svc = _get_services()["drive"]
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    try:
        resp = svc.files().list(
            q=f"modifiedTime > '{cutoff}' and trashed = false",
            orderBy="modifiedTime desc",
            pageSize=20,
            fields="files(id, name, mimeType, modifiedTime, webViewLink, lastModifyingUser)",
        ).execute()
    except Exception as e:
        logger.warning("Failed to fetch recent Drive edits: %s", e)
        return []

    return [
        {
            "id": f.get("id", ""),
            "name": f.get("name", ""),
            "type": f.get("mimeType", ""),
            "modified": f.get("modifiedTime", ""),
            "url": f.get("webViewLink", ""),
            "doc_link": f.get("webViewLink", ""),
            "modified_by": (f.get("lastModifyingUser") or {}).get("displayName", ""),
        }
        for f in resp.get("files", [])
    ]


# ─── Repo Observation ─────────────────────────────────────

def get_repo_activity(repo_path: str = str(REPO_ROOT.parent), days: int = 3) -> dict:
    """Observe recent activity across ALL repos and experiment folders in the workspace."""
    import subprocess, os

    result: dict[str, Any] = {"workspace_path": repo_path, "repos": [], "recent_files": []}

    # Scan for git repos (max depth 2)
    for entry in sorted(os.listdir(repo_path)):
        sub = os.path.join(repo_path, entry)
        if not os.path.isdir(sub):
            continue
        git_dir = os.path.join(sub, ".git")
        if os.path.isdir(git_dir):
            repo_info = _scan_git_repo(sub, entry, days)
            if repo_info.get("recent_commits"):
                result["repos"].append(repo_info)

    # Scan for recently modified files across entire workspace (including non-git folders like experiments)
    cutoff_ts = datetime.now().timestamp() - (days * 86400)
    extensions = {".py", ".md", ".mp4", ".tsx", ".ts", ".json", ".txt", ".sh"}
    try:
        for root, dirs, files in os.walk(repo_path):
            depth = root.replace(repo_path, "").count(os.sep)
            if depth > 3:
                dirs.clear()
                continue
            if any(skip in root for skip in ["node_modules", ".git", "__pycache__", ".next", "venv"]):
                continue
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in extensions:
                    continue
                fpath = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime > cutoff_ts:
                        result["recent_files"].append({
                            "path": os.path.relpath(fpath, repo_path),
                            "modified": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
                            "size_kb": round(os.path.getsize(fpath) / 1024, 1),
                        })
                except Exception:
                    pass
    except Exception:
        pass

    result["recent_files"].sort(key=lambda x: x["modified"], reverse=True)
    result["recent_files"] = result["recent_files"][:30]

    return result


def _scan_git_repo(repo_path: str, name: str, days: int) -> dict:
    """Scan a single git repo for recent activity."""
    import subprocess
    info: dict[str, Any] = {"name": name, "path": repo_path}

    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        )
        info["branch"] = branch.stdout.strip()
    except Exception:
        info["branch"] = ""

    try:
        log = subprocess.run(
            ["git", "log", f"--since={days} days ago", "--format=%h|%s|%ai", "--no-merges", "-10"],
            cwd=repo_path, capture_output=True, text=True, timeout=10,
        )
        commits = []
        if log.stdout.strip():
            for line in log.stdout.strip().split("\n"):
                parts = line.split("|", 2)
                if len(parts) == 3:
                    commits.append({"hash": parts[0], "message": parts[1], "date": parts[2]})
        info["recent_commits"] = commits
    except Exception:
        info["recent_commits"] = []

    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        )
        changes = status.stdout.strip()
        info["uncommitted_changes"] = changes[:1000] if changes else "clean"
    except Exception:
        pass

    return info


def extract_links_from_chat_messages(space_id: str, lookback_days: int = 30) -> list[dict]:
    """Extract Google Docs/Sheets/Drive links from chat messages and read their content."""
    import re
    messages = read_space_messages(space_id=space_id, lookback_days=lookback_days, max_results=100)

    url_pattern = re.compile(r'https?://(?:docs\.google\.com|drive\.google\.com)/\S+')
    found_links: list[dict] = []
    seen_urls: set[str] = set()

    for msg in messages:
        text = msg.get("text", "")
        urls = url_pattern.findall(text)
        for url in urls:
            clean_url = url.rstrip(".,;)>")
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            file_id = extract_drive_id_from_url(clean_url)
            doc_content = ""
            doc_name = ""
            if file_id:
                try:
                    doc = read_drive_file(file_id)
                    doc_name = doc.get("name", "")
                    doc_content = doc.get("content", "")[:5000]
                except Exception as e:
                    doc_content = f"[Could not read: {e}]"

            found_links.append({
                "url": clean_url,
                "shared_by": msg.get("sender", ""),
                "shared_date": msg.get("created", ""),
                "space": msg.get("space", ""),
                "context_message": text[:500],
                "doc_name": doc_name,
                "doc_preview": doc_content,
                "file_id": file_id or "",
            })

    return found_links


# ─── GChat DM Reading ─────────────────────────────────────

def list_dm_spaces() -> list[dict]:
    """List all Direct Message (1:1) chat spaces."""
    svc = _get_services()["chat"]
    all_spaces = []
    page_token = None

    for _ in range(5):
        kwargs: dict[str, Any] = {"pageSize": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            resp = svc.spaces().list(**kwargs).execute()
        except Exception as e:
            logger.warning("Failed to list DM spaces: %s", e)
            return all_spaces

        for sp in resp.get("spaces", []):
            sp_type = sp.get("spaceType", sp.get("type", ""))
            if sp_type == "DIRECT_MESSAGE" or sp.get("singleUserBotDm") is False and "DM" in sp.get("type", ""):
                all_spaces.append({
                    "id": sp["name"],
                    "display_name": sp.get("displayName", "(DM)"),
                    "type": "DM",
                    "last_active": sp.get("lastActiveTime", ""),
                })

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return all_spaces


def read_dm_messages(person_name: str, max_messages: int = 20) -> list[dict]:
    """Read DM messages with a specific person by fuzzy-matching their name."""
    dm_spaces = list_dm_spaces()
    target_lower = person_name.strip().lower()

    matched_space = None
    for sp in dm_spaces:
        display = sp.get("display_name", "").lower()
        if target_lower in display or display in target_lower:
            matched_space = sp
            break

    if not matched_space:
        for sp in dm_spaces:
            display = sp.get("display_name", "").lower()
            if any(word in display for word in target_lower.split()):
                matched_space = sp
                break

    if not matched_space:
        return [{"error": f"No DM space found for '{person_name}'", "available_dms": [s["display_name"] for s in dm_spaces[:10]]}]

    messages = read_space_messages(
        space_id=matched_space["id"],
        max_results=max_messages,
        lookback_days=14,
    )

    is_social = _classify_dm_content(messages)

    return [{
        "space_id": matched_space["id"],
        "person": matched_space["display_name"],
        "is_social": is_social,
        "message_count": len(messages),
        "messages": messages,
    }]


def _classify_dm_content(messages: list[dict]) -> bool:
    """Classify if a DM conversation is social/gossip vs work-related."""
    if not messages:
        return False

    social_indicators = 0
    total = len(messages)

    for msg in messages:
        text = msg.get("text", "").strip()
        if len(text) < 5:
            social_indicators += 1
        elif all(c in "😀😂🤣❤️👍🏻👍🔥💯🙏🎉✅👏🤔😅😊🥳" for c in text.replace(" ", "")):
            social_indicators += 1

    return total > 0 and (social_indicators / total) > 0.7


# ─── Deep Meeting Transcript Scan ────────────────────────────

def deep_scan_transcripts(days: int = 30) -> list[dict]:
    """Deep scan ALL meeting transcripts from email over N days.

    Returns summarized transcripts for storage in ChromaDB.
    """
    transcripts = search_meeting_transcripts(days=days)

    summaries = []
    for t in transcripts:
        body = t.get("body", t.get("snippet", ""))
        if not body:
            continue

        summary = {
            "id": t["id"],
            "subject": t["subject"],
            "date": t["date"],
            "body_preview": body[:3000],
            "message_link": t.get("message_link", ""),
        }

        try:
            from tools.codebase.search import store_transcript_memory
            store_transcript_memory(
                text=f"Meeting: {t['subject']} ({t['date']})\n\n{body[:2000]}",
                metadata={
                    "meeting_title": t["subject"],
                    "date": t["date"],
                    "email_id": t["id"],
                },
            )
        except Exception as e:
            logger.warning("Failed to store transcript in ChromaDB: %s", e)

        summaries.append(summary)

    return summaries


def search_transcripts_semantic(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over stored meeting transcripts."""
    try:
        from tools.codebase.search import search_transcripts
        return search_transcripts(query, top_k=top_k)
    except Exception as e:
        return [{"error": str(e)}]


# ─── Experiment Tracker ──────────────────────────────────────

def scan_experiments(workspace_path: str = str(REPO_ROOT.parent)) -> list[dict]:
    """Scan all experiment_* folders and detect their progress stage."""
    import os

    experiments = []
    try:
        for entry in sorted(os.listdir(workspace_path)):
            if not entry.startswith("experiment") and not entry.startswith("Experiment"):
                continue
            exp_path = os.path.join(workspace_path, entry)
            if not os.path.isdir(exp_path):
                continue

            exp_info: dict[str, Any] = {
                "name": entry,
                "path": exp_path,
                "stage": "unknown",
                "topic": "",
                "has_script": False,
                "has_clips": False,
                "has_output": False,
                "has_final": False,
                "files_count": 0,
                "completion_pct": 0,
            }

            docs_dir = os.path.join(exp_path, "docs")
            if os.path.isdir(docs_dir):
                script_path = os.path.join(docs_dir, "script.md")
                if os.path.exists(script_path):
                    exp_info["has_script"] = True
                    try:
                        content = open(script_path, encoding="utf-8").read(500)
                        lines = content.split("\n")
                        for line in lines:
                            if line.startswith("# ") or line.startswith("## "):
                                exp_info["topic"] = line.lstrip("# ").strip()
                                break
                    except Exception:
                        pass

            clips_dir = os.path.join(exp_path, "clips")
            if os.path.isdir(clips_dir):
                clip_count = sum(1 for f in os.listdir(clips_dir) if os.path.isdir(os.path.join(clips_dir, f)))
                if clip_count > 0:
                    exp_info["has_clips"] = True
                    exp_info["clips_count"] = clip_count

            output_dir = os.path.join(exp_path, "output")
            if os.path.isdir(output_dir):
                exp_info["has_output"] = True
                for f in os.listdir(output_dir):
                    if f.endswith(".mp4") and "final" in f.lower():
                        exp_info["has_final"] = True
                        fp = os.path.join(output_dir, f)
                        exp_info["final_file"] = f
                        exp_info["final_size_mb"] = round(os.path.getsize(fp) / (1024 * 1024), 1)
                        break

            total_files = 0
            for root, dirs, files in os.walk(exp_path):
                total_files += len(files)
                if total_files > 500:
                    break
            exp_info["files_count"] = total_files

            stages = [exp_info["has_script"], exp_info["has_clips"], exp_info["has_output"], exp_info["has_final"]]
            exp_info["completion_pct"] = int(sum(stages) / 4 * 100)
            if exp_info["has_final"]:
                exp_info["stage"] = "complete"
            elif exp_info["has_output"]:
                exp_info["stage"] = "rendering"
            elif exp_info["has_clips"]:
                exp_info["stage"] = "editing"
            elif exp_info["has_script"]:
                exp_info["stage"] = "scripted"
            else:
                exp_info["stage"] = "planning"

            try:
                mtime = max(
                    os.path.getmtime(os.path.join(root, f))
                    for root, _, files in os.walk(exp_path)
                    for f in files
                ) if total_files > 0 else 0
                exp_info["last_modified"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") if mtime else ""
            except Exception:
                exp_info["last_modified"] = ""

            experiments.append(exp_info)

    except Exception as e:
        logger.warning("Experiment scan failed: %s", e)

    return experiments


# ─── What Did I Miss ─────────────────────────────────────────

def what_did_i_miss(since_hours: int = 12) -> dict:
    """Collect everything that happened since N hours ago, grouped by urgency."""
    result: dict[str, Any] = {
        "since_hours": since_hours,
        "urgent": [],
        "important": [],
        "fyi": [],
    }

    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        cutoff_str = cutoff.strftime("%Y/%m/%d")

        emails = search_emails(f"is:unread after:{cutoff_str}", max_results=30)
        for em in emails:
            item = {
                "type": "email",
                "from": em.get("from", ""),
                "subject": em.get("subject", ""),
                "snippet": em.get("snippet", ""),
                "link": f"https://mail.google.com/mail/u/0/#inbox/{em.get('id', '')}",
            }
            subj_lower = em.get("subject", "").lower()
            if any(w in subj_lower for w in ["urgent", "asap", "blocking", "critical"]):
                result["urgent"].append(item)
            elif any(w in subj_lower for w in ["action", "review", "approve", "deadline"]):
                result["important"].append(item)
            else:
                result["fyi"].append(item)
    except Exception as e:
        result["email_error"] = str(e)

    try:
        events = get_todays_events()
        for ev in events:
            result["important"].append({
                "type": "meeting",
                "summary": ev.get("summary", ""),
                "start": ev.get("start", ""),
                "attendees": ev.get("attendees", [])[:5],
                "link": ev.get("hangout_link", ev.get("calendar_link", "")),
            })
    except Exception as e:
        result["calendar_error"] = str(e)

    try:
        repo = get_repo_activity(days=1)
        if repo.get("repos"):
            for r in repo["repos"]:
                commits = r.get("recent_commits", [])
                if commits:
                    result["fyi"].append({
                        "type": "code_activity",
                        "repo": r["name"],
                        "commits": len(commits),
                        "latest": commits[0].get("message", "") if commits else "",
                    })
    except Exception:
        pass

    return result


# ─── Email Draft Helper ──────────────────────────────────────

def create_draft(to: str, subject: str, body: str) -> dict:
    """Create a Gmail draft (does NOT send)."""
    svc = _get_services()["gmail"]
    import email.mime.text

    msg = email.mime.text.MIMEText(body)
    msg["to"] = to
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        draft = svc.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()
        return {
            "id": draft["id"],
            "message_id": draft["message"]["id"],
            "status": "draft_created",
        }
    except Exception as e:
        return {"error": f"Draft creation failed: {e}"}


def create_calendar_event(
    summary: str,
    start_time: str,
    end_time: str,
    attendees: list[str] | None = None,
    description: str = "",
) -> dict:
    """Create a Google Calendar event."""
    svc = _get_services()["calendar"]
    event_body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start_time, "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": end_time, "timeZone": "Asia/Kolkata"},
        "description": description,
    }
    if attendees:
        event_body["attendees"] = [{"email": a} for a in attendees]

    try:
        event = svc.events().insert(calendarId="primary", body=event_body).execute()
        return {
            "id": event["id"],
            "link": event.get("htmlLink", ""),
            "status": "event_created",
        }
    except Exception as e:
        return {"error": f"Event creation failed: {e}"}


# ─── Knowledge Access ────────────────────────────────────────

def get_current_knowledge() -> dict:
    """Get the FULL previous analysis result and workspace intelligence.

    CRITICAL: Call this FIRST on every analysis cycle. It contains your previous analysis,
    learned contact priorities, tracked deadlines, reply patterns, and accumulated insights.
    Use it to avoid re-analyzing things you already know, and to check if tasks were already completed.
    """
    try:
        from tools.workspace_assistant import knowledge
    except Exception:
        # Advanced analyzer companion not ported in the core build — degrade gracefully.
        return {"note": "knowledge store not available in this build"}

    result: dict[str, Any] = {}

    last_analysis = knowledge.get_analysis_result()
    if last_analysis:
        result["previous_analysis"] = last_analysis
        result["previous_targets"] = last_analysis.get("daily_targets", [])
        result["previous_replies"] = last_analysis.get("needs_reply", [])
        result["completed_since_last"] = last_analysis.get("completed_since_last", [])

    result["tasks"] = knowledge.get_tasks(include_done=True)
    result["insights"] = knowledge.get_insights(min_confidence=0.3)
    result["reply_patterns"] = knowledge.get_reply_patterns()
    result["top_contacts"] = knowledge.get_top_contacts(15)

    try:
        from tools.workspace_assistant.contacts import get_contact_summary_for_prompt
        result["contact_priorities"] = get_contact_summary_for_prompt()
    except Exception:
        pass

    try:
        from tools.workspace_assistant.deadlines import get_active_deadlines
        result["active_deadlines"] = get_active_deadlines()
    except Exception:
        pass

    return result


# ─── Combined Helpers ─────────────────────────────────────

def get_workspace_summary() -> dict:
    """Quick summary: unread email count, today's events."""
    summary: dict[str, Any] = {}

    try:
        emails = search_emails("is:unread", max_results=5)
        summary["unread_emails"] = len(emails)
        summary["email_preview"] = [
            {"from": e["from"], "subject": e["subject"]} for e in emails[:5]
        ]
    except Exception as e:
        summary["email_error"] = str(e)

    try:
        events = get_todays_events()
        summary["todays_events"] = len(events)
        summary["events"] = [
            {"summary": ev["summary"], "start": ev["start"]} for ev in events
        ]
    except Exception as e:
        summary["calendar_error"] = str(e)

    return summary


# ═══════════════════════════════════════════════════════════════════════════
# gog-backed shim (v0.12) — the OAuth `_get_services()` path above depends on a
# client_secret.json that doesn't exist, so it FileNotFoundError'd on every
# autonomous cycle. These definitions come LAST (last-definition-wins), so the
# names the verticals call now delegate to tools.gog_workspace (the `gog` CLI,
# already authorized). If `gog` is unavailable, they degrade to []/{} rather
# than crash. Reuses _format_event + _classify_dm_content above.
# ═══════════════════════════════════════════════════════════════════════════

def _run_async(coro):
    """Run an async gog helper from sync code. The verticals call these sync
    functions from INSIDE a running event loop (their perceive/run_once are
    async), so we must use a fresh thread when a loop is already running — bare
    asyncio.run() would raise 'cannot be called from a running event loop'."""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


def _gog_ok() -> bool:
    try:
        from tools import gog_workspace as gw
        return gw.available()
    except Exception:
        return False


def search_emails(query: str = "is:unread", max_results: int = 20) -> list[dict]:  # noqa: F811
    if not _gog_ok():
        return []
    from tools import gog_workspace as gw
    threads = _run_async(gw.search_emails(query, max_results)) or []
    out = []
    for t in threads:
        if not isinstance(t, dict):
            continue
        out.append({
            "id": t.get("id", t.get("threadId", "")),
            "thread_id": t.get("id", ""),
            "from": t.get("from", ""),
            "to": t.get("to", ""),
            "subject": t.get("subject", "(no subject)"),
            "date": t.get("date", ""),
            "snippet": t.get("snippet", ""),      # gog search omits; filled by read_email if needed
            "labels": t.get("labels", []),
            "message_link": "",
        })
    return out


def read_email(msg_id: str) -> dict:  # noqa: F811
    if not _gog_ok():
        return {}
    from tools import gog_workspace as gw
    r = _run_async(gw.read_email(msg_id)) or {}
    if not isinstance(r, dict):
        return {}
    h = r.get("headers", {}) if isinstance(r.get("headers"), dict) else {}
    msg = r.get("message", {}) if isinstance(r.get("message"), dict) else {}
    return {
        "id": msg.get("id", msg_id),
        "thread_id": msg.get("threadId", ""),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "subject": h.get("subject", ""),
        "date": h.get("date", ""),
        "body": r.get("body", msg.get("snippet", "")),
        "labels": msg.get("labelIds", []),
        "message_link": "",
    }


def _gog_spaces(dm_only: bool) -> list[dict]:
    if not _gog_ok():
        return []
    from tools import gog_workspace as gw
    from channels.gchat_bridge import _space_id
    spaces = _run_async(gw.list_spaces(50)) or []
    out = []
    for s in spaces:
        if not isinstance(s, dict):
            continue
        stype = str(s.get("spaceType") or s.get("type") or "")
        is_dm = "DIRECT" in stype.upper()
        if dm_only and not is_dm:
            continue
        sid = _space_id(s)
        # BUG-1 fix: gog DM spaces carry NO `name`, so an empty display_name made
        # inbox_triage skip every DM (`if not person: continue`). Fall back to the
        # space id so DMs are addressable; read_dm_messages accepts a space id.
        display = s.get("name", "") or (sid if is_dm else "")
        out.append({
            "id": sid,
            "display_name": display,
            "type": "DM" if is_dm else (s.get("type") or "SPACE"),
            "space_link": s.get("uri", ""),
            "last_active": s.get("lastActiveTime", ""),
        })
    return out


def list_spaces(max_results: int = 50) -> list[dict]:  # noqa: F811
    return _gog_spaces(dm_only=False)[:max_results]


def list_dm_spaces() -> list[dict]:  # noqa: F811
    return _gog_spaces(dm_only=True)


def _map_msgs(msgs) -> list[dict]:
    out = []
    for m in (msgs or []):
        if isinstance(m, dict) and m.get("text"):
            raw = m.get("sender", "")
            # gog_workspace resolves users/<id> -> "Name <email>" (People API,
            # kv-cached) and attaches sender_name; prefer it so triage, drafts
            # and transcripts see real names instead of opaque ids.
            out.append({
                "id": m.get("resource", ""),
                "text": m.get("text", ""),
                "sender": m.get("sender_name") or raw,
                "sender_id": raw,
                "sender_type": "",                       # gog doesn't expose; callers tolerate
                "created": m.get("createTime", ""),
                "space": m.get("space", ""),
                "space_link": "",
            })
    return out


def read_space_messages(space_id: str, max_results: int = 20, after: str | None = None,
                        lookback_days: int = 5) -> list[dict]:  # noqa: F811
    if not _gog_ok():
        return []
    from tools import gog_workspace as gw
    return _map_msgs(_run_async(gw.read_space_messages(space_id, max_results)))


def read_dm_messages(person_name: str, max_messages: int = 20) -> list[dict]:  # noqa: F811
    if not _gog_ok():
        return [{"error": "gog unavailable", "available_dms": []}]
    from tools import gog_workspace as gw
    # `person_name` may be a space id (spaces/XXX — the BUG-1 fallback) or a real
    # person name. A space id reads directly via read_space_messages; else read_dm.
    if str(person_name).startswith("spaces/"):
        msgs = _map_msgs(_run_async(gw.read_space_messages(person_name, max_messages)))
    else:
        msgs = _map_msgs(_run_async(gw.read_dm(person_name, max_messages)))
    return [{
        "space_id": person_name if str(person_name).startswith("spaces/") else "",
        "person": person_name,
        "is_social": _classify_dm_content(msgs),
        "message_count": len(msgs),
        "messages": msgs,
    }]


def get_upcoming_events(hours: int = 24, max_results: int = 20) -> list[dict]:  # noqa: F811
    if not _gog_ok():
        return []
    from tools import gog_workspace as gw
    events = _run_async(gw.upcoming_events(max_results)) or []
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=hours)
    out = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        fmt = _format_event(ev)          # flattens gog's object start/end + maps meet_link/attendees
        # window filter (best-effort; keep if we can't parse the start)
        try:
            s = fmt.get("start", "")
            st = datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None
            if st is not None:
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                if st > horizon or st < now - timedelta(hours=1):
                    continue
        except (ValueError, TypeError):
            pass
        out.append(fmt)
    return out


def get_user_sent_emails(hours: int = 48) -> list[dict]:  # noqa: F811
    days = max(1, round(hours / 24))
    threads = search_emails(f"in:sent newer_than:{days}d", max_results=20)
    out = []
    for t in threads[:20]:
        body = ""
        if t.get("id"):
            body = (read_email(t["id"]) or {}).get("body", "")[:300]
        out.append({"id": t.get("id", ""), "to": t.get("to", ""),
                    "subject": t.get("subject", ""), "date": t.get("date", ""),
                    "body_preview": body, "message_link": ""})
    return out


def search_meeting_transcripts(days: int = 5) -> list[dict]:  # noqa: F811
    q = ('(subject:"Notes:" OR subject:"Meeting transcript" OR subject:"Action items" '
         'OR subject:"recording") newer_than:%dd' % max(1, days))
    threads = search_emails(q, max_results=15)
    out = []
    for t in threads[:10]:
        full = read_email(t["id"]) if t.get("id") else {}
        out.append({"id": t.get("id", ""), "subject": t.get("subject", ""),
                    "from": t.get("from", ""), "date": t.get("date", ""),
                    "snippet": t.get("snippet", ""), "body": (full or {}).get("body", ""),
                    "message_link": ""})
    return out
