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

A `Notice` breaks the one-message shape on purpose, and only it does. An
announcement — "the context was reset", "your message is queued" — is a fact
about the run and not part of the answer, so it is posted as **its own message**
and the placeholder is left alone. Editing the placeholder into an announcement
would replace the answer with a remark about it.

**Endings tell the truth, and there are two kinds of them.** A Turn that
produced *something* and stopped short — a timeout that nearly finished, a token
limit — keeps what it produced and carries an explicit line saying it was
interrupted; silence that reads like a finished answer is the failure being
prevented. A Turn that produced *nothing* — a refusal, an empty timeout, a dead
bridge — is a **structured rejection**: the result body is marked `[no-send]`,
the placeholder is left exactly as it is, and the Worker writes no failure
message of its own. The broker owns that message, and two apologies for one
failure is the thing the spec forbids.

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
    CANCELLED,
    COMPLETED,
    FAILED,
    NORMAL_REASONS,
    REFUSED,
    TIMEOUT,
    TOKEN_LIMIT,
    Done,
    MessageChunk,
    Notice,
    PermissionAsked,
    ToolFinished,
    ToolStarted,
    TurnContext,
    final_text,
)
from .outgoing import Delivery, Outbox, carries_files, not_sent_notice, only_files_line
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

#: What the placeholder becomes when the reply carries a file. Same branch, same
#: reason: the body has to travel the delivery path, because that is the only
#: path with the send allowlist on it — see `agent_connect.outgoing`. The reply
#: and its files arrive together, just below.
FILE_POINTER = "✅ Done — the reply and its file follow below."
FILE_POINTER_MANY = "✅ Done — the reply and its {count} files follow below."

#: What a person is told when their message arrives while the Session is busy.
#: Only one Turn at a time may be open on a Session, so the message waits — and
#: a wait nobody was told about is indistinguishable from a message that was
#: dropped.
QUEUED = ("📥 agent-connect: I am still working on an earlier message in this "
          "room, so this one is queued and will be answered next{others}.")

#: The structured rejection. `[no-send]` is one of the three skip markers the
#: relay client's `parse_markers()` recognises at the start of a result body:
#: the result is archived and **nothing at all is delivered**, which is exactly
#: what "the broker owns the failure notice" needs. It is not a marker invented
#: here — `worker.py` has written `[no-send] empty task` since long before the
#: Ladder existed, and this is the same shape with a reason attached.
NO_SEND = "[no-send]"

#: The line that says a Turn ended somewhere other than a finished answer, for
#: each reason that is not completion. Used only when the Adapter gave no note
#: of its own; an Adapter that explained itself is not corrected. What must
#: never happen is neither: a Turn that stopped short and reads as if it had
#: not is the failure this table exists to prevent.
STOP_LINES = {
    TIMEOUT: "⏱ agent-connect: the turn ran past its deadline and was interrupted.",
    CANCELLED: "🛑 agent-connect: the turn was cancelled before it finished.",
    REFUSED: "🚫 agent-connect: this request was refused.",
    TOKEN_LIMIT: "✂️ agent-connect: the agent reached its token limit, so this "
                 "answer stops short of the whole of it.",
    FAILED: "⚠️ agent-connect: the turn did not finish.",
}

