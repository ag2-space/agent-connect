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

from ..events import COMPLETED, Done, MessageChunk, TurnContext, TurnEvent
from ..sandbox import sandbox_preamble


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
        """
        prompt = sandbox_preamble(ctx.sandbox, ctx.access_tier) + ctx.prompt
        text = await asyncio.to_thread(self.impl.run, prompt, ctx.sandbox, ctx.cwd)
        text = text if isinstance(text, str) else str(text)
        if text:
            yield MessageChunk(text=text)
        yield Done(reason=COMPLETED, text=text)
