"""The Worker's side of the Agent Client Protocol.

The Worker is the **ACP Client**; the Local Agent — or a bridge process standing
in for it, such as `@agentclientprotocol/claude-agent-acp` — is the **ACP
Agent**. That assignment is the point: the Client is the side that holds the
Permission Policy, so the decision about what the Local Agent may do never rests
with the Local Agent.

This module knows the protocol and nothing else. It has no notion of a Task, a
room, an Access Tier, the relay, or the Adapter registry. Room participation
consumes it directly.

    async with AcpClient.spawn(["claude-agent-acp"]) as client:
        await client.initialize()
        session_id = await client.new_session(cwd="/path/to/repo")
        turn = await client.prompt(session_id, "what does worker.py do?")
        print(turn.text, turn.stop_reason)

Progress arrives two ways and is never discarded: every `session/update`
notification is handed to the `on_update` callback as it arrives, and is also
collected on the `TurnResult`. Nothing consumes the callback yet; the
event-shaped Adapter contract will.

Transport and schema come from the `agent-client-protocol` package. That
dependency — and the pydantic it carries — is the one `docs/adr/0001` accepted
when it ended this repository's `dependencies = []` policy.
"""
from __future__ import annotations

import asyncio
import os
import warnings
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Sequence

import acp

# Protocol version this Client speaks. Verified against
# @agentclientprotocol/claude-agent-acp 0.64.2, which also negotiates 1.
PROTOCOL_VERSION = acp.PROTOCOL_VERSION

# The Worker never implements the protocol's terminal-provisioning methods:
# granting a remotely-triggered process an interactive terminal is not an
# acceptable trade even for owner-tier work. Nor does it offer filesystem
# methods — the Local Agent does its own file I/O and routes the decision
# through `session/request_permission`, which is where the Policy lives.
#
# These are also the protocol's defaults, so the schema library omits them from
# the wire rather than sending explicit falses. Stating them here anyway is the
# point: if a later change wants a capability on, it turns it on here, and the
# absence of `terminal` stays a decision rather than an oversight.
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}}


class AcpError(Exception):
    """Anything the ACP Agent, or the connection to it, got wrong."""


class SessionResumeRefused(AcpError):
    """The ACP Agent would not resume the Session it was asked to resume.

    A Session that cannot be resumed costs context, not an error: the caller is
    expected to open a fresh one and say so.
    """


class AcpAgentGone(AcpError):
    """The ACP Agent process died, or its stdio closed, with work outstanding."""


@dataclass(frozen=True)
class AgentDescription:
    """What the ACP Agent said about itself at `initialize`."""

    name: str
    version: str
    protocol_version: int
    capabilities: dict
    auth_methods: list

    @property
    def can_resume_sessions(self) -> bool:
        return bool(self.capabilities.get("loadSession"))

    def accepts_prompt_content(self, kind: str) -> bool:
        """Whether the ACP Agent advertised a prompt content kind, e.g. "image".

        An attachment the Local Agent did not advertise is reported honestly
        rather than dropped, so callers ask before they send.
        """
        return bool((self.capabilities.get("promptCapabilities") or {}).get(kind))


@dataclass(frozen=True)
class Update:
    """One `session/update` notification, in our shape rather than the wire's.

    `kind` is the ACP discriminator (`agent_message_chunk`, `tool_call`,
    `agent_thought_chunk`, `plan`, …). `text` is the text it carried, when it
    carried any. `raw` is the whole payload, unmodified, so nothing is lost on
    the way through — this module normalises for convenience, never by
    discarding.
    """

    session_id: str
    kind: str
    text: str
    raw: dict


@dataclass(frozen=True)
class PermissionRequest:
    """The ACP Agent asking the Worker whether it may do something.

    A handler returns the `option_id` it chooses, or `None` to cancel. The
    binding is *cooperative*: it constrains only an agent that chooses to ask.
    """

    session_id: str
    tool_call: dict
    options: list

    def option_of_kind(self, *kinds: str) -> Optional[str]:
        """The id of the first offered option whose `kind` is one of `kinds`.

        Option ids are the ACP Agent's to name, so a policy that hard-codes
        `"allow"` breaks on the next bridge release; the `kind` field is the
        part the protocol defines.
        """
        for option in self.options:
            if option.get("kind") in kinds:
                return option.get("optionId")
        return None


@dataclass(frozen=True)
class TurnResult:
    """The end of one Turn: why it stopped, and everything it produced."""

    stop_reason: str
    text: str
    updates: list = field(default_factory=list)


PermissionHandler = Callable[[PermissionRequest], Any]
UpdateHandler = Callable[[Update], Any]


def reject_all(request: PermissionRequest) -> None:
    """The default Permission Policy: refuse everything.

    Failing closed is the only safe default for a module that does not know
    what it is being asked on behalf of. A real Policy is injected by the
    caller that does know.
    """
    return None


