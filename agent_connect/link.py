"""The Listener Link: a standing dial to a remote ACP Agent, for what it says unasked.

Every Turn dials the listener and hangs up when the Turn ends (`docs/adr/0004`), so
between Turns nothing of this Worker is connected to it. That is fine for answers,
which arrive inside a Turn, and useless for a message the agent sends on its own —
a scheduled task finishing hours after the room asked for it. The agent keeps such a
message until a client is connected; this module is the client that is.

One coroutine, `keep_link`, dials, waits while the link lives, and dials again after a
pause when it dies. Notifications arriving on it go to `post`, the same way a Turn's
own dial hands them on. It never sends a prompt and never opens a Session: it is a
listener on the client side of the socket, nothing more — and it says so on
`initialize` (`receiver_meta`), which is what makes the agent hand it the queue.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, Callable, Optional

from .acp import AcpClient, Notification

#: The notification an AG2 Assistant listener sends for a task-run outcome:
#: `{"destination": <chat id>, "text": <message>, ...}`.
NOTIFY_METHOD = "ag2/notify"

#: The `_meta` key a Client sets to `true` on `initialize` to say it takes
#: `_ag2/notify` and delivers it. The agent flushes what it has queued to such a
#: Client and to no other (AG2 Assistant, ADR 0049). A connection that says
#: nothing — the Worker's startup preflight, which dials, initializes and hangs up
#: before there is any Room Ops to post with — is a probe, and is handed nothing:
#: that is how what was queued while the Worker was down survives the Worker's
#: own restart instead of vanishing into its first connection.
RECEIVE_META_KEY = "ag2/receive"


def receiver_meta() -> dict:
    """What a Client that can deliver notifications says on `initialize`."""
    return {RECEIVE_META_KEY: True}

#: How long to wait before dialling again after the link died, growing to a cap.
BACKOFF_S = (5.0, 10.0, 20.0, 60.0)
#: How often the link checks that its socket is still open.
HEARTBEAT_S = 5.0

Poster = Callable[[str, str], Awaitable[object]]


def room_message(notification: Notification) -> Optional[tuple[str, str]]:
    """The (destination, text) a notification asks to post, or None for any other."""
    if notification.method != NOTIFY_METHOD:
        return None
    destination = str(notification.params.get("destination") or "").strip()
    text = str(notification.params.get("text") or "").strip()
    if not destination or not text:
        return None
    return destination, text


def poster_for(post: Poster) -> Callable[[Notification], Awaitable[None]]:
    """A notification handler that posts a room message where the agent says."""

    async def handle(notification: Notification) -> None:
        message = room_message(notification)
        if message is None:
            return
        destination, text = message
        await post(destination, text)

    return handle


async def keep_link(
    url: str,
    post: Poster,
    *,
    token: str = "",
    cookie_store=None,
    stop: Optional[asyncio.Event] = None,
    backoff=BACKOFF_S,
    heartbeat_s: float = HEARTBEAT_S,
    sleep=asyncio.sleep,
) -> None:
    """Hold one dial to `url` open until `stop` is set, re-dialling when it dies."""
    stop = stop or asyncio.Event()
    handle = poster_for(post)
    attempt = 0
    while not stop.is_set():
        try:
            async with AcpClient.dial(
                url, token=token, cookie_store=cookie_store, on_notification=handle
            ) as client:
                await client.initialize(meta=receiver_meta())
                attempt = 0
                while client.alive and not stop.is_set():
                    await sleep(heartbeat_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a link that is down is retried, not fatal
            print(f"agent-connect: listener link: {exc}", file=sys.stderr)
        if stop.is_set():
            return
        pause = backoff[min(attempt, len(backoff) - 1)]
        attempt += 1
        await sleep(pause)
