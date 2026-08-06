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

**Naming an agent is enough; a command is always allowed to win.** The ordinary
operator sets `AGENT_CONNECT_ACP_AGENT=claude` and a preset in this module says
what to run. An operator with an ACP Agent nobody here anticipated sets
`AGENT_CONNECT_ACP_COMMAND` instead, and it overrides every preset — a preset
table must never be the reason a working ACP Agent cannot be used.

Every setting this Adapter reads is documented once, in `README.md` §
Settings. That section is the authoritative list for the whole Worker;
`test_acp_settings.py` fails if a setting exists in code and not there.

`AGENT_CONNECT_ACP_MODE` is optional because the mode ids are the ACP Agent's to
name. What matters is that the Session runs in the mode that *routes* permission
requests to the Worker rather than one that suppresses them — which is what
every bridge's default mode does, so the default is to leave it alone. The
variable exists for an agent whose default is something else.

**Startup check, and nothing more.** `preflight()` is what the Worker runs
before it serves its first Task: it resolves the command, notices a missing
bridge and turns it into install advice, and asks the ACP Agent whether it is
logged in. It opens no Session and sends no prompt. Discovering at the first
message in a room that the Local Agent wants a login is the failure this
prevents — the person who asked gets an answer, not an authentication notice.

**No interactive terminal, on any path.** ACP lets a Client offer terminal
provisioning so the Agent can ask for a shell. The Worker does not implement it
and does not advertise it (`CLIENT_CAPABILITIES` in `acp/core.py` is where that
decision is stated, and `test_acp_no_terminal.py` asserts it holds on the wire).
A remotely-triggered process does not get a terminal on the operator's machine.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from dataclasses import dataclass
from typing import AsyncIterator, Dict, List, Optional, Sequence, Tuple

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
AGENT_ENV = "AGENT_CONNECT_ACP_AGENT"
SKIP_AUTH_ENV = "AGENT_CONNECT_ACP_SKIP_AUTH_CHECK"

#: The bridge that makes Claude Code an ACP Agent, pinned.
#:
#: Pinned rather than floated because this package renamed itself (it was
#: `claude-code-acp`) and moved through many major versions inside six months.
#: An unpinned fetch eventually pulls an incompatible release into an install
#: that was working yesterday, and the operator has changed nothing.
#:
#: **This is the pinned version of record.** `install.sh` installs this exact
#: spec and `install.test.sh` asserts the two agree, so raising it is a diff a
#: reviewer sees rather than something that happens on its own.
BRIDGE_PACKAGE = "@agentclientprotocol/claude-agent-acp"
BRIDGE_VERSION = "0.64.2"
BRIDGE_SPEC = f"{BRIDGE_PACKAGE}@{BRIDGE_VERSION}"


@dataclass(frozen=True)
class Preset:
    """What running one well-known ACP Agent takes, as far as we know it.

    `binary` is what an installed copy is called on PATH; `fallback` is the
    command to run when it is not installed — pinned, never `@latest`.
    `verified` says whether a real Turn was actually observed against it, which
    is the difference between a preset an operator can trust and a preset that
    is our best reading of someone's documentation.
    """

    binary: str
    fallback: Tuple[str, ...]
    install: str
    login: str
    verified: bool
    args: Tuple[str, ...] = ()
    note: str = ""

    def command(self, env: Optional[dict] = None) -> List[str]:
        """The command to run: the installed copy, else the pinned fetch."""
        path = shutil.which(self.binary, path=(env or os.environ).get("PATH"))
        return [path, *self.args] if path else list(self.fallback)


