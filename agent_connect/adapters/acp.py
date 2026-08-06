"""The ACP Adapter: one Adapter for any Local Agent that speaks ACP.

The Worker is the ACP Client and the Local Agent — usually through the
`@agentclientprotocol/claude-agent-acp` bridge — is the ACP Agent. This module
is the translation layer between the two vocabularies: a `TurnContext` in, a
stream of `agent_connect.events` out, ACP in the middle.

**Owner-tier only, and that is the design.** Every other Adapter is confined by
the operating system: the Sandbox derived from the Access Tier is passed to a
CLI that enforces it, and an agent that declines to co-operate is still stopped.
ACP has no such thing. The Local Agent performs its own file access and the only
lever left is answering its `session/request_permission` calls by policy — which
binds an agent that asks and does nothing whatsoever to one that does not.
Rather than ship a cooperative imitation of a guarantee that used to be real,
a Task at any other Tier is refused at the top of `turn` and never reaches ACP.

That refusal is in exactly one place, deliberately: lifting it later (once there
is real confinement around the bridge process) is a single change, and there is
no second path into this Adapter that could forget it. It reads `ctx.access_tier`
and nothing else — a field the Worker's parser establishes and the sender cannot
write, since a duplicated `access_tier` header already fails closed to "other".

**Configuration here is deliberately minimal.** The ACP Agent command is given
explicitly, in the existing `AGENT_CONNECT_*` style:

    AGENT_CONNECT_ADAPTER=acp
    AGENT_CONNECT_ACP_COMMAND="npx @agentclientprotocol/claude-agent-acp"
    AGENT_CONNECT_ACP_MODE=default        # optional, see below

Presets, version pinning and a friendly startup check are ticket 05's; this
Adapter only promises that a missing or mistyped command produces a sentence
rather than a traceback.

`AGENT_CONNECT_ACP_MODE` is optional because the mode ids are the ACP Agent's to
name. What matters is that the Session runs in the mode that *routes* permission
requests to the Worker rather than one that suppresses them — which is what
every bridge's default mode does, so the default is to leave it alone. The
variable exists for an agent whose default is something else.
"""
from __future__ import annotations

import asyncio
import os
import shlex
from typing import AsyncIterator, List, Optional, Sequence

from ..acp.core import AcpClient, AcpError, Update
from ..acp.policy import WorkingDirectoryPolicy
from ..events import (
    CANCELLED,
    COMPLETED,
    FAILED,
    REFUSED,
    TOKEN_LIMIT,
    Done,
    MessageChunk,
    PermissionAsked,
    Plan,
    Thinking,
    ToolFinished,
    ToolStarted,
    TurnContext,
    TurnEvent,
)

#: The one Access Tier this Adapter serves. See the module docstring: this is
#: the single point the whole restriction lives at.
OWNER = "owner"

COMMAND_ENV = "AGENT_CONNECT_ACP_COMMAND"
MODE_ENV = "AGENT_CONNECT_ACP_MODE"

REFUSAL = (
    "I only answer my owner over this connection.\n\n"
    "This agent is driven through the Agent Client Protocol, which — unlike the "
    "other ways agent-connect runs a local agent — gives the operating system no "
    "say in what the agent may touch. The only limit available is the agent "
    "asking permission and being told no, and an agent that does not ask is not "
    "stopped by it. Rather than offer a limit that only looks like one, "
    "agent-connect does not run this connection for anyone but the person who "
    "registered the agent."
)

#: ACP's stop reasons, mapped onto ours. Anything unrecognised is `FAILED` on
#: purpose: a stop reason added upstream must not read as a completed answer.
STOP_REASONS = {
    "end_turn": COMPLETED,
    "cancelled": CANCELLED,
    "refusal": REFUSED,
    "max_tokens": TOKEN_LIMIT,
    "max_turn_requests": FAILED,
}

#: ACP tool kinds, mapped onto the coarse `ToolStarted.action` classification.
TOOL_ACTIONS = {
    "read": "read", "edit": "edit", "delete": "edit", "move": "edit",
    "execute": "execute", "search": "search", "fetch": "other",
    "think": "other", "other": "other",
}


