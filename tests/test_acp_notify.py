"""What the ACP Agent says unasked, and where a Session says its messages belong.

A dialled AG2 Assistant listener keeps a scheduled task's outcome until a client is
connected, then sends it as the `_ag2/notify` extension notification. This suite
drives that against the same fake door `test_acp_websocket` dials: the door now
also records what `session/new` carried and can speak first.

Run: .venv/bin/python tests/test_acp_notify.py   (needs agent-client-protocol[http])
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path
import asyncio
import contextlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def run(coro):
    return asyncio.run(coro)


try:
    from websockets.asyncio.server import serve as ws_serve
    from acp.ws.client import create_websocket_stream  # noqa: F401
except ImportError:
    print("  --   skipped (set up agent-client-protocol[http] to run the wire half)")
    print("PASS — acp notify (settings only)")
    sys.exit(0)

from fake_acp_agent import FakeAcpAgent  # noqa: E402

from agent_connect import worker  # noqa: E402
from agent_connect.acp import AcpClient, Notification  # noqa: E402
from agent_connect.adapters.acp import (  # noqa: E402
    DESTINATION_META_KEY,
    AcpAdapter,
    destination_meta,
)
from agent_connect.events import TurnContext  # noqa: E402
from agent_connect.link import NOTIFY_METHOD, keep_link, room_message  # noqa: E402
from agent_connect.sessions import SessionStore  # noqa: E402

ROOM = "!room:example"


class TalkativeAgent(FakeAcpAgent):
    """The fake agent, which also speaks first: every entry under `notify` in its
    script goes out as an extension notification once `initialize` is answered."""

    def _initialize(self, params):
        result = super()._initialize(params)
        for entry in self.script.get("notify") or []:
            asyncio.ensure_future(self._notify("_" + NOTIFY_METHOD, dict(entry)))
        return result


class Door:
    """A fake ACP Agent behind a WebSocket; keeps every agent it served and can hang up."""

    def __init__(self, script: dict, token: str = "good-token"):
        self.script, self.token = script, token
        self.dials, self.agents, self.connections = 0, [], []
        self._server, self.port = None, 0

    async def __aenter__(self):
        self._server = await ws_serve(self._session, "127.0.0.1", 0, process_request=self._authorise)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/acp"

    def _authorise(self, connection, request):
        if request.headers.get("Authorization", "") != f"Bearer {self.token}":
            return connection.respond(403, "forbidden\n")
        return None

    async def hang_up(self):
        for ws in list(self.connections):
            await ws.close()

    async def _session(self, ws):
        self.dials += 1
        self.connections.append(ws)
        agent = TalkativeAgent(dict(self.script), None)
        self.agents.append(agent)

        async def send(message):
            await ws.send(json.dumps(message))

        agent._send = send  # noqa: SLF001 — swapping the fake's transport is the point
        try:
            async for raw in ws:
                message = json.loads(raw)
                if "method" not in message and "id" in message:
                    fut = agent._pending.pop(message["id"], None)  # noqa: SLF001
                    if fut is not None and not fut.done():
                        fut.set_result(message)
                    continue
                asyncio.ensure_future(agent._handle(message))  # noqa: SLF001
        finally:
            self.connections.remove(ws)


async def until(cond, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


SCRIPT = {"turns": [{"actions": [{"type": "message", "text": "PONG"}],
                     "stopReason": "end_turn"}],
          "notify": [{"destination": ROOM, "text": "✅ nightly: done", "connection": "acp-1"}]}

# --- pure: what a notification asks for --------------------------------------------------

check(room_message(Notification(NOTIFY_METHOD, {"destination": ROOM, "text": "hi"})) == (ROOM, "hi"),
      "an `ag2/notify` with a destination and text is a room message")
check(room_message(Notification(NOTIFY_METHOD, {"destination": ROOM, "text": "  "})) is None,
      "one with nothing to say is not")
check(room_message(Notification("other/thing", {"destination": ROOM, "text": "hi"})) is None,
      "and any other method is left alone")

ctx = TurnContext(prompt="p", task_id="t1", room=ROOM, room_name="Dev", access_tier="owner")
meta = destination_meta(ctx)
check(meta[DESTINATION_META_KEY]["chat_id"] == ROOM and meta[DESTINATION_META_KEY]["access_tier"] == "owner",
      "a Turn's room becomes the Session's destination, with the Tier beside it")
check(destination_meta(TurnContext(prompt="p", task_id="t1")) == {},
      "a roomless Task declares no destination")

# --- the wire: metadata out, notifications in ------------------------------------------------


async def _dial_and_listen():
    async with Door(SCRIPT) as door:
        heard = []
        async with AcpClient.dial(door.url, token=door.token, on_notification=heard.append) as client:
            await client.initialize()
            sid = await client.new_session(
                cwd="/repo", meta={DESTINATION_META_KEY: {"chat_id": ROOM}}
            )
            arrived = await until(lambda: len(heard) >= 1)
        sent = door.agents[0].report["sessions"][0]["params"]
        return sid, sent, heard, arrived


sid, sent, heard, arrived = run(_dial_and_listen())
check(bool(sid), "a Session opens with metadata attached")
check(sent.get("_meta", {}).get(DESTINATION_META_KEY, {}).get("chat_id") == ROOM,
      "and `session/new` carried the destination under `_meta`")
check(arrived and heard[0].method == NOTIFY_METHOD,
      "an extension notification reaches `on_notification` with the underscore dropped")
check(arrived and heard[0].params["destination"] == ROOM and heard[0].params["text"] == "✅ nightly: done",
      "with the params as the agent sent them")


async def _link_posts_and_redials():
    async with Door(SCRIPT) as door:
        posted = []

        async def post(destination, text):
            posted.append((destination, text))

        async def quick(seconds):  # no real waiting in a test
            await asyncio.sleep(0.01)

        stop = asyncio.Event()
        task = asyncio.ensure_future(
            keep_link(door.url, post, token=door.token, stop=stop, sleep=quick)
        )
        first = await until(lambda: len(posted) >= 1)
        await door.hang_up()
        again = await until(lambda: door.dials >= 2 and len(posted) >= 2)
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return posted, door.dials, first, again


posted, dials, first, again = run(_link_posts_and_redials())
check(first and posted[0] == (ROOM, "✅ nightly: done"),
      "the standing link posts what the agent says unasked, to the room it names")
check(again and dials >= 2, "and dials again after the door hangs up")


async def _adapter_declares_and_posts():
    with tempfile.TemporaryDirectory() as tmp:
        async with Door(SCRIPT) as door:
            adapter = AcpAdapter(url=door.url, token=door.token,
                                 store=SessionStore(Path(tmp) / "sessions.json"))
            posted = []

            async def post(destination, text):
                posted.append((destination, text))

            adapter.set_notifier(post)
            text = await worker.run_turn(adapter, ctx)
            await until(lambda: len(posted) >= 1, 2)
            sent = door.agents[0].report["sessions"][0]["params"]
            return text, sent, posted


text, sent, posted = run(_adapter_declares_and_posts())
check("PONG" in text, "an Adapter Turn still answers over the door")
check(sent.get("_meta", {}).get(DESTINATION_META_KEY, {}).get("chat_id") == ROOM,
      "and opens its Session with the room as the destination")
check(posted and posted[0] == (ROOM, "✅ nightly: done"),
      "a notification arriving during the Turn's own dial is posted too")


class FakeOps:
    def __init__(self):
        self.sent = []

    async def message(self, room, body, mentions=None):
        self.sent.append((room, body))
        return "$event"


async def _worker_starts_a_link():
    async with Door(SCRIPT) as door:
        adapter = AcpAdapter(url=door.url, token=door.token)
        ops = FakeOps()
        link = worker.start_listener_link(adapter, ops)
        is_task = isinstance(link, asyncio.Task)
        delivered = await until(lambda: len(ops.sent) >= 1)
        if link is not None:
            link.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await link
        none_without_ops = worker.start_listener_link(adapter, None)
        spawned = worker.start_listener_link(AcpAdapter(command=["fake-agent"]), ops)
        return is_task, delivered, ops.sent, none_without_ops, spawned


is_task, delivered, sent_by_worker, none_without_ops, spawned = run(_worker_starts_a_link())
check(is_task and delivered and sent_by_worker[0] == (ROOM, "✅ nightly: done"),
      "the Worker holds a standing link to a dialled agent and posts through Room Ops")
check(none_without_ops is None, "a Worker without Room Ops holds no link")
check(spawned is None, "and a spawned agent gets none: it is only there during a Turn")

print()
if fails:
    print(f"FAIL — {fails} failing")
    sys.exit(1)
print("PASS — acp notify green")
