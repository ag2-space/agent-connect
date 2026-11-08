"""Private yes/no questions to the agent's owner, in the owner's DM with the agent.

The relay client asks s.space's `room.question.*` Actions (`ag2_relay_client.
questions`); this awaits them. Every failure is `None`: no private DM, no MCP
access, an error. A caller treats that as "the owner could not be asked", which
is never a yes.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Mapping, Optional, Sequence, Tuple

from .offthread import in_daemon_thread

#: How long a Turn waits for the owner to allow an action, in seconds.
TIMEOUT_ENV = "AGENT_CONNECT_APPROVAL_TIMEOUT"
DEFAULT_TIMEOUT_S = 600.0
POLL_S = 2.0
PROMPT_MAX = 2000
ANSWERED = "answered"
CLOSED = ("expired", "cancelled")


def timeout_s(env: Optional[Mapping[str, str]] = None) -> float:
    env = os.environ if env is None else env
    try:
        value = float(env.get(TIMEOUT_ENV, DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return value if 0 <= value < float("inf") else DEFAULT_TIMEOUT_S


def clip(text: str, limit: int = PROMPT_MAX) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class OwnerQuestions:
    """The relay client's `questions`, awaited from the event loop."""

    def __init__(self, sync):
        self.sync = sync

    async def ask(self, prompt: str, options: Sequence[Tuple[str, str]], *,
                  expires_in_s: int, operation_id: str) -> Optional[str]:
        try:
            return await in_daemon_thread(
                lambda: self.sync.ask(clip(prompt), list(options),
                                      expires_in_s=int(max(60, expires_in_s)),
                                      operation_id=operation_id))
        except Exception:  # noqa: BLE001 — unasked, never a yes
            return None

    async def answer(self, question_id: str) -> Tuple[str, str]:
        """(status, chosen option) now; ("", "") when it cannot be read."""
        try:
            got = await in_daemon_thread(self.sync.inspect, question_id)
        except Exception:  # noqa: BLE001
            got = None
        if got is None:
            return "", ""
        return str(got.status or ""), str(got.option or "") if got.status == ANSWERED else ""

    async def wait(self, question_id: str, timeout: float, poll: float = POLL_S,
                   clock=time.monotonic, sleep=asyncio.sleep) -> str:
        """The chosen option, or "" on expiry, cancellation or timeout."""
        deadline = clock() + timeout
        while clock() < deadline:
            status, option = await self.answer(question_id)
            if status == ANSWERED:
                return option
            if status in CLOSED:
                return ""
            await sleep(poll)
        return ""


def questions_for(client) -> Optional[OwnerQuestions]:
    sync = getattr(client, "questions", None)
    return OwnerQuestions(sync) if sync is not None else None