#: Presets live here, in code, deliberately: a table an operator has to edit is
#: not a preset. Anything absent is served by `AGENT_CONNECT_ACP_COMMAND`, which
#: overrides this table entirely — see the module docstring.
PRESETS: Dict[str, Preset] = {
    "claude": Preset(
        binary="claude-agent-acp",
        fallback=("npx", "-y", BRIDGE_SPEC),
        install=f"npm install -g {BRIDGE_SPEC}",
        login="claude  (then /login), or `claude setup-token`",
        verified=True,
        note=f"Claude Code through the {BRIDGE_PACKAGE} bridge, pinned to "
             f"{BRIDGE_VERSION} — the version a full Turn was run against.",
    ),
    "gemini": Preset(
        binary="gemini",
        fallback=("npx", "-y", "@google/gemini-cli", "--experimental-acp"),
        install="npm install -g @google/gemini-cli",
        login="gemini  (then /auth)",
        verified=False,
        args=("--experimental-acp",),
        note="Gemini CLI speaks ACP itself, behind --experimental-acp. Read "
             "from its documentation, NOT observed here: if it has moved, set "
             f"{COMMAND_ENV} and the preset is out of your way.",
    ),
}

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
        raise AcpError(_unconfigured())
    return shlex.split(raw)


def _unconfigured() -> str:
    """What to say to someone who has selected the ACP Adapter and stopped."""
    return (
        f"the ACP Adapter needs to know which agent to run. Either name one:\n"
        f"    export {AGENT_ENV}={'|'.join(sorted(PRESETS))}\n"
        f"or give the command yourself, which overrides any preset:\n"
        f'    export {COMMAND_ENV}="npx -y {BRIDGE_SPEC}"'
    )


def preset_for(name: str) -> Preset:
    """The named preset, or a sentence naming the ones that exist."""
    preset = PRESETS.get(name.strip().lower())
    if preset is None:
        raise AcpError(
            f"{AGENT_ENV}={name!r} is not a preset agent-connect knows. "
            f"Known: {', '.join(sorted(PRESETS))}.\n"
            f"Any other ACP Agent runs through {COMMAND_ENV}, e.g.\n"
            f'    export {COMMAND_ENV}="my-agent --acp"'
        )
    return preset


def resolve_command(env: Optional[dict] = None) -> List[str]:
    """The ACP Agent command, from an explicit command or from a preset.

    Precedence is the point of this function: an explicit command wins over a
    named preset unconditionally, even when the preset exists and looks better
    informed. A preset table that could block a working ACP Agent would be
    worse than no presets at all.
    """
    env = os.environ if env is None else env
    if (env.get(COMMAND_ENV) or "").strip():
        return command_from_env(env)
    name = (env.get(AGENT_ENV) or "").strip()
    if name:
        return preset_for(name).command(env)
    raise AcpError(_unconfigured())


def install_advice(command: Sequence[str], env: Optional[dict] = None) -> str:
    """What an operator whose bridge is missing has to install, by name.

    A `FileNotFoundError` traceback names a path; this names a package and the
    line that installs it. Which package is knowable when the command came from
    a preset, and guessable-but-not-claimed when it did not.
    """
    env = os.environ if env is None else env
    missing = command[0] if command else "(no command)"
    name = (env.get(AGENT_ENV) or "").strip().lower()
    preset = PRESETS.get(name) if not (env.get(COMMAND_ENV) or "").strip() else None
    lines = [f"the ACP Agent's command is not installed: {missing!r}."]
    if preset is not None:
        lines.append(f"Install it with:\n    {preset.install}")
    if missing in ("npx", "npm", "node"):
        lines.append(
            "That command comes from Node.js, which is not on this machine's "
            "PATH — install Node.js 18+ first (https://nodejs.org)."
        )
    elif preset is None:
        lines.append(
            f"It came from {COMMAND_ENV}, so agent-connect cannot say what "
            f"installs it. Check the command runs in your own shell, or name a "
            f"preset instead: {AGENT_ENV}={'|'.join(sorted(PRESETS))}."
        )
    return "\n".join(lines)