def command_from_env(env: Optional[dict] = None) -> List[str]:
    """The ACP Agent's command line, as the operator wrote it.

    Split the way a shell would, so `npx @scope/pkg --flag` works as typed,
    without inviting a shell to interpret anything else in it.
    """
    raw = (env if env is not None else os.environ).get(COMMAND_ENV, "").strip()
    if not raw:
        raise AcpError(
            f"set {COMMAND_ENV} to the ACP Agent's command line, e.g.\n"
            f'    export {COMMAND_ENV}="npx @agentclientprotocol/claude-agent-acp"'
        )
    return shlex.split(raw)


def preamble(ctx: TurnContext) -> str:
    """What the Local Agent is told about the situation it is answering in.

    Written here rather than inherited from the shim's sandbox preamble, which
    describes an operating-system Sandbox this Adapter does not have. Saying
    "this run's sandbox is workspace-write" over ACP would be stating a
    confinement that is not there.
    """
    who = ctx.sender_name or "the owner"
    where = f" in {ctx.room_name}" if ctx.room_name else ""
    return (
        f"[agent-connect] {who} is asking you this{where}, through a chat room. "
        "Answer in chat: prose, no more than a few short paragraphs unless asked "
        "for more. You are working in the directory this session was opened in; "
        "file operations outside it will be refused when you ask for them.\n\n"
    )


def events_for(update: Update) -> List[TurnEvent]:
    """One ACP `session/update` notification, in our vocabulary.

    Returns a list because most updates map onto one event and some onto none:
    an update this Adapter has no word for is dropped rather than guessed at.
    """
    raw = update.raw or {}
    if update.kind == "agent_message_chunk":
        return [MessageChunk(text=update.text)] if update.text else []
    if update.kind == "agent_thought_chunk":
        return [Thinking(text=update.text)] if update.text else []
    if update.kind == "plan":
        entries = [
            {"title": e.get("content") or e.get("title") or "", "status": e.get("status") or ""}
            for e in (raw.get("entries") or [])
            if isinstance(e, dict)
        ]
        return [Plan(entries=entries)]
    if update.kind in ("tool_call", "tool_call_update"):
        return [_tool_event(raw)]
    return []


def _tool_event(raw: dict) -> TurnEvent:
    """A tool update as a start or an end, by the status it carries.

    ACP reports one tool call's whole life through the same notification kind,
    so the status is the only thing that says which of our two events it is.
    """
    tool_id = raw.get("toolCallId") or ""
    title = raw.get("title") or ""
    status = raw.get("status") or ""
    if status in ("completed", "failed"):
        return ToolFinished(
            tool_id=tool_id, title=title,
            status=COMPLETED if status == "completed" else FAILED,
            detail={"raw_status": status},
        )
    return ToolStarted(
        tool_id=tool_id, title=title,
        action=TOOL_ACTIONS.get(raw.get("kind") or "", "other"),
        detail={"raw_status": status},
    )


