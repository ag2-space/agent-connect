"""Held collaborator replies, and the owner's release (`agent_connect.result_guard`).

A held reply is written to `<workspace>/withheld/<review id>.json`, owner-only,
on the owner's machine, where the Worker runs, and the owner is asked privately,
in their DM with the agent, whether to post it (`agent_connect.questions`); the
question shows the reply. `watch` acts on the answer. When no private question
can be asked, the room message says how to act instead: reply `release` or
`discard` to it, or `release <review id>`. A room release is honoured only when
the broker attests the sender as `owner` and names a review held for this room.
Either way the stored reply must be unchanged.

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
#: Which question and which room message belong to a review. In memory only, so
#: the agent, which can write the workspace, cannot point a review elsewhere.
_QUESTION: dict = {}
_EVENT: dict = {}
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
         now: Optional[float] = None, cwd: str = "") -> Optional[str]:
    """Store a held reply; return its review id, or None if it could not be stored."""
    now = time.time() if now is None else now
    review_id = "wr_" + secrets.token_hex(8)
    record = {"review_id": review_id, "room": room, "body": body, "why": list(why),
              "created_at": now, "expires_at": now + TTL_S, "cwd": cwd,
              "process": _PROCESS, "seal": _seal(review_id, room, body)}
    try:
        _write(directory(env) / f"{review_id}.json", record)
    except OSError:
        return None
    return review_id


def bind_event(review_id: str, event_id: str, env: Optional[Mapping[str, str]] = None) -> None:
    """Remember which room message announced the review, so a reply to it can release."""
    _EVENT[review_id] = event_id


def bind_question(review_id: str, question_id: str, env: Optional[Mapping[str, str]] = None) -> None:
    _QUESTION[review_id] = question_id


OPTIONS = (("release", "Post it to the room"), ("discard", "Discard it"))


def owner_prompt(review_id: str, body: str, why, asked_by: str, room: str) -> str:
    """The private question: what was held, why, and the reply with secrets masked."""
    from .result_guard import mask

    head = (f"A reply to {asked_by} in {room} was withheld ({review_id}): it contained "
            f"{', '.join(why)}. Post it to the room?\n\nThe reply, with secrets masked "
            f"(Post it sends the original):\n\n")
    body = mask(body)
    room_left = 2000 - len(head)
    if len(body) > room_left:
        tail = f"\n\n… (cut; the whole reply is in the Worker's workspace, withheld/{review_id}.json)"
        body = body[: max(0, room_left - len(tail))] + tail
    return head + body


def review_message(review_id: str, why, asked: bool = False) -> str:
    """What the room is told: no content and no secret type, and how the owner acts."""
    if asked:
        return (f"🔒 agent-connect: this reply was withheld for the owner's review ({review_id}). "
                f"The owner has been asked privately whether to post it. It expires in 24 hours.")
    return (
        f"🔒 agent-connect: this reply was withheld for the owner's review ({review_id}).\n\n"
        f"Owner: read it on the Worker's machine, in the Worker's workspace under "
        f"`withheld/{review_id}.json`, then reply to this message "
        f"with `release` to post it or `discard` to delete it. It expires in 24 hours."
    )


async def watch(ops, poll: float = 15.0, sleep=None, rounds: Optional[int] = None) -> None:
    """Act on the owner's private answers until cancelled (`rounds` bounds it for tests)."""
    import asyncio
    sleep = sleep or asyncio.sleep
    while rounds is None or rounds > 0:
        await check_answers(ops)
        if rounds is not None:
            rounds -= 1
        await sleep(poll)


async def check_answers(ops, env: Optional[Mapping[str, str]] = None) -> None:
    questions = getattr(ops, "questions", None)
    if questions is None:
        return
    sweep(env)
    for review_id, question_id in list(_QUESTION.items()):
        path = directory(env) / f"{review_id}.json"
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            _QUESTION.pop(review_id, None)
            continue
        if record.get("review_id") != review_id:
            _QUESTION.pop(review_id, None)
            _remove(path)
            continue
        status, option = await questions.answer(question_id)
        if status in ("expired", "cancelled", "answered"):
            _QUESTION.pop(review_id, None)
        if status in ("expired", "cancelled") or (status == "answered" and option != "release"):
            _remove(path)
        elif status == "answered":
            _remove(path)
            if hmac.compare_digest(record.get("seal", ""),
                                   _seal(record["review_id"], record["room"], record["body"])):
                await post_released(ops, record)


async def post_released(ops, record: dict) -> None:
    """Post a released reply the way `complete` would: files first, then the text."""
    from ag2_relay_client import markers

    parsed = markers.parse(record["body"])
    notes = []
    for action in parsed.actions:
        if action.kind != "attach":
            continue
        try:
            result = await ops.upload(record["room"], action.value, record.get("cwd") or None)
            if not getattr(result, "ok", False):
                notes.append(f"[attachment not sent: {getattr(result, 'reason', '') or action.value}]")
        except Exception:  # noqa: BLE001 — said, not dropped
            notes.append(f"[attachment not sent: {action.value}]")
    text = "\n\n".join(part for part in (parsed.body.strip(), *notes, RELEASED_NOTE) if part)
    try:
        await ops.message(record["room"], text)
    except Exception:  # noqa: BLE001 — logged by the caller's Room Ops
        pass


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
    _EVENT.pop(record["review_id"], None)
    _QUESTION.pop(record["review_id"], None)
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
    return set(_EVENT.values()) - {""}


def _find(room: str, named: Optional[str], reply_to: str, env):
    for path in _entries(env):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("room") != room:
            continue
        review = record.get("review_id")
        if review != path.stem:
            continue
        if (named and review == named) or \
                (not named and reply_to and _EVENT.get(review) == reply_to):
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