def login_advice(agent, env: Optional[dict] = None) -> str:
    """What an operator whose Local Agent is not logged in has to run.

    Built from the preset when there is one, and from what the ACP Agent itself
    advertised when there is not — `authMethods` carries a human name and often
    a description, which is the agent's own words for its login.
    """
    env = os.environ if env is None else env
    name = (env.get(AGENT_ENV) or "").strip().lower()
    preset = PRESETS.get(name)
    lines = [
        "the Local Agent is not logged in — it offered agent-connect a way to "
        "authenticate, which means it has no credentials of its own yet."
    ]
    if preset is not None:
        lines.append(f"Log in with:\n    {preset.login}")
    offered = [
        (m.get("name") or m.get("id") or "").strip()
        for m in (agent.auth_methods or [])
    ]
    offered = [o for o in offered if o]
    if offered:
        lines.append("The agent offered: " + ", ".join(offered) + ".")
    lines.append(
        "agent-connect will not log in for you: it never opens an interactive "
        "terminal on your machine on behalf of a room. Log in yourself in your "
        "own shell, then start the Worker again.\n"
        f"If this check is wrong for your agent, set {SKIP_AUTH_ENV}=1."
    )
    return "\n".join(lines)


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


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
        #: What `preflight` learned about the ACP Agent, if it has run.
        self.agent_description = None

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<AcpAdapter {self._command or 'from ' + COMMAND_ENV}>"

    def command(self) -> List[str]:
        return list(self._command) if self._command else resolve_command()

    def mode(self) -> str:
        return self._mode if self._mode is not None else os.environ.get(MODE_ENV, "").strip()

    async def preflight(self) -> Optional[str]:
        """Is this Worker able to serve? `None` if yes, else what to fix.

        Run once at startup, before any Task exists. It resolves the command,
        starts the ACP Agent, and asks it who it is — `initialize` and nothing
        else. **No Session is opened and no prompt is sent**, so this costs no
        tokens and cannot do work; and no terminal is offered, here or anywhere.

        The authentication signal is `authMethods` on the initialize response:
        an ACP Agent that is already authenticated has no method to offer, and
        one that offers methods is telling the Client it needs to log in.

        **How far that is verified, exactly.** An authenticated
        `@agentclientprotocol/claude-agent-acp` 0.64.2 answers with an empty
        list — observed, twice. An ACP Agent that *does* advertise methods stops
        the Worker with the login command — observed against the fake ACP Agent.
        What could not be observed is a genuinely logged-out Claude bridge:
        started with `$HOME` redirected at an empty directory it still answered
        `authMethods: []`, so it is finding credentials somewhere else (a
        keychain, most likely) and this check would not fire for it. Treat the
        check as a real catch for ACP Agents that advertise auth methods and as
        no catch at all for ones that do not — and note that the honest reading
        is "the agent said it needs authenticating", which is what the message
        says. `AGENT_CONNECT_ACP_SKIP_AUTH_CHECK=1` exists for the other
        direction: a startup check that is wrong must be escapable without
        editing the package.
        """
        try:
            command = self.command()
        except AcpError as exc:
            return str(exc)
        if shutil.which(command[0]) is None:
            return install_advice(command)
        try:
            async with AcpClient.spawn(command, cwd=os.getcwd()) as client:
                agent = await client.initialize()
        except AcpError as exc:
            if "not found" in str(exc):
                return install_advice(command)
            return f"the ACP Agent would not start: {exc}"
        except Exception as exc:  # noqa: BLE001 — a startup check reports, never raises
            return f"the ACP Agent would not start: {exc}"
        self.agent_description = agent
        if agent.auth_methods and not _truthy(os.environ.get(SKIP_AUTH_ENV)):
            return login_advice(agent)
        return None

    def describe(self) -> str:
        """One line about what preflight found, for the Worker's startup log."""
        agent = getattr(self, "agent_description", None)
        if agent is None:
            return "acp: no ACP Agent contacted yet"
        return f"acp: {agent.name or 'an unnamed ACP Agent'} {agent.version}".strip()

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
            # A missing bridge mid-Turn gets the same install advice the startup
            # check gives, rather than a bare "command not found".
            note = (install_advice(command) if "not found" in str(exc) else str(exc))
            yield Done(reason=FAILED, text="".join(chunks),
                       note=f"agent-connect: {note}")
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
