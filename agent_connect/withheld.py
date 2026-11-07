"""Held collaborator replies, and the owner's release (`agent_connect.result_guard`).

A held reply is written to `<workspace>/withheld/<review id>.json`, owner-only,
on the owner's machine, where the Worker runs. The room is told how to review
it. The owner releases or discards it by replying `release` or `discard` to that
message; `release <review id>` anywhere in the room works too. A release is
honoured only when the broker attests the sender as `owner`, it names a review
held for this room, and the stored reply is unchanged.

The integrity key lives in this process only, so a reply the agent edits on disk
after it was held is refused, and a Worker restart invalidates pending reviews.
Reviews expire after `TTL_S`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .sessions import workspace_dir

DIR_NAME = "withheld"
TTL_S = 24 * 3600
OWNER = "owner"
_KEY = secrets.token_bytes(32)
#: Which Worker process held a review; another process's cannot be verified.
_PROCESS = secrets.token_hex(8)
_COMMAND = re.compile(r"^\s*(release|discard)(?:\s+(wr_[0-9a-f]{16}))?\s*[.!]?\s*$", re.IGNORECASE)

RELEASED_NOTE = "✅ agent-connect: the owner released this withheld reply."
DISCARDED = "🗑 agent-connect: the withheld reply was discarded."
NOT_FOUND = ("agent-connect: no withheld reply matches that. It may have expired, been "
             "handled already, or been held before the Worker restarted.")
TAMPERED = ("agent-connect: that withheld reply changed on disk after it was held, so it "
            "was not posted and has been deleted.")


def directory(env: Optional[Mapping[str, str]] = None) -> Path:
    return workspace_dir(env) / DIR_NAME


def _seal(review_id: str, room: str, body: str) -> str:
    return hmac.new(_KEY, json.dumps([review_id, room, body]).encode(), hashlib.sha256).hexdigest()


def hold(room: str, body: str, why, env: Optional[Mapping[str, str]] = None,
         now: Optional[float] = None) -> Optional[str]:
    """Store a held reply; return its review id, or None if it could not be stored."""
    now = time.time() if now is None else now
    review_id = "wr_" + secrets.token_hex(8)
    record = {"review_id": review_id, "room": room, "body": body, "why": list(why),
              "created_at": now, "expires_at": now + TTL_S, "event_id": "",
              "process": _PROCESS, "seal": _seal(review_id, room, body)}
    try:
        _write(directory(env) / f"{review_id}.json", record)
    except OSError:
        return None
    return review_id


def bind_event(review_id: str, event_id: str, env: Optional[Mapping[str, str]] = None) -> None:
    """Record which room message announced the review, so a reply to it can release."""
    path = directory(env) / f"{review_id}.json"
    try:
        record = json.loads(path.read_text())
        record["event_id"] = event_id
        _write(path, record)
    except (OSError, ValueError):
        pass


def review_message(review_id: str, why) -> str:
    """What the room is told: no content and no secret type, and how the owner acts."""
    where = directory() / f"{review_id}.json"
    return (
        f"🔒 agent-connect: this reply was withheld for the owner's review ({review_id}).\n\n"
        f"Owner: read it on the Worker's machine in `{where}`, then reply to this message "
        f"with `release` to post it or `discard` to delete it. It expires in 24 hours."
    )


@dataclass(frozen=True)
class Decision:
    """The answer to an owner's `release`/`discard`; `body` is what to post."""
    body: str


def command(text: str) -> Optional[tuple]:
    match = _COMMAND.match(text or "")
    return (match.group(1).lower(), match.group(2)) if match else None


def decide(ctx, env: Optional[Mapping[str, str]] = None,
           now: Optional[float] = None) -> Optional[Decision]:
    """An owner's release or discard, or None when this Turn is not one."""
    if ctx.access_tier != OWNER:
        return None
    parsed = command(ctx.prompt)
    if parsed is None:
        return None
    verb, named = parsed
    reply_to = getattr(ctx, "reply_to_event", "")
    if not named and not reply_to:
        return None
    sweep(env, now)
    record, path = _find(ctx.room, named, reply_to, env)
    if record is None:
        known = named or reply_to in _announced(env) or getattr(ctx, "reply_to_me", False)
        return Decision(NOT_FOUND) if known else None
    _remove(path)
    if verb == "discard":
        return Decision(DISCARDED)
    if not hmac.compare_digest(record.get("seal", ""),
                               _seal(record["review_id"], record["room"], record["body"])):
        return Decision(TAMPERED)
    return Decision(f"{record['body']}\n\n{RELEASED_NOTE}")


def sweep(env: Optional[Mapping[str, str]] = None, now: Optional[float] = None) -> None:
    """Delete expired reviews and ones held by another Worker process."""
    now = time.time() if now is None else now
    for path in _entries(env):
        try:
            record = json.loads(path.read_text())
            stale = record.get("expires_at", 0) < now or record.get("process") != _PROCESS
        except (OSError, ValueError):
            stale = True
        if stale:
            _remove(path)


def _announced(env) -> set:
    found = set()
    for path in _entries(env):
        try:
            found.add(json.loads(path.read_text()).get("event_id", ""))
        except (OSError, ValueError):
            pass
    return found - {""}


def _find(room: str, named: Optional[str], reply_to: str, env):
    for path in _entries(env):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("room") != room:
            continue
        if (named and record.get("review_id") == named) or \
                (not named and reply_to and record.get("event_id") == reply_to):
            return record, path
    return None, None


def _entries(env):
    d = directory(env)
    return sorted(d.glob("wr_*.json")) if d.is_dir() else []


def _remove(path: Optional[Path]) -> None:
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".wr-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
