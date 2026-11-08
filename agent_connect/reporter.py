"""One Turn's outcome, as the broker delivers it (ADR 0002, amended for 0.4.0).

The Worker posts no placeholder and edits nothing into an answer. The broker's
intake reaction and s.space's queued / working / replied indicators show that
the agent took the request; the Turn ends in one `Outcome`, which the Worker
hands to the relay client:

- `completed`: the answer is the result body, and the broker posts it once, as
  a reply to the request. The activity ledger records the task as responded.
- `no_action`: the agent had nothing to say — no text, or `NO_REPLY` followed
  only by its Activity lines (`agent_connect.activity`).
  Nothing is posted and nobody is mentioned; the ledger records no action.
- `failed`: the Turn produced nothing and did not end normally. The broker
  posts its one failure notice; the diagnostics go to the archived result.

A `Notice` is a fact about the run ("the context was reset"), posted as its own
message through Room Ops; one that cannot be posted leads the answer instead.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import List, Optional, Sequence

from ag2_relay_client.markers import mxids_in

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
from . import activity, result_guard, withheld
from .roomops import RoomOpError

#: What an agent replies, alone, when the message needs no answer.
NO_REPLY = "NO_REPLY"

#: How a Turn ended, as the relay client reports it to the broker.
ANSWERED = "completed"
NO_ACTION = "no_action"
FAILED_OUTCOME = "failed"

#: The skip marker an archived, undelivered result starts with.
NO_SEND = "[no-send]"

#: The line that says a Turn ended somewhere other than a finished answer, used
#: when the Adapter gave no note of its own.
STOP_LINES = {
    TIMEOUT: "⏱ agent-connect: the turn ran past its deadline and was interrupted.",
    CANCELLED: "🛑 agent-connect: the turn was cancelled before it finished.",
    REFUSED: "🚫 agent-connect: this request was refused.",
    TOKEN_LIMIT: "✂️ agent-connect: the agent reached its token limit, so this "
                 "answer stops short of the whole of it.",
    FAILED: "⚠️ agent-connect: the turn did not finish.",
}

#: A notice on a collaborator's Turn that the guard would have held.
NOTICE_WITHHELD = "🔒 agent-connect: a notice about this turn was withheld because it may contain sensitive data."
#: A collaborator's reply that cannot be released: a redirect, or one that could not be stored.
WITHHELD = "🔒 agent-connect: this reply was withheld and cannot be released."
#: The owner's activity row for a Turn that needed no reply.
NO_ACTION_OUTCOME = "The agent had nothing to add."


@dataclass(frozen=True)
class Outcome:
    """How one Turn ended: `kind` is `ANSWERED`, `NO_ACTION` or `FAILED_OUTCOME`.
    `body` is the answer for `ANSWERED`, archived diagnostics for `FAILED_OUTCOME`."""

    kind: str
    body: str = ""
    #: The owner's Activity row labels, already checked; empty when none.
    title: str = ""
    summary: str = ""


def _labelled(outcome: Outcome, labels: "activity.Labels", reason: str = COMPLETED) -> Outcome:
    """Attach the Activity labels. A Turn that failed or did not end normally
    (timeout, cancel, refusal, token limit) keeps its title only: the agent's
    outcome line could claim a success that did not happen."""
    if not labels:
        return outcome
    stopped_short = outcome.kind == FAILED_OUTCOME or reason not in NORMAL_REASONS
    summary = "" if stopped_short else labels.outcome
    return replace(outcome, title=labels.title, summary=summary)


def is_no_reply(text: str) -> bool:
    """The whole reply is the `NO_REPLY` sentinel: exact, never a substring."""
    return (text or "").strip() == NO_REPLY


def mentioned_mxids(body: str, room_members: Sequence[str] = ()) -> List[str]:
    """The full mxids a body names, kept to the room's roster when one is known."""
    found = mxids_in(body)
    if room_members:
        roster = set(room_members)
        found = [mxid for mxid in found if mxid in roster]
    return found


