"""HITL escalation seam: hand a permission the Policy cannot allow to a human.

Sutando's HITL Manager (sonichi/sutando `src/hitl/`) ingests RuntimeEvents
from `<workspace>/state/hitl/events/` and records the human's answer in the
requirement file under `<workspace>/state/hitl/requirements/`. This module
speaks that file contract and nothing else — no import of sutando, no
network — so a Worker on the same Mac can route an ACP `request_permission`
to the owner's chat card and read the click back.

Off unless `AGENT_CONNECT_HITL_WORKSPACE` names the workspace. A timeout, an
expired or cancelled card, or a chosen action outside the request's own
option list returns `None` — a refusal (the request's reject option), never
an allow. The guard that ties a card to its request names the Session the
request was asked in, because ACP tool-call ids are only unique within one:
without that, one room's click would answer another room's request.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

WORKSPACE_ENV = "AGENT_CONNECT_HITL_WORKSPACE"
TIMEOUT_ENV = "AGENT_CONNECT_HITL_TIMEOUT"
SCHEMA = "space.ag2.hitl.runtime_event.v1"
RUNTIME = "acp"
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_S = 1.0
#: The card was withdrawn. Sutando's `cancel()` / `expire()` transition the
#: status and keep whatever `chosen_action` the owner had clicked before the
#: card went stale, and the Manager renders such a card as "no longer
#: applicable" — so the click it still carries is not an answer here either.
REFUSED = {"cancelled", "expired"}
#: Every status after which no further answer will come. `resolved` is the
#: happy path and legitimately carries the chosen action.
TERMINAL = REFUSED | {"resolved"}


def configured(env: Optional[dict] = None) -> Optional[Path]:
    env = os.environ if env is None else env
    ws = env.get(WORKSPACE_ENV, "").strip()
    return Path(ws) if ws else None


def timeout_s(env: Optional[dict] = None) -> float:
    """The wait bound in seconds; the default for anything that is not one.

    `float()` accepts more than numbers: `"inf"`, `"Infinity"` and an
    overflowed `"1e400"` all read as infinity, and an infinite deadline is a
    Turn that never ends, blocked on a card nobody has to click. A bound that
    is not finite and non-negative is not a bound, so it is treated like
    `"abc"` — the default stands in. `"0"` is honoured: it means "do not wait".
    """
    env = os.environ if env is None else env
    try:
        value = float(env.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    if not math.isfinite(value) or value < 0:
        return DEFAULT_TIMEOUT_S
    return value


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:120]


def _tool_call_dict(tool_call: Any) -> dict:
    return tool_call if isinstance(tool_call, dict) else {}


def _option_id(option: Any) -> str:
    """One option's id as the card will show it. `""` for an option without one."""
    if not isinstance(option, dict):
        return ""
    raw = option.get("optionId") or option.get("option_id") or ""
    return str(raw) if raw else ""


def guard_for(request: Any, session: str = "") -> str:
    """The guard that ties a card to this request, and to this Session.

    `_find_decision` matches a requirement on its guard alone, so the guard is
    the whole of what keeps one request's answer from reaching another. ACP
    tool-call ids are scoped to a Session — two rooms can each be asking about
    `toolCallId="t1"` at once — so the Session is part of the guard, not just
    of the event that posted it. A request that brings no id gets a random one
    rather than a clock: two such requests in one millisecond would otherwise
    share a guard as well.
    """
    tc = _tool_call_dict(request.tool_call)
    ident = tc.get("toolCallId") or tc.get("tool_call_id") or tc.get("id") or ""
    ident = str(ident) if ident else uuid.uuid4().hex
    return f"acp:{_safe(session)}:{ident}"


def event_for(request: Any, session: str) -> dict:
    tc = _tool_call_dict(request.tool_call)
    title = str(tc.get("title") or tc.get("kind") or "tool call")
    options = []
    for o in request.options or []:
        oid = _option_id(o)
        if oid:
            options.append({"id": oid, "label": str(o.get("name") or oid), "kind": str(o.get("kind") or "")})
    return {
        "schema": SCHEMA,
        "session": session,
        "socket": "",
        "runtime": RUNTIME,
        "kind": "permission",
        "prompt": f"Agent wants to {title}",
        "guard": guard_for(request, session),
        "observed_ms": int(time.time() * 1000),
        # What is being asked, structurally: the Manager's policy reads `tool`.
        "subject": {"tool": str(tc.get("kind") or title), "input": json.dumps(tc.get("rawInput"), default=str)[:200]},
        "options": options,
    }


def _write_atomic(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ev-", suffix=".tmp", dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(body, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _find_decision(workspace: Path, guard: str) -> Optional[tuple]:
    """(chosen_action, status) for the requirement carrying `guard`, else None."""
    d = workspace / "state" / "hitl" / "requirements"
    if not d.is_dir():
        return None
    for p in d.glob("hitl_*.json"):
        try:
            req = json.loads(p.read_text()).get("requirement") or {}
        except (OSError, ValueError):
            continue
        if req.get("guard") == guard:
            return req.get("chosen_action"), str(req.get("status") or "")
    return None


async def escalate(
    request: Any,
    *,
    session: str,
    workspace: Path,
    timeout: float = DEFAULT_TIMEOUT_S,
    poll: float = DEFAULT_POLL_S,
    sleep: Callable = asyncio.sleep,
) -> Optional[str]:
    """Post the requirement, wait for the human's option id; None on timeout.

    Always leaves a tombstone so the Manager closes the card whichever way
    this ends. The caller maps None onto the request's reject option.
    """
    ev = event_for(request, session)
    guard = ev["guard"]
    path = workspace / "state" / "hitl" / "events" / f"{_safe(session)}-{_safe(guard)}.json"
    _write_atomic(path, ev)
    chosen: Optional[str] = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            found = _find_decision(workspace, guard)
            if found is not None:
                action, status = found
                # A withdrawn card first: it may still carry the click the
                # owner made before it went stale, and that click is not an
                # answer to anything any more. Then the answer, then the
                # remaining terminal state (resolved without an action).
                if status in REFUSED:
                    break
                if action:
                    chosen = str(action)
                    break
                if status in TERMINAL:
                    break
            await sleep(poll)
    finally:
        _write_atomic(path, {"schema": SCHEMA, "session": session, "guard": guard, "cleared": True})
    # The same normalisation the card was built with, so an option the owner
    # was shown is an option the owner can pick.
    valid = {_option_id(o) for o in (request.options or [])} - {""}
    return chosen if chosen is not None and chosen in valid else None
