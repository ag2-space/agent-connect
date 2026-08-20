"""Room Ops: the Worker's only way of speaking in a room.

A Room Op is an action the relay performs in a room *as* the Agent Identity —
post, edit, react, upload. This module asks for two of them, over plain HTTP,
with the bearer token the Worker already holds. It never speaks Matrix: the
relay is the only Matrix speaker in this system, and `docs/adr/0002` records why
that boundary is not ours to cross.

Two ops are all the Ladder needs: post the placeholder and keep its identifier,
then edit that same message. Reactions are not among them — **the broker places
the intake reaction itself, and the Worker must not**, or the room sees it
twice.

**Failure is not fatal, ever.** A room that cannot be spoken to is a room whose
answer arrives the old way: through the Task's result, which the relay client
posts. So every failure here is caught, the instance marks itself unavailable so
the next Task does not pay the same timeout again, and the Ladder degrades to
the plain result path rather than losing the answer. The one thing this module
must never do is raise into the Worker loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Mapping, Optional

from ag2_relay_client.credentials import parse_onboarding_token

from .relay import URL_ENV
from .relay import token as relay_token

#: The relay's room-op endpoint (`docs/adr/0002`).
ROOM_PATH = "/v1/room"

#: Same default the installer writes into the relay client's launcher, so the
#: Worker and the relay client talk to the same gateway without a new setting.
DEFAULT_URL = "https://chat.ag2.space/relay"

#: The relay client sends this; CloudFlare's bot-fight rejects urllib's default.
USER_AGENT = "sutando-gateway-client/1.0"


class RoomOpError(Exception):
    """A Room Op the relay did not perform."""


class RoomOps:
    """The relay's room endpoint, as the two calls the Ladder makes.

    Instances are shared across Tasks — `available` is why. The first failure
    turns it off for the life of the Worker: a relay that does not do room ops
    will not start doing them mid-run, and a per-Task retry would add its
    timeout to every answer for nothing.
    """

    def __init__(self, url: str, token: str, timeout: float = 15.0):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        #: False once a Room Op has failed; the Ladder then stays out of the way.
        self.available = True

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<RoomOps {self.url} available={self.available}>"

    async def message(self, room: str, body: str) -> str:
        """Post a message as the Agent Identity; return its event identifier.

        The identifier is the whole point: without it there is nothing to edit,
        and the Ladder collapses into a stream of separate messages.
        """
        # The relay reads only `room_id` (WORKER-PROTOCOL.md); a `room` key is
        # ignored and the op fails with a 400.
        reply = await self._call({"op": "message", "room_id": room, "body": body})
        event_id = _event_id(reply)
        if not event_id:
            raise RoomOpError("the relay posted the message but returned no event id")
        return event_id

    async def edit(self, room: str, event_id: str, body: str) -> None:
        """Replace the body of a message this Agent Identity posted."""
        await self._call(
            {"op": "edit", "room_id": room, "event_id": event_id, "body": body}
        )

    async def _call(self, payload: dict) -> dict:
        if not self.available:
            raise RoomOpError("room ops are switched off after an earlier failure")
        try:
            return await asyncio.to_thread(self._request, payload)
        except Exception as exc:  # noqa: BLE001 — one failure, then out of the way
            self.available = False
            print(
                f"agent-connect: room ops disabled — {payload.get('op')} failed: {exc}",
                file=sys.stderr, flush=True,
            )
            raise RoomOpError(str(exc)) from exc

    def _request(self, payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(f"{self.url}{ROOM_PATH}", data=data, method="POST")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode().strip()
        return json.loads(raw) if raw else {}


def _event_id(reply: dict) -> str:
    """The posted message's identifier, under whichever name it came back."""
    if not isinstance(reply, dict):
        return ""
    for key in ("event_id", "eventId", "id"):
        value = reply.get(key)
        if isinstance(value, str) and value:
            return value
    inner = reply.get("result") or reply.get("data")
    if isinstance(inner, dict):
        return _event_id(inner)
    return ""


def room_ops_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[RoomOps]:
    """The relay to speak to, or `None` when this Worker holds no token.

    The credential is the one the Worker already has, read by
    `agent_connect.relay.token` and by nothing of this module's own: one reader,
    so the Ladder and the wire cannot end up pointed at two different bearers.

    **The gateway is chosen the way the library chooses it**, and for the same
    reason: the URL that travels inside a combined `https://gateway|secret`
    token *is* the gateway that credential belongs to, and `REMOTE_TASK_URL` is
    for a bare secret that carries none. This module used to let the environment
    outrank the token, so a stale `REMOTE_TASK_URL` beside a combined token
    produced one process talking to two gateways — the wire at the token's, the
    Ladder at the environment's, with only a log line about it. The split itself
    is the library's `parse_onboarding_token`, so `%7C` and a bearer containing
    a pipe mean here exactly what they mean there.

    No token means no Ladder — a workspace driven by hand, or a test, keeps the
    plain behaviour where the answer travels as the Task's result.
    """
    env = os.environ if env is None else env
    raw = relay_token(env)
    if not raw:
        return None
    url_from_token, token = parse_onboarding_token(raw)
    url = (url_from_token or env.get(URL_ENV) or DEFAULT_URL).strip()
    if not token or not url:
        return None
    return RoomOps(url, token)