#: What the room is told when the agent finished without saying anything. Not a
#: failure — nothing went wrong, so no broker notice is coming, and the room
#: would otherwise be left watching the placeholder for ever.
SILENT = "✅ agent-connect: the agent finished without an answer to give."

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
        outbox: Optional[Outbox] = None,
    ):
        self.ops = ops
        self.settings = settings or LadderSettings()
        self._clock = clock
        #: Where a file the agent produced is staged so the Relay Client's send
        #: allowlist can see it. Built on demand, because a Turn that names no
        #: file must not touch the filesystem to find that out.
        self.outbox = outbox
        #: The placeholder's event identifier; falsy means nothing was posted.
        self.event_id = ""
        self._room = ""
        self._ctx: Optional[TurnContext] = None
        self._last_edit = 0.0
        self._steps: List[str] = []      # tool titles, in the order they started
        self._failed: set = set()        # titles of tool calls that failed
        self._refused: List[str] = []    # what the Permission Policy rejected
        self._unposted: List[str] = []   # notices the room could not be told
        #: Number of progress edits actually issued — the throttle, observable.
        self.progress_edits = 0
        #: Number of `Notice` events posted as their own message.
        self.notices_posted = 0
        #: True when the Turn ended as a structured rejection — observable so a
        #: caller can tell "nothing was delivered" from "nothing happened".
        self.rejected = False
        #: The files this Turn put on their way to the room, by the name the room
        #: will see, and the ones it refused to send.
        self.delivered: tuple = ()
        self.not_sent: tuple = ()

    # -- the Ladder ---------------------------------------------------------

    async def start(self, ctx: TurnContext) -> None:
        """Post the placeholder. Silence is what this is here to end."""
        self._room = ctx.room
        self._ctx = ctx
        if self.ops is None or not self._room or not getattr(self.ops, "available", True):
            return
        try:
            self.event_id = await self.ops.message(self._room, PLACEHOLDER)
        except RoomOpError:
            self.event_id = ""
        # `_last_edit` is deliberately left at its floor: the *first* piece of
        # tool activity edits straight away, so a room learns what the agent is
        # doing without waiting out a throttle window. The rate limit applies
        # from there on.

    async def queued(self, ctx: TurnContext, ahead: int = 1) -> bool:
        """Say that this Task is waiting for the Session, before it starts.

        Posted *before* `start()`, and therefore before the placeholder, so the
        room reads it in the order it happened: "this one is queued" now, "⏳ On
        it..." when the Session is free. A person who sends a second message and
        hears nothing cannot tell it from a message that was dropped, which is
        the whole reason this exists.

        It is deliberately not kept: an announcement that is only true while the
        Turn is waiting must not resurface stapled to the answer.
        """
        self._room = ctx.room
        self._ctx = ctx
        others = "" if ahead <= 1 else f" ({ahead} messages are ahead of it)"
        return await self.notice(QUEUED.format(others=others), keep=False)

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
        return await self.finish(
            final_text(done, chunks),
            done.note if done else "",
            done.reason if done else COMPLETED,
        )

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
        elif isinstance(event, Notice):
            await self.notice(event.text)

    async def finish(self, answer: str, note: str = "", reason: str = COMPLETED) -> str:
        """Edit the placeholder into the answer; return what to write as result.

        Four endings, and which one happened is visible in the return value.
        The answer fits an edit: the placeholder becomes it, and the result body
        is the terminal marker, so the delivery path posts nothing. The answer
        does not fit: the placeholder becomes a short pointer and the answer
        itself is the result body, which the relay client chunks as it always
        has. **The reply carries a file**: the same pointer branch, for a
        different reason — the body has to reach the delivery path, because the
        send allowlist lives there and a `[REPLIED]` body is archived unread. And
        there is no answer at all: nothing is edited and the result is a
        structured rejection — see `reject`.
        """
        ending = _ending(reason, note)
        files = self._outgoing(answer)
        answer = files.text or only_files_line(files.sent)
        # A reply whose whole content was a file marker still has something to
        # deliver, and one that names a file it may not send still has something
        # to say. Only a Turn with none of the three produced nothing.
        if not answer.strip() and not files.asked:
            if reason not in NORMAL_REASONS or not self.event_id:
                return self.reject(reason, ending)
            # A Turn that ended *normally* with nothing to say is not a failure,
            # so no broker notice is coming — and a placeholder is already in the
            # room promising one. Rejecting here would leave it reading "on it"
            # for ever, so the Ladder is finished by the only party that knows
            # what happened. With no placeholder there is nothing to finish, and
            # the rejection above is right: an empty answer must not be posted.
            answer = SILENT
        body = _assemble(
            answer, ending, self._steps, self._failed, self._refused, self._unposted,
            not_sent_notice(files.refused),
        )
        if files.markers:
            # Invisible to the room — the delivery path strips them and uploads
            # what they name. Last, so the prose above is what a person reads.
            body = body + "\n\n" + "\n".join(files.markers)
        if not self.event_id:
            return body
        if files.markers or len(body) > self.settings.ceiling:
            await self._edit(_pointer(files.markers))
            return body
        if not await self._edit(body):
            # The final edit is the one failure that must not lose the answer.
            return body
        return f"{REPLIED}\n\n{body}"

    def _outgoing(self, answer: str) -> Delivery:
        """Stage whatever files this answer named, and take the markers out of it.

        The Worker never uploads anything itself: a file leaves this machine by
        being placed in the outgoing result directory, which is the one place the
        Relay Client's send allowlist trusts. See `agent_connect.outgoing` for why
        the route matters more than the feature.
        """
        if not carries_files(answer):
            return Delivery(text=answer or "")
        if self.outbox is None:
            self.outbox = Outbox()
        delivery = self.outbox.stage(answer, self._ctx)
        self.delivered = delivery.sent
        self.not_sent = delivery.refused
        return delivery

    def reject(self, reason: str, ending: str = "") -> str:
        """A Turn that produced nothing: say so to the archive, not to the room.

        **The Worker posts no failure message and edits nothing.** The relay
        client reads `[no-send]` at the head of a result body as "archive this
        and deliver nothing", so the room hears about the failure from the
        broker, which owns that message — the spec's one rule here is that a
        person must not receive two different apologies for one failure.

        The placeholder is deliberately left as it stands. Every edit available
        is a sentence the Worker would be writing about a failure, which is the
        message it must not write; the broker's notice is the one that arrives.

        What follows the marker is for whoever reads the archived result: the
        machine-readable reason, the Adapter's own explanation, any
        announcement that never reached the room, and the summary of what the
        Turn managed to do before it ended with nothing to say.
        """
        self.rejected = True
        parts = [
            f"{NO_SEND} agent-connect: nothing to deliver — this turn produced no "
            f"answer (reason: {reason}). The broker posts the failure notice; "
            f"the worker posts none."
        ]
        parts += [n.strip() for n in self._unposted if n and n.strip()]
        if ending.strip():
            parts.append(ending.strip())
        summary = _summary(self._steps, self._failed, self._refused)
        if summary:
            parts.append(summary)
        return "\n\n".join(parts)

    # -- internals ----------------------------------------------------------

    async def notice(self, text: str, keep: bool = True) -> bool:
        """Tell the room something about the run, as its own message.

        **Never by editing the placeholder.** The placeholder belongs to this
        Task's answer; editing it into "the context was reset" would replace the
        answer with a remark about it. A notice is a second message, posted the
        moment it is true, and the Ladder carries on above it untouched.

        A room that cannot be posted to does not lose the notice: it rides out
        on the result body instead, where the delivery path will post it — that
        is `keep`, and it is what an announcement about memory needs.

        `keep=False` is for an announcement that is only true *now*: "your
        message is queued" arriving stapled to the answer is not a smaller
        version of the same information, it is noise. A notice that could not be
        posted while it was true is dropped.

        Returns whether the room was actually told.
        """
        text = (text or "").strip()
        if not text:
            return False
        if self.ops is not None and self._room and getattr(self.ops, "available", True):
            try:
                await self.ops.message(self._room, text)
                self.notices_posted += 1
                return True
            except RoomOpError:
                pass
        if keep:
            self._unposted.append(text)
        return False

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


