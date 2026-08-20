"""Room Ops: the Worker's only way of speaking in a room.

A Room Op is an action the relay performs in a room *as* the Agent Identity —
post, edit, react, upload. This module asks for two of them. It never speaks
Matrix: the relay is the only Matrix speaker in this system, and
`docs/adr/0002` records why that boundary is not ours to cross.

**It no longer speaks HTTP either.** This module used to hold a `urllib` POST, a
bearer header, a User-Agent copied out of the relay client with a comment saying
so, a hardcoded `https://chat.ag2.space/relay`, and a naive literal-`|` split of
the combined onboarding token — one of four copies of that parse in the
workspace, and the one that let a stale `REMOTE_TASK_URL` point the Ladder at a
different gateway than the wire. All of it is the Relay Client's now
(workspace `docs/adr/0001`), which is where it always belonged: the Worker was
speaking to `/v1/room` beside `/v1/tasks` with the same bearer, and that is one
speaker, not two.

What is left is the shape of the seam: **the library is sync, this side is
asyncio.** So every op is awaited on a daemon thread of its own
(`agent_connect.relay.in_daemon_thread`) rather than on the event loop.

Two ops are all the Ladder needs: post the placeholder and keep its identifier,
then edit that same message. Reactions are not among them — **the broker places
the intake reaction itself, and the Worker must not**, or the room sees it
twice. The library now enforces that rather than trusting it: the wire loop
tells its Room Ops which events it was served, and a `react` on one is refused.

**Failure is not fatal, ever.** A room that cannot be spoken to is a room whose
answer arrives the plain way, through the Task's result. That promise is the
library's and it is kept there — no method on `ag2_relay_client.roomops.RoomOps`
raises; a failure answers `None` or `False` and buys a **time-gated** cooldown
(~300 s), not the process-lifetime latch this module used to keep. A broker that
starts doing room ops again after a deploy is picked up without a restart.

This module turns those falsy answers into `RoomOpError` for the one caller that
wants to branch on them — the `TurnReporter`, whose Ladder needs to know whether
a placeholder exists. Raising *here* is safe in a way raising on the wire is
not: every `raise` below is inside a `try` in the reporter, and the answer is
already on its way through `complete` regardless.
"""
from __future__ import annotations

from typing import Optional

from .relay import in_daemon_thread


class RoomOpError(Exception):
    """A Room Op the relay did not perform."""


class RoomOps:
    """The library's Room Ops, awaited from the event loop.

    Holds one `ag2_relay_client.roomops.RoomOps`, shared across Tasks — the
    cooldown is why. A relay that is not doing room ops for one Task is not
    doing them for the next one either, and finding that out per Task costs
    every answer the timeout.
    """

    def __init__(self, ops):
        #: The library's. Everything about the wire — the URL, the bearer, the
        #: User-Agent, the retries, the cooldown — is on the other side of it.
        self.ops = ops

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<RoomOps {self.ops!r}>"

    @property
    def available(self) -> bool:
        """False while the cooldown from the last failure is still running."""
        return bool(getattr(self.ops, "available", True))

    async def message(self, room: str, body: str) -> str:
        """Post a message as the Agent Identity; return its event identifier.

        The identifier is the whole point: without it there is nothing to edit,
        and the Ladder collapses into a stream of separate messages.
        """
        event_id = await in_daemon_thread(self.ops.message, room, body)
        if not event_id:
            raise RoomOpError("the relay did not post the message, or named no "
                              "event id for it")
        return event_id

    async def edit(self, room: str, event_id: str, body: str) -> None:
        """Replace the body of a message this Agent Identity posted."""
        if not await in_daemon_thread(self.ops.edit, room, event_id, body):
            raise RoomOpError("the relay did not edit the message")


def room_ops_for(client) -> Optional[RoomOps]:
    """The Ladder's Room Ops, taken off the Relay Client the Worker built.

    One object, one bearer, one gateway. The Ladder used to build its own from
    the environment, which is how a single process ended up talking to two
    gateways — the wire at the token's, the Ladder at `REMOTE_TASK_URL`'s, with
    only a log line about it. There is nothing left here to disagree with: the
    client already resolved the credential, and this asks it for what it built.

    `None` when there is no client — a workspace driven by hand, or a test —
    and then the answer travels whole in the Task's result, as it did before the
    Ladder existed.
    """
    ops = getattr(client, "room_ops", None)
    return RoomOps(ops) if ops is not None else None