class TurnReporter:
    """Runs one Turn and returns its `Outcome`. `ops` (Room Ops) may be `None`:
    then notices lead the answer instead of being posted."""

    def __init__(self, ops=None):
        self.ops = ops
        self._room = ""
        self._members: tuple = ()
        self._failed_ids: set = set()
        self._started_ids: set = set()
        self._anonymous_failures = 0
        self._refused = 0
        self._diagnostics = deque(maxlen=50)  # archived with a failure only
        self._unposted: List[str] = []
        #: Number of `Notice` events posted as their own message.
        self.notices_posted = 0

    async def run(self, adapter, ctx: TurnContext) -> Outcome:
        self._ctx = ctx
        self._room = ctx.room
        self._members = tuple(ctx.room_members)
        chunks: List[str] = []
        done: Optional[Done] = None
        async for event in adapter.turn(ctx):
            if isinstance(event, MessageChunk):
                chunks.append(event.text)
            elif isinstance(event, Done):
                done = event
            else:
                await self.on_event(event)
        answer, trailer = activity.split(final_text(done, chunks))
        labels = activity.guarded(
            ctx, activity.pick(activity.from_meta(done.meta if done else None), trailer),
            result_guard.verdict)
        if is_no_reply(answer):
            answer = ""
        note = done.note if done else ""
        reason = done.reason if done else COMPLETED
        return _labelled(await self._finish_turn(ctx, answer, note, reason), labels, reason)

    async def _finish_turn(self, ctx: TurnContext, answer: str, note: str, reason: str) -> Outcome:
        held = result_guard.verdict(ctx, f"{answer}\n{note}")
        if held.hold:
            review = None
            if "a redirect to another room" not in held.why:
                review = withheld.hold(ctx.room, answer, held.why, cwd=ctx.cwd)
            if review is None:
                return self.finish(WITHHELD, "", reason)
            asked = await self._ask_owner(ctx, review, answer, held.why)
            return self.finish(withheld.review_message(review, held.why, asked), "", reason)
        return self.finish(answer, note, reason)

    async def on_event(self, event) -> None:
        """Count outcomes for the archive; nothing reaches the room but notices."""
        if isinstance(event, ToolStarted):
            if not event.tool_id or event.tool_id not in self._started_ids:
                self._started_ids.add(event.tool_id)
                self._diagnostics.append((event.title or event.action or "working")[:500])
        elif isinstance(event, ToolFinished):
            if event.status != COMPLETED:
                if event.tool_id:
                    self._failed_ids.add(event.tool_id)
                else:
                    self._anonymous_failures += 1
                self._diagnostics.append(("failed: " + (event.title or event.tool_id or "untitled tool"))[:500])
        elif isinstance(event, PermissionAsked) and not event.allowed:
            self._refused += 1
            reason = f" — {event.reason}" if event.reason else ""
            self._diagnostics.append(f"refused: {event.title}{reason}"[:500])
        elif isinstance(event, Notice):
            await self.notice(event.text)

    def finish(self, answer: str, note: str = "", reason: str = COMPLETED) -> Outcome:
        """The Turn's outcome from its answer, note and stop reason."""
        ending = _ending(reason, note)
        if not answer.strip():
            # An Adapter that explained the failure (not configured, not logged
            # in) is answered with that explanation; a bare failure is the broker's.
            if reason not in NORMAL_REASONS and not (reason == FAILED and (note or "").strip()):
                return self.failed(reason, ending)
            if not ending.strip() and not self._unposted:
                return Outcome(NO_ACTION)
        body = _assemble(answer, ending, self._failure_count(), self._refused, self._unposted)
        if not body:
            return Outcome(NO_ACTION)
        return Outcome(ANSWERED, body)

    def failed(self, reason: str, ending: str = "") -> Outcome:
        """A Turn that produced nothing: the broker posts the failure notice; the
        archived body says why, for whoever reads it."""
        parts = [f"agent-connect: this turn produced no answer (reason: {reason})."]
        parts += [n.strip() for n in self._unposted if n and n.strip()]
        if ending.strip():
            parts.append(ending.strip())
        summary = _summary(self._failure_count(), self._refused)
        if summary:
            parts.append(summary)
        if self._diagnostics:
            parts.append("Operator diagnostics (last 50 events):\n" + "\n".join(self._diagnostics))
        return Outcome(FAILED_OUTCOME, "\n\n".join(parts))

    async def _ask_owner(self, ctx: TurnContext, review: str, answer: str, why) -> bool:
        """Ask the owner privately whether to post a held reply; False if it can't be asked."""
        questions = getattr(self.ops, "questions", None)
        if questions is None:
            return False
        prompt = withheld.owner_prompt(review, answer, why, result_guard.asked_by(ctx),
                                       ctx.room_name or ctx.room)
        question = await questions.ask(prompt, withheld.OPTIONS, expires_in_s=withheld.TTL_S,
                                       operation_id=review)
        if question:
            withheld.bind_question(review, question)
        return bool(question)

    async def notice(self, text: str, keep: bool = True) -> bool:
        """Tell the room a fact about the run, as its own message. One that cannot
        be posted leads the answer when `keep`. Returns whether it was posted."""
        text = (text or "").strip()
        if not text:
            return False
        ctx = getattr(self, "_ctx", None)
        if ctx is not None and result_guard.verdict(ctx, text).hold:
            text = NOTICE_WITHHELD
        if self.ops is not None and self._room and getattr(self.ops, "available", True):
            try:
                # Mentions only for the full ids the notice itself names, never
                # the requester: a notice must not wake the agent that asked.
                await self.ops.message(self._room, text,
                                       [m for m in mentioned_mxids(text, self._members)
                                        if not ctx or m != ctx.user_id])
                self.notices_posted += 1
                return True
            except RoomOpError:
                pass
        if keep:
            self._unposted.append(text)
        return False

    def _failure_count(self) -> int:
        return len(self._failed_ids) + self._anonymous_failures


def _assemble(answer: str, note: str, failed: int, refused: int, notices=()) -> str:
    """The answer, its ending, and a compact summary of what was done.

    Routine tool activity is not appended to the answer. Only aggregate failure
    and refusal notices are retained: tool titles and permission details may
    contain raw arguments, commands or local paths, not public-facing prose.

    `notices` are announcements that could not be posted as their own message
    (no relay, no room, a refused op). They lead the body rather than being
    dropped: a Worker with no room ops must still be able to say that the
    conversation started over.

    A file that could not be sent is *not* assembled here. That refusal is
    written where the refusing happens — the Relay Client appends
    `[attachment not sent: …]` to the body it POSTs — because a sentence about a
    judgement this side did not make would be this side guessing at one.

    Only ever called with something to say: a Turn that produced nothing goes to
    `TurnReporter.reject` instead, and does not travel this path at all.
    """
    parts = [n.strip() for n in notices if n and n.strip()]
    if answer.strip():
        parts.append(answer.strip())
    if note.strip():
        parts.append(note.strip())
    summary = _summary(failed, refused)
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


def _summary(failed: int, refused: int) -> str:
    lines: List[str] = []
    # Do not echo raw tool titles, including failed ones, into the room.
    if failed:
        count = "1 reported tool failure" if failed == 1 else f"{failed} reported tool failures"
        lines.append(f"— {count} during this turn.")
    if refused:
        count = "1 operation" if refused == 1 else f"{refused} operations"
        lines.append(f"— refused ({count}).")
    return "\n".join(lines)