def _extract_text(update: dict) -> str:
    content = update.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        return content.get("text") or ""
    return ""


def _as_dict(value: Any) -> dict:
    """Wire-shaped dict from whatever the schema layer handed us.

    Serializer warnings are swallowed on purpose. The schema library is on a
    lower version line than the bridges it talks to, so a bridge that returns a
    field shaped slightly differently from the Python models makes pydantic
    complain on every single notification. That is library noise in an operator's
    log, not information — and the value still round-trips, because everything
    downstream reads plain dicts.
    """
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return dump(by_alias=True, exclude_none=True, mode="json")
    return dict(value or {})


class _ClientCallbacks(acp.Client):
    """Adapts the schema library's callbacks onto our own handlers.

    Kept private and thin: it exists so that pydantic models and the library's
    keyword-splatting convention stop here rather than reaching any caller.
    """

    def __init__(self, owner: "AcpClient"):
        self._owner = owner

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        raw = _as_dict(update)
        await self._owner._on_update_received(
            Update(
                session_id=session_id,
                kind=raw.get("sessionUpdate") or "",
                text=_extract_text(raw),
                raw=raw,
            )
        )

    async def request_permission(
        self, session_id: str, tool_call: Any, options: Any, **kwargs: Any
    ) -> dict:
        request = PermissionRequest(
            session_id=session_id,
            tool_call=_as_dict(tool_call),
            options=[_as_dict(o) for o in (options or [])],
        )
        option_id = await self._owner._decide_permission(request)
        if option_id is None:
            return {"outcome": {"outcome": "cancelled"}}
        return {"outcome": {"outcome": "selected", "optionId": option_id}}