class AcpAdapter:
    """Drives one Turn over ACP, for the owner, under a Permission Policy.

    Registered as an object rather than a module because it already speaks the
    event-shaped contract: the Adapter registry passes anything exposing `turn`
    through unwrapped.
    """

    name = "acp"

    def __init__(self, command: Optional[Sequence[str]] = None, mode: Optional[str] = None):
        # Injectable so a test does not have to set process environment; `None`
        # means "read the environment when the Turn runs", which is what the
        # Worker gets.
        self._command = list(command) if command else None
        self._mode = mode

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<AcpAdapter {self._command or 'from ' + COMMAND_ENV}>"

    def command(self) -> List[str]:
        return list(self._command) if self._command else command_from_env()

    def mode(self) -> str:
        return self._mode if self._mode is not None else os.environ.get(MODE_ENV, "").strip()

    async def turn(self, ctx: TurnContext) -> AsyncIterator[TurnEvent]:
        """One Turn: refuse, or run it over ACP and report what happened."""
        # --- the whole non-owner restriction, in one place -----------------
        if ctx.access_tier != OWNER:
            yield Done(reason=REFUSED, text=REFUSAL)
            return

        try:
            command = self.command()
        except AcpError as exc:
            yield Done(reason=FAILED, text="", note=f"agent-connect: {exc}")
            return

        cwd = ctx.cwd or os.getcwd()
        policy = WorkingDirectoryPolicy(cwd)
        queue: asyncio.Queue = asyncio.Queue()

        def on_update(update: Update) -> None:
            for event in events_for(update):
                queue.put_nowait(event)

        def on_permission(request):
            """The Permission Policy decides, and the room gets to hear about it.

            The event is emitted after the decision and carries it, because a
            rejected request is the interesting one: without it a blocked agent
            is indistinguishable from a lazy one.
            """
            decision = policy.decide(request)
            queue.put_nowait(
                PermissionAsked(
                    title=decision.title,
                    allowed=decision.allowed,
                    reason=decision.reason,
                    detail={"paths": list(decision.paths)},
                )
            )
            return decision.option_id

        async def run():
            async with AcpClient.spawn(
                command, cwd=cwd, on_update=on_update, permission_handler=on_permission
            ) as client:
                await client.initialize()
                session_id = await client.new_session(cwd=cwd)
                mode = self.mode()
                if mode:
                    await client.set_session_mode(session_id, mode)
                return await client.prompt(session_id, preamble(ctx) + ctx.prompt)

        work = asyncio.ensure_future(run())
        chunks: List[str] = []
        refused: List[str] = []
        try:
            async for event in _drain(queue, work):
                if isinstance(event, MessageChunk):
                    chunks.append(event.text)
                elif isinstance(event, PermissionAsked) and not event.allowed:
                    refused.append(f"{event.title} — {event.reason}")
                yield event
            result = work.result()
        except asyncio.CancelledError:
            raise
        except AcpError as exc:
            yield Done(reason=FAILED, text="".join(chunks),
                       note=f"agent-connect: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 — one Turn's failure is its own
            yield Done(reason=FAILED, text="".join(chunks),
                       note=f"agent-connect: the ACP Turn failed: {exc}")
            return
        finally:
            if not work.done():
                work.cancel()

        yield Done(
            reason=STOP_REASONS.get(result.stop_reason, FAILED),
            text="".join(chunks),
            note=_note(result.stop_reason, refused),
        )


async def _drain(queue: asyncio.Queue, work: asyncio.Future) -> AsyncIterator[TurnEvent]:
    """Events as they arrive, until the Turn ends — then the ones still queued.

    The callbacks the core takes are pushed to rather than pulled from, so the
    queue is what turns them back into a stream. Draining after `work` finishes
    matters: the last message chunk of a Turn is often still in flight when the
    `prompt` call returns, and dropping it would truncate the answer.
    """
    while True:
        item = asyncio.ensure_future(queue.get())
        done, _ = await asyncio.wait({item, work}, return_when=asyncio.FIRST_COMPLETED)
        if item in done:
            yield item.result()
            continue
        item.cancel()
        break
    while not queue.empty():
        yield queue.get_nowait()
    work.result()  # re-raises whatever the Turn failed with


def _note(stop_reason: str, refused: List[str]) -> str:
    """The operator- and room-facing footnote for a Turn that was not plain."""
    lines: List[str] = []
    if refused:
        lines.append(
            "agent-connect refused "
            + ("1 request" if len(refused) == 1 else f"{len(refused)} requests")
            + " from the agent:"
        )
        lines += [f"- {r}" for r in refused[:5]]
    mapped = STOP_REASONS.get(stop_reason)
    if mapped is None:
        lines.append(
            "(the ACP Agent stopped for a reason agent-connect does not know: "
            f"{stop_reason or 'none given'!r})"
        )
    elif mapped != COMPLETED:
        lines.append(f"(the Turn stopped early: {stop_reason})")
    return "\n".join(lines)
