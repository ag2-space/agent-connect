"""The Ladder: one message, from acknowledgement to answer.

The shape is the relay protocol's, not ours, and `docs/adr/0002` fixes it:

1. The **broker** places the intake reaction when it acknowledges the Task.
   The Worker must not — a second one is not a second opinion, it is a bug the
   room can see.
2. The Worker posts the **fleet-wide placeholder copy** and keeps the event
   identifier that comes back.
3. It **edits that same message** as the work proceeds.
4. It **edits it into the answer**, and completes the lease with the terminal
   marker so the delivery path posts nothing further. Exactly one reply.

Step 3 is *our* extension on top of a canonical two-step shape, and it is
switchable off in one setting (`AGENT_CONNECT_LIVE_PROGRESS=0`), because the
placeholder copy is fleet-wide and its owners may judge live editing to be
noise. Retreating must cost a setting, not a rewrite.

Three rules govern what the room sees while the work runs:

*Tool activity, not text.* Progress says "reading worker.py", not the answer as
it is being typed. An edit per message chunk would be a storm of replacements
for content the final edit overwrites anyway.

*Throttled.* At most one progress edit per throttle window, however busy the
Turn is.

*Never the model's reasoning.* `Thinking` is in the vocabulary so that some
consumer *may* read it. The room is not that consumer, and there is a test that
says so.

The reporter is the only thing that knows this contract. It works the same for
the ACP Adapter and for a shimmed one, which simply emits fewer events: a
shimmed Adapter gets a placeholder and a final answer, with nothing in between,
and no code here is aware of the difference.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import List, Mapping, Optional

from .events import (
    COMPLETED,
    Done,
    MessageChunk,
    PermissionAsked,
    ToolFinished,
    ToolStarted,
    TurnContext,
    final_text,
)
from .roomops import RoomOpError

#: The fleet-wide placeholder copy. Not ours to reword: every agent on the
#: platform posts this same sentence, and a room learns to read it at a glance.
PLACEHOLDER = "⏳ On it..."

#: Completes the lease. The relay client archives the result and posts nothing,
#: because the answer is already in the room. It must start the result body.
REPLIED = "[REPLIED]"

#: What the placeholder becomes when the answer was too long to edit in. The
#: answer follows as its own message, through the result path, which the relay
#: client already knows how to chunk.
POINTER = "✅ Done — the answer was too long for this message, so it follows below."

DEFAULT_THROTTLE = 3.0
DEFAULT_CEILING = 4000

LIVE_ENV = "AGENT_CONNECT_LIVE_PROGRESS"
THROTTLE_ENV = "AGENT_CONNECT_PROGRESS_THROTTLE"
CEILING_ENV = "AGENT_CONNECT_EDIT_CEILING"

_OFF = ("0", "false", "no", "off")


@dataclass(frozen=True)
class LadderSettings:
    """How much of the Ladder this Worker climbs.

    `live` is the one setting the spec promises: false leaves the canonical
    two-step shape — placeholder, then answer — untouched underneath.
    """

    live: bool = True
    throttle: float = DEFAULT_THROTTLE
    ceiling: int = DEFAULT_CEILING

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LadderSettings":
        env = os.environ if env is None else env
        return cls(
            live=(env.get(LIVE_ENV, "").strip().lower() or "1") not in _OFF,
            throttle=_number(env.get(THROTTLE_ENV), DEFAULT_THROTTLE, float),
            ceiling=_number(env.get(CEILING_ENV), DEFAULT_CEILING, int),
        )


def _number(raw, fallback, cast):
    """A setting a person typed, or the default — never a crash at startup."""
    try:
        value = cast(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return fallback
    return value if value >= 0 else fallback


class TurnReporter:
    """Drives one Turn up the Ladder and returns the Task's result body.

    Constructed per Turn. `ops` may be `None` — a Worker with no relay token,
    or a test — and then nothing is posted and the answer travels the way it
    always did, as the result body. That fallback is also where a relay that
    refuses a Room Op ends up, so a room that cannot be edited still gets its
    answer.
    """

    def __init__(
        self,
        ops=None,
        settings: Optional[LadderSettings] = None,
        clock=time.monotonic,
    ):
        self.ops = ops
        self.settings = settings or LadderSettings()
        self._clock = clock
        #: The placeholder's event identifier; falsy means nothing was posted.
        self.event_id = ""
        self._room = ""
        self._last_edit = 0.0
        self._steps: List[str] = []      # tool titles, in the order they started
        self._failed: set = set()        # titles of tool calls that failed
        self._refused: List[str] = []    # what the Permission Policy rejected
        #: Number of progress edits actually issued — the throttle, observable.
        self.progress_edits = 0

    # -- the Ladder ---------------------------------------------------------

    async def start(self, ctx: TurnContext) -> None:
        """Post the placeholder. Silence is what this is here to end."""
        self._room = ctx.room
        if self.ops is None or not self._room or not getattr(self.ops, "available", True):
            return
        try:
            self.event_id = await self.ops.message(self._room, PLACEHOLDER)
        except RoomOpError:
            self.event_id = ""
        self._last_edit = self._clock()

    async def run(self, adapter, ctx: TurnContext) -> str:
        """One whole Turn: placeholder, live edits, answer, result body."""
        await self.start(ctx)
        chunks: List[str] = []
        done: Optional[Done] = None
        async for event in adapter.turn(ctx):
            if isinstance(event, MessageChunk):
                chunks.append(event.text)
            elif isinstance(event, Done):
                done = event
            else:
                await self.on_event(event)
        return await self.finish(final_text(done, chunks), done.note if done else "")

    async def on_event(self, event) -> None:
        """Note what the agent is doing, and maybe say so in the room.

        `Thinking` reaches this method and goes no further — deliberately, and
        the room never learns it existed.
        """
        if isinstance(event, ToolStarted):
            self._steps.append(event.title or event.action or "working")
            await self._progress()
        elif isinstance(event, ToolFinished):
            if event.title:
                if event.title not in self._steps:
                    self._steps.append(event.title)
                if event.status != COMPLETED:
                    self._failed.add(event.title)
            await self._progress()
        elif isinstance(event, PermissionAsked) and not event.allowed:
            self._refused.append(
                f"{event.title} — {event.reason}" if event.reason else event.title
            )

    async def finish(self, answer: str, note: str = "") -> str:
        """Edit the placeholder into the answer; return what to write as result.

        Two endings, and which one happened is visible in the return value. The
        answer fits an edit: the placeholder becomes it, and the result body is
        the terminal marker, so the delivery path posts nothing. The answer does
        not fit: the placeholder becomes a short pointer and the answer itself
        is the result body, which the relay client chunks as it always has.
        """
        body = _assemble(answer, note, self._steps, self._failed, self._refused)
        if not self.event_id:
            return body
        if len(body) > self.settings.ceiling:
            await self._edit(POINTER)
            return body
        if not await self._edit(body):
            # The final edit is the one failure that must not lose the answer.
            return body
        return f"{REPLIED}\n\n{body}"

    # -- internals ----------------------------------------------------------

    async def _progress(self) -> None:
        if not self.settings.live or not self.event_id or not self._steps:
            return
        now = self._clock()
        if now - self._last_edit < self.settings.throttle:
            return
        self._last_edit = now
        if await self._edit(self._progress_body()):
            self.progress_edits += 1

    def _progress_body(self) -> str:
        step = self._steps[-1]
        count = f" (step {len(self._steps)})" if len(self._steps) > 1 else ""
        return f"{PLACEHOLDER}\n\n{step}{count}"

    async def _edit(self, body: str) -> bool:
        try:
            await self.ops.edit(self._room, self.event_id, body)
        except RoomOpError:
            self.event_id = ""
            return False
        return True


def _assemble(answer: str, note: str, steps, failed, refused) -> str:
    """The answer, its ending, and a compact summary of what was done.

    The summary is not a log. It is the two or three lines that let a person see
    the effects without reading one — *including the operations the Permission
    Policy rejected*, because without them a blocked agent is indistinguishable
    from a lazy one and the person is left wondering why the thing did not
    happen.
    """
    parts = [answer.strip()] if answer.strip() else []
    if note.strip():
        parts.append(note.strip())
    summary = _summary(steps, failed, refused)
    if summary:
        parts.append(summary)
    body = "\n\n".join(parts).strip()
    return body or "agent-connect: the agent produced no answer."


def _summary(steps, failed, refused, limit: int = 6) -> str:
    lines: List[str] = []
    if steps:
        shown = [f"{s} (failed)" if s in failed else s for s in steps[:limit]]
        more = len(steps) - len(shown)
        lines.append(
            "— did: " + " · ".join(shown) + (f" · +{more} more" if more > 0 else "")
        )
    if refused:
        count = "1 operation" if len(refused) == 1 else f"{len(refused)} operations"
        lines.append(f"— refused ({count}): " + " · ".join(refused[:limit]))
    return "\n".join(lines)