class AcpClient:
    """A live connection to one ACP Agent process.

    One instance owns one child process and the Sessions opened on it. Methods
    map onto the protocol; the shapes crossing the boundary are ours.
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        connection: Any,
        *,
        on_update: Optional[UpdateHandler] = None,
        permission_handler: Optional[PermissionHandler] = None,
        stderr_tail: Optional[deque] = None,
    ):
        self._process = process
        self._connection = connection
        self._on_update = on_update
        self._permission_handler = permission_handler or reject_all
        self._stderr = stderr_tail if stderr_tail is not None else deque(maxlen=200)
        self._turn_updates: Optional[list] = None
        self.agent: Optional[AgentDescription] = None

    # -- lifecycle --------------------------------------------------------

    @classmethod
    @asynccontextmanager
    async def spawn(
        cls,
        command: Sequence[str],
        *,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        on_update: Optional[UpdateHandler] = None,
        permission_handler: Optional[PermissionHandler] = None,
    ):
        """Start an ACP Agent and hold a connection to it for the block.

        `command` is the ACP Agent's command line — the bridge, or a Local Agent
        that speaks ACP natively. The process is always reaped on the way out,
        including when the body raised.
        """
        if not command:
            raise AcpError("no ACP Agent command given")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, **(env or {})} if env else None,
            )
        except FileNotFoundError as exc:
            # Ticket 05 turns this into operator-facing install advice; the core
            # only promises that "the bridge is missing" is distinguishable.
            raise AcpError(f"ACP Agent command not found: {command[0]}") from exc

        stderr_tail: deque = deque(maxlen=200)
        drain = asyncio.ensure_future(_drain(process.stderr, stderr_tail))
        client = cls(
            process,
            None,
            on_update=on_update,
            permission_handler=permission_handler,
            stderr_tail=stderr_tail,
        )
        client._connection = acp.connect_to_agent(
            _ClientCallbacks(client), process.stdin, process.stdout
        )
        try:
            yield client
        finally:
            drain.cancel()
            await client.close()

    async def close(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._process.kill()
                await self._process.wait()

    @property
    def alive(self) -> bool:
        return self._process.returncode is None

    def stderr_tail(self) -> str:
        """The ACP Agent's last lines of stderr — the only clue when it dies."""
        return "".join(self._stderr)

    # -- protocol ---------------------------------------------------------

    async def initialize(self) -> AgentDescription:
        """Negotiate the protocol and learn what this ACP Agent can do."""
        response = await self._guard(
            self._connection.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=CLIENT_CAPABILITIES,
            )
        )
        info = _as_dict(getattr(response, "agentInfo", None))
        self.agent = AgentDescription(
            name=info.get("name") or "",
            version=info.get("version") or "",
            protocol_version=getattr(response, "protocolVersion", PROTOCOL_VERSION),
            capabilities=_as_dict(getattr(response, "agentCapabilities", None)),
            auth_methods=[_as_dict(m) for m in (getattr(response, "authMethods", None) or [])],
        )
        return self.agent

    async def new_session(
        self, cwd: str, mcp_servers: Optional[list] = None
    ) -> str:
        """Open a Session rooted at `cwd`, and return its identifier.

        The working directory is fixed here because that is where the protocol
        puts it — there is no per-prompt working directory to be had.
        """
        response = await self._guard(
            self._connection.new_session(cwd=cwd, mcp_servers=mcp_servers or [])
        )
        return response.sessionId

    async def load_session(
        self, session_id: str, cwd: str, mcp_servers: Optional[list] = None
    ) -> None:
        """Resume a Session the ACP Agent already holds.

        Resumption replays the entire prior conversation as `session/update`
        notifications, which reach `on_update` like any other. A caller that is
        putting updates somewhere a person can see must suppress them for the
        duration of this call, or it will publish an old transcript.

        Raises `SessionResumeRefused` when the ACP Agent will not resume.
        """
        if self.agent is not None and not self.agent.can_resume_sessions:
            raise SessionResumeRefused(
                f"{self.agent.name or 'the ACP Agent'} does not support session resumption"
            )
        try:
            await self._guard(
                self._connection.load_session(
                    cwd=cwd, session_id=session_id, mcp_servers=mcp_servers or []
                )
            )
        except AcpAgentGone:
            raise
        except AcpError as exc:
            raise SessionResumeRefused(str(exc)) from exc

    async def set_session_mode(self, session_id: str, mode_id: str) -> None:
        await self._guard(
            self._connection.set_session_mode(session_id=session_id, mode_id=mode_id)
        )

    async def prompt(self, session_id: str, prompt: Any) -> TurnResult:
        """Run one Turn on a Session and wait for it to stop.

        `prompt` may be a string, one content block, or a list of them. Only one
        Turn at a time may be open on a Session — that is a protocol
        requirement, and serialising per Session is the caller's job.
        """
        blocks = _prompt_blocks(prompt)
        collected: list = []
        self._turn_updates = collected
        try:
            response = await self._guard(
                self._connection.prompt(session_id=session_id, prompt=blocks)
            )
        finally:
            self._turn_updates = None
        return TurnResult(
            stop_reason=getattr(response, "stopReason", "") or "",
            text="".join(u.text for u in collected if u.kind == "agent_message_chunk"),
            updates=collected,
        )

    async def cancel(self, session_id: str) -> None:
        """End a running Turn through the protocol rather than by killing it.

        The Turn's `prompt` call returns normally, with a `cancelled` stop
        reason and whatever it produced before the cancellation — so other
        Sessions on this ACP Agent survive.
        """
        await self._guard(self._connection.cancel(session_id=session_id))

    # -- internals --------------------------------------------------------

    async def _guard(self, awaitable: Awaitable) -> Any:
        """Await protocol work, turning a dead ACP Agent into a clear failure.

        Without this, an ACP Agent that exits mid-Turn leaves the caller waiting
        on a response that will never come.
        """
        call = asyncio.ensure_future(_await(awaitable))
        death = asyncio.ensure_future(self._process.wait())
        try:
            done, _ = await asyncio.wait(
                {call, death}, return_when=asyncio.FIRST_COMPLETED
            )
            if call in done:
                try:
                    return call.result()
                except acp.RequestError as exc:
                    raise AcpError(str(exc)) from exc
                except (ConnectionError, EOFError) as exc:
                    # The transport noticed the closed pipe before `wait()`
                    # resolved. Same event, whichever got there first.
                    raise await self._gone() from exc
            raise await self._gone()
        finally:
            for pending in (call, death):
                if not pending.done():
                    pending.cancel()

    async def _gone(self) -> AcpAgentGone:
        """Describe the death of the ACP Agent, exit code and stderr included."""
        try:
            await asyncio.wait_for(self._process.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        return AcpAgentGone(
            "the ACP Agent exited "
            f"(code {self._process.returncode}){_suffix(self.stderr_tail())}"
        )

    async def _on_update_received(self, update: Update) -> None:
        if self._turn_updates is not None:
            self._turn_updates.append(update)
        if self._on_update is not None:
            result = self._on_update(update)
            if asyncio.iscoroutine(result):
                await result

    async def _decide_permission(self, request: PermissionRequest) -> Optional[str]:
        result = self._permission_handler(request)
        if asyncio.iscoroutine(result):
            result = await result
        return result


async def _await(value: Any) -> Any:
    return await value


async def _drain(stream: Any, sink: deque) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        sink.append(line.decode("utf-8", "replace"))


def _suffix(stderr: str) -> str:
    stderr = stderr.strip()
    return f": {stderr[-500:]}" if stderr else ""


def _prompt_blocks(prompt: Any) -> list:
    if isinstance(prompt, str):
        return [{"type": "text", "text": prompt}]
    if isinstance(prompt, dict):
        return [prompt]
    return [b if isinstance(b, dict) else b for b in prompt]
