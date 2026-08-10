"""What happens to a Task that arrives while its Session is busy.

Only one [[Turn]] at a time may be open on a [[Session]] — that is the
protocol's rule, not a preference — so a second Task for the same *(room, Access
Tier)* has to wait for the first. Two things about that waiting are worth a
module of their own.

**It is per Session, and only per Session.** A room whose agent is thinking must
not stop any other room's, so the queue is keyed by exactly the pair the Session
is keyed by (`TurnContext.session_key`, which is also what the Adapter looks a
Session up by). One queue per key, and Tasks on different keys never meet.

**A wait nobody was told about is indistinguishable from a message that was
dropped.** So arrival is a decision that is *reported*, rather than a lock that
is silently taken: `arrive()` answers "is this Turn waiting, and behind how
many?" before anything blocks, which is what lets the Worker say so in the room
while it is still true.

## Shaped for Steering, which is not implemented here

[[Steering]] — injecting a message into a Turn that is already running so it
changes course instead of queueing behind — is an extension the Claude bridge
already offers, and adopting it must be an addition rather than a rewrite. That
is why this is a structure with a named admission step and a handle on the Turn
that is currently running, rather than a bare `asyncio.Lock`:

* `SessionQueue.arrive(ctx)` is the **one place** where a Task's fate is
  decided. Steering enters as a third answer alongside "run now" and "wait":
  ask `self.running` whether it will take the message, and if it does, this Task
  never becomes a Turn at all.
* `SessionQueue.running` is the handle that question needs. It is a
  `PendingTurn`, alive for exactly as long as the Turn is, and it is where a
  `steer()` coroutine would hang — the Adapter's `session/prompt` extension has
  to reach *that* Turn, not the Session in the abstract.
* `PendingTurn.ahead` is already the answer to "was anything running?", so the
  room-facing wording ("queued" versus "steered into the running turn") is a
  branch in the Worker, not a new mechanism.

Nothing here implements any of that, deliberately: how a redirected Turn should
be *presented* in a room is undesigned, and the spec puts Steering out of scope.
What this module promises is that adopting it touches `arrive()` and a new
method on `PendingTurn`, and touches nothing else.

This module imports nothing from the rest of the package. It is about waiting,
not about ACP, rooms or Tasks.
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

#: A Session key as it travels: (room, Access Tier). The same pair
#: `TurnContext.session_key` produces and `agent_connect.sessions` maps.
Key = Tuple[str, str]


class PendingTurn:
    """One Task's claim on a Session: waiting, then holding, then done.

    Used as an asynchronous context manager, so a Turn cannot forget to release
    the Session it took — including when the Turn raised:

        turn = queue.arrive(ctx)
        if turn.queued:
            ...tell the room...
        async with turn:
            ...run the Turn...
    """

    __slots__ = ("ctx", "ahead", "on_wait", "_queue", "_held")

    def __init__(self, ctx, queue: "SessionQueue", ahead: int, on_wait=None):
        self.ctx = ctx
        #: How many Turns were outstanding on this Session when this one
        #: arrived — running or already waiting. Zero means it starts at once.
        self.ahead = ahead
        #: Called with this Turn, once, if it has to wait — before it does.
        #: Announcing the wait after it ended would be telling someone their
        #: message was queued at the same moment as its answer.
        self.on_wait = on_wait
        self._queue = queue
        self._held = False

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<PendingTurn key={self._queue.key} ahead={self.ahead}>"

    @property
    def queued(self) -> bool:
        """Whether this Task has to wait for someone else's Turn to finish."""
        return self.ahead > 0

    async def __aenter__(self) -> "PendingTurn":
        try:
            if self.queued and self.on_wait is not None:
                await self.on_wait(self)
            await self._queue._take(self)
        except BaseException:
            # It never held the Session, so it must not be left counting
            # against the next arrival — a phantom in the queue would tell
            # every later Task it was waiting for something that is not there.
            self._queue._drop(self)
            raise
        self._held = True
        return self

    async def __aexit__(self, *exc) -> bool:
        if self._held:
            self._held = False
            self._queue._give_back(self)
        return False


class SessionQueue:
    """The Turns outstanding on one Session: at most one running, the rest waiting.

    The mutual exclusion is an `asyncio.Lock`, which is first-in-first-out, so
    two people's messages are answered in the order they were sent. What this
    adds over holding the lock directly is that the queue *knows* it is a queue:
    who is running, who is waiting, and how many — the facts a room has to be
    told, and the handle Steering will need.
    """

    def __init__(self, key: Key = ("", "")):
        self.key = key
        self._lock = asyncio.Lock()
        #: The Turn holding the Session right now, or `None`.
        self.running: Optional[PendingTurn] = None
        #: Every Turn admitted and not yet finished, in arrival order — the one
        #: at the head is usually the one `running`. A Turn stays here until it
        #: releases the Session, so the length is what an arriving Task is
        #: behind, counted without a race.
        self.outstanding: List[PendingTurn] = []

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<SessionQueue {self.key} running={self.running is not None} waiting={len(self.waiting)}>"

    @property
    def busy(self) -> bool:
        return bool(self.outstanding)

    def arrive(self, ctx, on_wait=None) -> PendingTurn:
        """Admit one Task to this Session and say what is happening to it.

        Synchronous on purpose. The count of Turns ahead is only true until the
        next `await`, and the caller needs it to decide whether to announce the
        wait *before* it starts waiting. Awaiting inside here would race a
        second Task into the same answer.

        This is the seam Steering enters at — see the module docstring.
        """
        turn = PendingTurn(ctx, self, ahead=len(self.outstanding), on_wait=on_wait)
        self.outstanding.append(turn)
        return turn

    # -- held by PendingTurn ------------------------------------------------

    def _drop(self, turn: PendingTurn) -> None:
        if turn in self.outstanding:
            self.outstanding.remove(turn)

    async def _take(self, turn: PendingTurn) -> None:
        await self._lock.acquire()
        self.running = turn

    def _give_back(self, turn: PendingTurn) -> None:
        if self.running is turn:
            self.running = None
        if turn in self.outstanding:
            self.outstanding.remove(turn)
        self._lock.release()


def queue_for(registry: Dict[Key, SessionQueue], key: Key) -> SessionQueue:
    """The queue for one Session key, created on first sight.

    The registry is the Worker's — one dict for the life of the process, so two
    Tasks in one room find the same queue. Nothing is ever removed from it: a
    queue is two empty lists and a lock, and a room that spoke once may speak
    again.
    """
    queue = registry.get(key)
    if queue is None:
        queue = registry[key] = SessionQueue(key)
    return queue
