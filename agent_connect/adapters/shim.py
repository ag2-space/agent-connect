"""Runs a synchronous Adapter under the event-shaped contract.

The five Adapters that existed before this contract — codex, ollama, omnigent,
cline, kilo — are **not rewritten**. Each is a module exposing
`run(task, sandbox, cwd) -> str`, a blocking call that shells out and waits.
This shim runs that call off-thread and emits the two events such an Adapter can
honestly produce: the text it returned, and a `Done` saying the Turn finished.

Two things it deliberately does not do:

*It does not invent progress.* A synchronous Adapter that hands back one string
at the end knows nothing about which file it read or which command it ran.
Emitting `ToolStarted` events it cannot substantiate would put fiction in a
room. It emits fewer events, and the consumers cope — that is the whole reason
the vocabulary is a stream rather than a fixed shape.

*It does not pretend to read attachments.* `run(task, sandbox, cwd)` takes one
string and nothing else — there is no second parameter a file could arrive
through, and the Adapters behind it shell out to CLIs that were never handed
one. So a Task that carried attachments gets a `Notice` saying so, by name, and
the files are not mentioned to the Local Agent in any other way. The two
alternatives are both worse: silence leaves someone waiting for an answer about
a screenshot nobody looked at, and pasting the *path* into the prompt tells the
agent to go and read a file the person only meant to show it — a path they did
not choose, from a directory the relay owns.

*It does not interpret the string.* Whatever the Adapter returned is the answer,
including its own error text ("`codex` CLI not found on PATH…"). A synchronous
Adapter reports its failures in band, so a shim that tried to classify them
would have to pattern-match another module's prose. The Turn is `COMPLETED`
because the Adapter completed; honest endings for Adapters that can actually
distinguish them are the ACP Adapter's business.

Concurrency comes for free: off-thread means two rooms' Tasks overlap, and the
Adapters gained that without a line changing inside them.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from .. import attachments as att
from ..events import COMPLETED, Done, MessageChunk, Notice, TurnContext, TurnEvent
from ..sandbox import sandbox_preamble

#: What the room is told when a Task carried files this kind of Adapter has no
#: way to pass on. The same opening sentence the ACP Adapter uses for an
#: attachment kind its agent did not advertise — from the room's side it is the
#: same fact, and the reason it is true is the Worker's business, not theirs.
UNREAD = (
    "📎 agent-connect: I can't read that kind of attachment. This agent is run "
    "through a plain command line that takes text and nothing else, so {what} did "
    "not reach it:\n{lines}\n"
    "Paste the content into the room instead and I can work with that."
)
UNREAD_ONE = "the attachment on your message"
UNREAD_MANY = "the {count} attachments on your message"
UNREAD_LINE = "• {label} ({mime}){why}"

#: Appended for a file the Relay Client never managed to fetch. Without it the
#: room is told a true sentence about the wrong failure: this kind of Adapter
#: takes text only, but that is not why a file nobody could download is absent,
#: and only the library's own reason says which of the two happened.
UNREAD_WHY = " — {reason}"


def unread_notice(ctx: TurnContext) -> str:
    """The one message a room gets about files that could not be passed on."""
    if not ctx.attachments:
        return ""
    what = (UNREAD_ONE if len(ctx.attachments) == 1
            else UNREAD_MANY.format(count=len(ctx.attachments)))
    lines = "\n".join(
        UNREAD_LINE.format(
            label=att.label(a), mime=att.mime_of(a),
            why="" if a.ok else UNREAD_WHY.format(reason=a.reason))
        for a in ctx.attachments
    )
    return UNREAD.format(what=what, lines=lines)


class ShimAdapter:
    """One synchronous Adapter module, wearing the event-shaped contract."""

    def __init__(self, name: str, impl: Any):
        self.name = name
        self.impl = impl

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<ShimAdapter {self.name}>"

    async def turn(self, ctx: TurnContext) -> AsyncIterator[TurnEvent]:
        """Run the Adapter's blocking entry point and report what it said.

        The sandbox preamble is attached here rather than by the Worker because
        it describes *this* kind of Adapter's confinement — an operating-system
        Sandbox the Worker chose. An Adapter whose confinement is different
        (the ACP one, whose Permission Policy is merely cooperative) must not
        inherit a sentence that would misdescribe it.

        Attachments are reported before the Adapter runs, not after: the person
        finds out that their screenshot was not read while the answer is still
        being written, rather than alongside an answer that already ignored it.
        """
        unread = unread_notice(ctx)
        if unread:
            yield Notice(text=unread)
        # `ctx.prompt` is passed exactly as the person typed it. Nothing about
        # the attachments is folded into it — not a filename, and above all not
        # a path.
        prompt = sandbox_preamble(ctx.sandbox, ctx.access_tier) + ctx.prompt
        text = await asyncio.to_thread(self.impl.run, prompt, ctx.sandbox, ctx.cwd)
        text = text if isinstance(text, str) else str(text)
        if text:
            yield MessageChunk(text=text)
        yield Done(reason=COMPLETED, text=text)
