"""HITL escalation seam: hand a permission the Policy cannot allow to a human.

Sutando's HITL Manager (sonichi/sutando `src/hitl/`) ingests RuntimeEvents
from `<workspace>/state/hitl/events/` and records the human's answer in the
requirement file under `<workspace>/state/hitl/requirements/`. This module
speaks that file contract and nothing else — no import of sutando, no
network — so a Worker on the same Mac can route an ACP `request_permission`
to the owner's chat card and read the click back.

Off unless `AGENT_CONNECT_HITL_WORKSPACE` names the workspace. A timeout is a
refusal (the request's reject option), never an allow.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

WORKSPACE_ENV = "AGENT_CONNECT_HITL_WORKSPACE"
TIMEOUT_ENV = "AGENT_CONNECT_HITL_TIMEOUT"
SCHEMA = "space.ag2.hitl.runtime_event.v1"
RUNTIME = "acp"
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_S = 1.0
TERMINAL = {"resolved", "cancelled", "expired"}


def configured(env: Optional[dict] = None) -> Optional[Path]:
    env = os.environ if env is None else env
    ws = env.get(WORKSPACE_ENV, "").strip()
    return Path(ws) if ws else None


def timeout_s(env: Optional[dict] = None) -> float:
    env = os.environ if env is None else env
    try:
        return float(env.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:120]


def _tool_call_dict(tool_call: Any) -> dict:
    return tool_call if isinstance(tool_call, dict) else {}


def guard_for(request: Any) -> str:
    tc = _tool_call_dict(request.tool_call)
    ident = tc.get("toolCallId") or tc.get("tool_call_id") or tc.get("id") or ""
    return f"acp:{ident}" if ident else f"acp:{int(time.time() * 1000)}"


def event_for(request: Any, session: str) -> dict:
    tc = _tool_call_dict(request.tool_call)
    title = str(tc.get("title") or tc.get("kind") or "tool call")
    options = []
    for o in request.options or []:
        if not isinstance(o, dict):
            continue
        oid = str(o.get("optionId") or o.get("option_id") or "")
        if oid:
            options.append({"id": oid, "label": str(o.get("name") or oid), "kind": str(o.get("kind") or "")})
    return {
        "schema": SCHEMA,
        "session": session,
        "socket": "",
        "runtime": RUNTIME,
        "kind": "permission",
        "prompt": f"Agent wants to {title}",
        "guard": guard_for(request),
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
                if action:
                    chosen = str(action)
                    break
                if status in TERMINAL:
                    break
            await sleep(poll)
    finally:
        _write_atomic(path, {"schema": SCHEMA, "session": session, "guard": guard, "cleared": True})
    valid = {o.get("optionId") or o.get("option_id") for o in (request.options or []) if isinstance(o, dict)}
    return chosen if chosen in valid else None
