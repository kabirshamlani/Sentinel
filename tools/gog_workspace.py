"""Google Workspace via the `gog` CLI — Gmail, Google Chat, Calendar, Drive.

Reuses the owner's already-authorized gog session (owner@example.com,
all scopes) instead of a separate OAuth token — one credential, works today.

Reads are direct (Tier R). SENDS never happen here — the chat/verticals build a
broker proposal and only the approved executor calls the send helpers below, so
every outbound email/chat message is a Telegram approval you tap first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil

logger = logging.getLogger("gog-workspace")

import os as _os
ACCOUNT = _os.environ.get("SENTINEL_GOOGLE_ACCOUNT", "owner@example.com")


def available() -> bool:
    return shutil.which("gog") is not None


async def _gog(*args, timeout: int = 45, json_out: bool = True) -> dict | list | str:
    """Run a gog command; return parsed JSON (or raw text). Never raises."""
    if not available():
        return {"error": "gog CLI not installed"}
    cmd = ["gog", "-a", ACCOUNT, *args]
    if json_out:
        cmd.append("-j")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = out.decode(errors="ignore").strip()
        if proc.returncode != 0:
            return {"error": (err.decode(errors="ignore") or "gog failed")[:300]}
        if not json_out:
            return text
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError:
            return {"raw": text[:4000]}
    except asyncio.TimeoutError:
        return {"error": f"gog timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


# ---------- Gmail (read) ----------

async def search_emails(query: str = "is:unread", max_results: int = 15) -> list[dict]:
    r = await _gog("gmail", "search", query, "--max", str(max_results))
    if isinstance(r, dict):
        return r.get("threads", []) if "threads" in r else ([] if "error" in r else [r])
    return r if isinstance(r, list) else []


async def read_email(message_id: str) -> dict:
    r = await _gog("gmail", "get", message_id)
    return r if isinstance(r, dict) else {"raw": str(r)}


def _walk_attachments(payload: dict, out: list) -> None:
    fn = payload.get("filename", "")
    body = payload.get("body", {}) or {}
    aid = body.get("attachmentId")
    if fn and aid:
        out.append({"filename": fn, "attachment_id": aid,
                    "mime": payload.get("mimeType", ""),
                    "size": body.get("size", 0)})
    for part in payload.get("parts", []) or []:
        _walk_attachments(part, out)


async def list_attachments(message_id: str) -> list[dict]:
    """Return [{filename, attachment_id, mime, size}] for a message. The find→
    download→send chain (e.g. payslips) calls this then download_attachment."""
    r = await read_email(message_id)
    payload = (r.get("message", {}) or {}).get("payload", {}) or {}
    out: list[dict] = []
    _walk_attachments(payload, out)
    return out


async def download_attachment(message_id: str, attachment_id: str,
                              out_dir: str = "", filename: str = "") -> dict:
    """Download one attachment to disk; returns {path} or {error}. Used by the
    chat agent to fetch email files (payslips, docs) then send them to Telegram."""
    import os, tempfile, re
    out_dir = out_dir or os.path.join(tempfile.gettempdir(), "sentinel_attachments")
    os.makedirs(out_dir, exist_ok=True)
    # SECURITY: the filename comes from the email (attacker-controllable). Reduce
    # to a safe basename so it can't traverse out of out_dir (e.g. an attachment
    # named "../../.ssh/authorized_keys"). Strip path separators + odd chars.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename or "")).lstrip(".")[:120]
    # gog writes the attachment; capture where. It prints/saves by attachment id —
    # ask for JSON to learn the saved path, else fall back to a named file.
    dest = os.path.join(out_dir, safe) if safe else out_dir
    filename = safe
    r = await _gog("gmail", "attachment", message_id, attachment_id,
                   "--out", dest, json_out=True, timeout=90)
    if isinstance(r, dict):
        path = r.get("path") or r.get("file") or (dest if os.path.isfile(dest) else "")
        if r.get("error"):
            # retry without --out (some gog builds save to cwd/default), then locate
            r2 = await _gog("gmail", "attachment", message_id, attachment_id,
                            json_out=True, timeout=90)
            path = (r2.get("path") or r2.get("file") or "") if isinstance(r2, dict) else ""
        if path and os.path.isfile(path):
            return {"path": path, "filename": os.path.basename(path)}
    # last resort: if a file with the expected name landed in out_dir
    if filename and os.path.isfile(os.path.join(out_dir, filename)):
        return {"path": os.path.join(out_dir, filename), "filename": filename}
    return {"error": f"attachment download failed: {str(r)[:200]}"}


async def find_and_download(query: str, max_msgs: int = 5, out_dir: str = "") -> list[dict]:
    """Convenience for the chat agent: search emails, download every attachment
    found, return [{path, filename, from, subject}]. Powers 'send me my payslips'."""
    out: list[dict] = []
    seen_names: set[str] = set()          # dedupe identical filenames across threads
    threads = await search_emails(query, max_msgs)
    for t in threads:
        mid = t.get("id") or t.get("threadId")
        if not mid:
            continue
        for att in await list_attachments(mid):
            if att["filename"] in seen_names:
                continue
            got = await download_attachment(mid, att["attachment_id"], out_dir,
                                            att["filename"])
            if got.get("path"):
                seen_names.add(att["filename"])
                out.append({**got, "from": t.get("from", ""),
                            "subject": t.get("subject", "")})
    return out


# ---------- Gmail (send — called ONLY by an approved broker action) ----------

async def send_email(to: str, subject: str, body: str,
                     attachments: list[str] | None = None) -> dict:
    # gog supports repeatable --attach (found 2026-08-15 after the resume
    # email went out single-part text/plain with NO attachment — the executor
    # simply never passed files through).
    args = ["gmail", "send", "--to", to, "--subject", subject, "--body", body]
    for a in attachments or []:
        args += ["--attach", str(a)]
    r = await _gog(*args, json_out=True, timeout=120)
    return r if isinstance(r, dict) else {"status": "sent", "raw": str(r)}


async def create_draft(to: str, subject: str, body: str) -> dict:
    r = await _gog("gmail", "drafts", "create", "--to", to, "--subject", subject,
                   "--body", body, timeout=60)
    return r if isinstance(r, dict) else {"status": "draft_created", "raw": str(r)}


async def list_drafts(max_results: int = 50) -> list[dict]:
    r = await _gog("gmail", "drafts", "list", "--max", str(max_results))
    if isinstance(r, dict):
        for k in ("drafts", "result", "messages", "items"):
            if isinstance(r.get(k), list):
                return r[k]
        return []
    return r if isinstance(r, list) else []


async def get_draft(draft_id: str) -> dict:
    r = await _gog("gmail", "drafts", "get", draft_id)
    return r if isinstance(r, dict) else {}


async def delete_draft(draft_id: str) -> dict:
    """Permanently delete a draft. gog refuses without --force (drafts are NOT trashed,
    they're gone for good — hence the explicit force here for non-interactive deletion)."""
    r = await _gog("gmail", "drafts", "delete", draft_id, "--force", timeout=45)
    return r if isinstance(r, dict) else {"status": "deleted", "id": draft_id}


async def clear_junk_drafts(dry_run: bool = False) -> dict:
    """Delete EMPTY/junk drafts (no recipient AND no body) — the leftovers from old
    over-drafting runs that the owner kept seeing. Only removes genuinely empty ones; any
    draft with real content is left untouched. Returns counts + what was removed."""
    drafts = await list_drafts(100)
    removed, kept = [], 0
    for d in drafts:
        did = d.get("id") or d.get("draftId") or ""
        if not did:
            continue
        full = await get_draft(did)
        msg = full.get("message", full) if isinstance(full.get("message"), dict) else full
        h = msg.get("headers", {}) if isinstance(msg.get("headers"), dict) else {}
        to = (h.get("to") or msg.get("to") or d.get("to") or "").strip()
        subject = (h.get("subject") or msg.get("subject") or "").strip()
        body = (msg.get("body") or msg.get("snippet") or "").strip()
        is_junk = not body and not to and not subject
        if is_junk:
            if not dry_run:
                await delete_draft(did)
            removed.append(did)
        else:
            kept += 1
    return {"removed": len(removed), "kept": kept, "ids": removed, "dry_run": dry_run}


# ---------- Google Chat (read) ----------

async def list_spaces(max_results: int = 50) -> list[dict]:
    r = await _gog("chat", "spaces", "list", "--max", str(max_results))
    if isinstance(r, dict):
        return r.get("spaces", [])
    return r if isinstance(r, list) else []


async def read_space_messages(space: str, max_results: int = 20) -> list[dict]:
    # --order createTime desc is LOAD-BEARING: gog defaults to OLDEST-first,
    # so a capped read pages from the beginning of history — in a long-lived
    # DM that returned Jan-2026 messages as "the latest" and made all recent
    # chat invisible (found live 2026-08-14 via "what did Riley say in DM").
    # Fetch the newest N, then reverse so consumers still see oldest-first.
    r = await _gog("chat", "messages", "list", space, "--max", str(max_results),
                   "--order", "createTime desc")
    if isinstance(r, dict):
        msgs = r.get("messages", [])
    else:
        msgs = r if isinstance(r, list) else []
    if isinstance(msgs, list):
        msgs = list(reversed(msgs))
    return await resolve_senders(msgs)


async def read_dm(person: str, max_results: int = 20) -> list[dict]:
    r = await _gog("chat", "dm", "read", person, "--max", str(max_results))
    if isinstance(r, dict):
        msgs = r.get("messages", r.get("raw", []))
    else:
        msgs = r if isinstance(r, list) else []
    return await resolve_senders(msgs) if isinstance(msgs, list) else msgs


# ---------- Google Chat sender names (People API, cached) ----------

_people_cache: dict[str, str] = {}


async def resolve_user(user_id: str) -> str:
    """Resolve a Chat sender 'users/<id>' to 'Name <email>' via the People API.

    Cached in memory and kv so each id costs one API call ever. Returns ''
    for profiles the account can't see (external users) — cached too, so we
    don't retry them on every read.
    """
    if not isinstance(user_id, str) or not user_id.startswith("users/"):
        return ""
    if user_id in _people_cache:
        return _people_cache[user_id]
    from conductor import kv
    disk = kv.get("gchat_people_cache", {}) or {}
    if user_id in disk:
        _people_cache[user_id] = disk[user_id]
        return disk[user_id]
    r = await _gog("people", "get", "people/" + user_id.split("/", 1)[1])
    label = ""
    if isinstance(r, dict):
        p = r.get("person") or {}
        names = [n.get("displayName") for n in p.get("names", []) if n.get("displayName")]
        emails = [e.get("value") for e in p.get("emailAddresses", []) if e.get("value")]
        name, email = (names[0] if names else ""), (emails[0] if emails else "")
        label = f"{name} <{email}>" if name and email else (name or email)
    _people_cache[user_id] = label
    disk[user_id] = label
    kv.put("gchat_people_cache", disk)
    return label


def _sender_uid(m: dict) -> str:
    """users/<id> from either sender shape (plain string OR {name: ...} dict —
    gog emits both across versions; the string-only assumption left every
    dict-shaped sender unresolved)."""
    s = m.get("sender")
    if isinstance(s, dict):
        s = s.get("name", "")
    return s if isinstance(s, str) and s.startswith("users/") else ""


async def resolve_senders(msgs: list[dict], cap: int = 12) -> list[dict]:
    """Add sender_name to chat messages in place (unique senders, capped)."""
    uniq: list[str] = []
    for m in msgs if isinstance(msgs, list) else []:
        s = _sender_uid(m) if isinstance(m, dict) else ""
        if s and s not in uniq:
            uniq.append(s)
    names = {s: await resolve_user(s) for s in uniq[:cap]}
    for m in msgs if isinstance(msgs, list) else []:
        if isinstance(m, dict):
            n = names.get(_sender_uid(m))
            if n:
                m["sender_name"] = n
    return msgs


# ---------- Google Chat (send — approved actions only) ----------

async def send_chat(space_or_person: str, message: str) -> dict:
    r = await _gog("chat", "messages", "send", space_or_person, "--text", message, timeout=45)
    return r if isinstance(r, dict) else {"status": "sent", "raw": str(r)}


# ---------- Calendar (read) ----------

def _norm_event(e: dict) -> dict:
    """Flatten start/end from gog's raw {'dateTime': ...} / {'date': ...}
    shape to a plain ISO string, mirroring workspace_tools._format_event().
    Without this, consumers that do str(e.get('start', ''))[:N] slice the
    Python dict repr instead of the actual timestamp."""
    if not isinstance(e, dict):
        return e
    out = dict(e)
    for key in ("start", "end"):
        v = out.get(key)
        if isinstance(v, dict):
            out[key] = v.get("dateTime", v.get("date", ""))
    return out


async def upcoming_events(max_results: int = 20) -> list[dict]:
    r = await _gog("calendar", "events", "list", "--max", str(max_results))
    if isinstance(r, dict):
        events = r.get("events", r.get("items", []))
    else:
        events = r if isinstance(r, list) else []
    # gog error payloads can put strings in the list — a str reaching
    # _norm_event was the "'str' object has no attribute 'get'" warning
    # that fired 67× in the daemon log.
    return [_norm_event(e) for e in events if isinstance(e, dict)]


# ---------- convenience: "what did I miss" ----------

async def whats_new(hours: int = 24) -> dict:
    """A quick unread-mail + recent-chat snapshot for briefings."""
    emails = await search_emails("is:unread", 10)
    spaces = await list_spaces(20)
    return {"unread_emails": emails, "chat_spaces": spaces}
