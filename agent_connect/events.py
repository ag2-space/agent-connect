"""The vocabulary that crosses the Adapter boundary.

An Adapter no longer takes a string and returns a string. It takes a
`TurnContext` and emits a stream of events, the last of which is `Done`:

    async for event in adapter.turn(ctx):
        ...

**The vocabulary is ours, not the protocol's.** Message chunk, thinking, tool
started, tool finished, plan, permission asked, notice, done-with-reason — that
is the whole list, and it is closed: it grows by argument, not by accident. No ACP type, and no type from any other protocol an Adapter happens
to speak, may appear here: an Adapter talking plain HTTP to a local model must
never have to imitate ACP to report that it produced some text. Adapters
translate *into* this vocabulary; consumers read only this vocabulary.

This module deliberately imports nothing from the rest of the package. Both
sides of the boundary depend on it — the Adapters that emit and the Worker (and
later the `TurnReporter`) that consumes — so it must not depend on either.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional, Tuple

# ---------------------------------------------------------------------------
# Why a Turn stopped. Ours, deliberately: a stop reason a person in a room can
# be told about, not a protocol enum. An Adapter maps whatever its Local Agent
# says onto one of these, and `FAILED` is where anything unrecognised lands so
# that a new upstream reason cannot silently read as a completed answer.
# ---------------------------------------------------------------------------
COMPLETED = "completed"
CANCELLED = "cancelled"
TIMEOUT = "timeout"
REFUSED = "refused"
TOKEN_LIMIT = "token_limit"
FAILED = "failed"

#: Every reason other than this one is a Turn that ended short of an answer.
NORMAL_REASONS = frozenset({COMPLETED})


@dataclass(frozen=True)
class Attachment:
    """One file that arrived with a room message, already on this machine.

    The relay client downloads whatever someone attached *before* the Task file
    is written, so `locator` is a local path and nothing on this side of the
    boundary fetches anything over the network. Everything else here is what the
    relay was *told* about the file by the platform it came from: `mime` and
    `filename` are labels, not facts, and neither decides what bytes are read.

    Deliberately metadata-only — no bytes and no file handle. An Adapter that
    cannot accept attachments at all (every shimmed one) has to be able to *see*
    that a Task carried some, so it can say so; only an Adapter that is going to
    pass them opens them, and `agent_connect.attachments` is the one place that
    happens.
    """

    locator: str
    mime: str = ""
    filename: str = ""
    size: int = 0
    sha256: str = ""
    id: str = ""


@dataclass(frozen=True)
class TurnContext:
    """Everything an Adapter is told about one Turn.

    Until this existed the Adapter received only the prompt text, which is the
    reason a Local Agent could not tell two people in a room apart. It now
    carries who asked (`sender_name`, `user_id`), where they asked (`room`,
    `room_name`), at what trust (`access_tier`, and the `sandbox` the Worker
    derived from it — never from anything the sender can write), and which
    message it came from (`source_message_id`, for threading later).

    `prompt` is what the person typed, with relay headers already stripped and
    no preamble attached. Framing is the Adapter's business: the shimmed
    Adapters prepend the sandbox preamble because their confinement is what it
    describes, and an Adapter with different confinement says something
    different.

    `attachments` are the files that came with the message. They are carried
    *beside* `prompt` and never folded into it: an attachment becomes content of
    the prompt or it is reported as unreachable, and neither is allowed to
    rewrite a word of what the person typed.
    """

    prompt: str
    task_id: str = ""
    room: str = ""
    room_name: str = ""
    access_tier: str = "other"
    sender_name: str = ""
    user_id: str = ""
    source_message_id: str = ""
    sandbox: str = "read-only"
    cwd: str = ""
    attachments: Tuple[Attachment, ...] = ()

    @property
    def session_key(self) -> Tuple[str, str]:
        """The pair a Session is keyed by: room and Access Tier.

        Never the room alone — a Session carries a permission mode, and a Tier
        must not inherit another Tier's. A Task the relay wrote without a room
        identifier gets a key of its own rather than sharing one with every
        other roomless Task, which would serialise unrelated work.
        """
        return (self.room or f"task:{self.task_id}", self.access_tier)


@dataclass(frozen=True)
class TurnEvent:
    """Base of the vocabulary. `kind` is the discriminator for consumers that
    would rather switch on a string than on a type."""

    kind: ClassVar[str] = ""


@dataclass(frozen=True)
class MessageChunk(TurnEvent):
    """Text meant for the person who asked. Concatenated, these are the answer."""

    kind: ClassVar[str] = "message_chunk"
    text: str = ""


@dataclass(frozen=True)
class Thinking(TurnEvent):
    """The Local Agent reasoning out loud.

    Emitted so a consumer *may* use it; the room is not one of those consumers
    — the chat carries answers, not thinking-out-loud.
    """

    kind: ClassVar[str] = "thinking"
    text: str = ""


@dataclass(frozen=True)
class ToolStarted(TurnEvent):
    """The Local Agent began doing something — reading a file, running a test.

    `action` is a coarse classification of the tool ("read", "edit",
    "execute", "search", "other"); `title` is the human phrase a room would
    show. `tool_id` correlates this with its `ToolFinished`.
    """

    kind: ClassVar[str] = "tool_started"
    tool_id: str = ""
    title: str = ""
    action: str = "other"
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolFinished(TurnEvent):
    """That same piece of work ended. `status` is "completed" or "failed"."""

    kind: ClassVar[str] = "tool_finished"
    tool_id: str = ""
    title: str = ""
    status: str = COMPLETED
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Plan(TurnEvent):
    """The Local Agent's current plan. `entries` are `{title, status}` dicts."""

    kind: ClassVar[str] = "plan"
    entries: list = field(default_factory=list)


@dataclass(frozen=True)
class PermissionAsked(TurnEvent):
    """The Local Agent asked to do something, and the Worker's Permission
    Policy answered.

    The answer travels with the question because a *rejected* request is the
    interesting one: without it a blocked agent is indistinguishable from a
    lazy one, and the person is left wondering why the thing did not happen.
    """

    kind: ClassVar[str] = "permission_asked"
    title: str = ""
    allowed: bool = False
    reason: str = ""
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Notice(TurnEvent):
    """Something the room is told *about* this run, rather than as part of it.

    "I could not restore our earlier conversation" is not an answer, not
    progress, and not a footnote on the answer — it is a fact about the run that
    a person needs at the moment it becomes true. So it is its own event, and a
    consumer posts it as its own message: folding it into the reply would put a
    remark about memory inside the answer to a question about something else.

    Kept deliberately narrow. This is not a logging channel — anything the
    operator needs and the room does not belongs on stderr.
    """

    kind: ClassVar[str] = "notice"
    text: str = ""


@dataclass(frozen=True)
class Done(TurnEvent):
    """The terminal event: why the Turn stopped, and the answer it produced.

    Exactly one of these ends every Turn, including a failed one. `text` is the
    full answer — an Adapter that streamed it in chunks repeats it here, so a
    consumer that only wants the final answer does not have to reassemble it.
    `note` is an operator- or room-facing explanation of a non-normal ending.
    """

    kind: ClassVar[str] = "done"
    reason: str = COMPLETED
    text: str = ""
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.reason in NORMAL_REASONS


def final_text(done: Optional["Done"], chunks: Any = ()) -> str:
    """The answer to write out, from the terminal event and the chunks seen.

    A Turn that emitted no `Done` at all is an Adapter bug, not a silent empty
    answer: the chunks are still returned so nothing already produced is lost.
    """
    if done is not None and done.text:
        return done.text
    return "".join(chunks)