def _pointer(markers) -> str:
    """What the placeholder becomes when the reply travels below it."""
    if not markers:
        return POINTER
    if len(markers) == 1:
        return FILE_POINTER
    return FILE_POINTER_MANY.format(count=len(markers))


def _assemble(answer: str, note: str, steps, failed, refused, notices=(), files="") -> str:
    """The answer, its ending, and a compact summary of what was done.

    The summary is not a log. It is the two or three lines that let a person see
    the effects without reading one — *including the operations the Permission
    Policy rejected*, because without them a blocked agent is indistinguishable
    from a lazy one and the person is left wondering why the thing did not
    happen.

    `notices` are announcements that could not be posted as their own message
    (no relay, no room, a refused op). They lead the body rather than being
    dropped: a Worker with no room ops must still be able to say that the
    conversation started over.

    `files` is what could not be sent, and it goes directly after the answer
    rather than as its own message: someone who asked for a report *and* did not
    get it is owed both facts in the same breath.

    Only ever called with something to say: a Turn that produced nothing goes to
    `TurnReporter.reject` instead, and does not travel this path at all.
    """
    parts = [n.strip() for n in notices if n and n.strip()]
    if answer.strip():
        parts.append(answer.strip())
    if files.strip():
        parts.append(files.strip())
    if note.strip():
        parts.append(note.strip())
    summary = _summary(steps, failed, refused)
    if summary:
        parts.append(summary)
    return "\n\n".join(parts).strip()


def _ending(reason: str, note: str) -> str:
    """The line that says how the Turn ended, for an ending that was not plain.

    A stop reason other than completion **always** produces one. The Adapter's
    own note is preferred where there is one — it knows more than a table does,
    and correcting it would only tell the person twice — and `STOP_LINES` is the
    floor under an Adapter that reported a reason and no words. The case this
    rules out is the third one: a Turn that stopped short and says nothing,
    which a person reads as a finished answer.
    """
    note = (note or "").strip()
    if reason in NORMAL_REASONS:
        return note
    return note or STOP_LINES.get(reason, STOP_LINES[FAILED])


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
