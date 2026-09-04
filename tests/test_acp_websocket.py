"""ACP over WebSocket: the dialled door, and the settings that pick it.

Two halves. The **settings** half is pure and always runs: precedence, the
mutual exclusion, the loopback rule, the scheme check. The **wire** half needs
the SDK's transport extra (`pip install 'agent-client-protocol[http]'`) and a
real socket, and skips with a sentence rather than failing when it is absent —
the stdio path does not need the extra and neither does most of this suite.

The door the wire half dials is `fake_acp_agent.FakeAcpAgent`, unchanged, with
its two I/O methods pointed at a WebSocket instead of stdin/stdout. That is the
point: the same scripted agent every stdio test uses, reached the other way.

Run: .venv/bin/python tests/test_acp_websocket.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from agent_connect.acp import AcpClient, AcpDialFailed, AcpError  # noqa: E402
from agent_connect.adapters.acp import (  # noqa: E402
    ALLOW_REMOTE_ENV,
    COMMAND_ENV,
    TOKEN_ENV,
    URL_ENV,
    Endpoint,
    resolve_endpoint,
    resolve_url,
)

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def run(coro):
    return asyncio.run(coro)


# --- settings: which agent, and may we dial it ------------------------------

env = {URL_ENV: "ws://127.0.0.1:8802/acp", TOKEN_ENV: "s3cret"}
endpoint = resolve_endpoint(env)
check(endpoint.dialled and endpoint.url == "ws://127.0.0.1:8802/acp",
      "a URL alone resolves to a dialled endpoint")
check(endpoint.token == "s3cret", "and carries the bearer read from its own setting")
check(endpoint.command == (), "and names no command, so nothing can be spawned for it")

endpoint = resolve_endpoint({COMMAND_ENV: "my-agent --acp"})
check(not endpoint.dialled and endpoint.command == ("my-agent", "--acp"),
      "a command alone still resolves the way it always did")

try:
    resolve_endpoint({URL_ENV: "ws://127.0.0.1:8802", COMMAND_ENV: "my-agent"})
    both = ""
except AcpError as exc:
    both = str(exc)
check("both set" in both, "a URL and a command together are refused, not resolved")
check(URL_ENV in both and COMMAND_ENV in both,
      "and the refusal names both, so the operator knows which to unset")

for bad in ("http://127.0.0.1:8802", "127.0.0.1:8802", "https://x/acp"):
    try:
        resolve_url(bad, {})
        said = ""
    except AcpError as exc:
        said = str(exc)
    check("WebSocket URL" in said, f"{bad!r} is refused: the remote transport is WebSocket")

try:
    resolve_url("ws://10.0.0.5:8802/acp", {})
    remote = ""
except AcpError as exc:
    remote = str(exc)
check(ALLOW_REMOTE_ENV in remote,
      "a non-loopback host is refused by default, and the refusal names the opt-out")
check("this machine" in remote or "guess" in remote,
      "and says why: the Permission Policy resolves paths on this filesystem")

check(resolve_url("ws://10.0.0.5:8802/acp", {ALLOW_REMOTE_ENV: "1"}) ==
      "ws://10.0.0.5:8802/acp",
      "and the opt-out is honoured once it is set on purpose")
for host in ("ws://localhost:8802/acp", "ws://127.0.0.1:8802/acp", "ws://[::1]:8802/acp"):
    check(resolve_url(host, {}) == host, f"{host} needs no opt-out")

check(Endpoint(url="ws://x").dialled and not Endpoint(command=("a",)).dialled,
      "`dialled` is what the rest of the Adapter branches on")

# --- the wire ---------------------------------------------------------------

try:
    import websockets
    from websockets.asyncio.server import serve as ws_serve

    from acp.ws.client import create_websocket_stream  # noqa: F401
except ImportError:
    print("\nSKIP — the wire half needs the SDK's transport extra:")
    print("       .venv/bin/python -m pip install 'agent-client-protocol[http]'")
    print("\n" + ("PASS — acp websocket settings green" if fails == 0
                  else f"FAIL — {fails} failing"))
    raise SystemExit(1 if fails else 0)

from fake_acp_agent import FakeAcpAgent  # noqa: E402


class Door:
    """The fake ACP Agent behind a WebSocket, with a bearer checked at upgrade.

    Sessions are held per *connection*, as a real listener holds them, so a
    second dial only finds an earlier Session if the door was told to remember
    it — which is the whole question a per-Turn dialler asks.
    """

    def __init__(self, script: dict, token: str = "good-token"):
        self.script = script
        self.token = token
        self.dials = 0
        self.rejected = 0
        self._server = None
        self.port = 0

    async def __aenter__(self):
        self._server = await ws_serve(
            self._session, "127.0.0.1", 0, process_request=self._authorise
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/acp"

    def _authorise(self, connection, request):
        """Refuse a bad bearer at the handshake, before any ACP frame exists."""
        offered = request.headers.get("Authorization", "")
        if offered != f"Bearer {self.token}":
            self.rejected += 1
            return connection.respond(403, "forbidden\n")
        return None

    async def _session(self, ws):
        self.dials += 1
        agent = FakeAcpAgent(dict(self.script), None)

        async def send(message):
            await ws.send(json.dumps(message))

        agent._send = send  # noqa: SLF001 — swapping the fake's transport is the point
        async for raw in ws:
            message = json.loads(raw)
            if "method" not in message and "id" in message:
                fut = agent._pending.pop(message["id"], None)  # noqa: SLF001
                if fut is not None and not fut.done():
                    fut.set_result(message)
                continue
            asyncio.ensure_future(agent._handle(message))  # noqa: SLF001


SCRIPT = {"turns": [{"actions": [{"type": "message", "text": "PONG"}],
                     "stopReason": "end_turn"}]}


async def _dial_and_turn():
    async with Door(SCRIPT) as door:
        async with AcpClient.dial(door.url, token=door.token) as client:
            agent = await client.initialize()
            session_id = await client.new_session(cwd="/repo")
            turn = await client.prompt(session_id, "ping")
            alive = client.alive
        return agent, session_id, turn, alive, door


agent, session_id, turn, alive, door = run(_dial_and_turn())
check(agent is not None, "a dialled Agent answers `initialize`")
check(bool(session_id), "and opens a Session over the socket")
check(turn.text == "PONG" and turn.stop_reason == "end_turn",
      "and runs a Turn whose text and stop reason arrive intact")
check(alive is True, "`alive` answers without a process to ask about")
check(door.dials == 1, "one dial served the whole Turn")


async def _bad_token():
    async with Door(SCRIPT, token="good-token") as door:
        try:
            async with AcpClient.dial(door.url, token="wrong-token"):
                return None, door
        except AcpDialFailed as exc:
            return exc, door


exc, door = run(_bad_token())
check(isinstance(exc, AcpDialFailed), "a bad bearer fails the dial, as its own class")
check("403" in str(exc) or "refused" in str(exc),
      "and says the door refused it rather than blaming the Turn")
check(door.rejected == 1 and door.dials == 0,
      "refused at the upgrade: no ACP frame was ever exchanged")


async def _no_token_offered():
    async with Door(SCRIPT) as door:
        try:
            async with AcpClient.dial(door.url):
                return None
        except AcpDialFailed as exc:
            return exc


check(isinstance(run(_no_token_offered()), AcpDialFailed),
      "a door that wants a bearer refuses a dial that brings none")


async def _two_dials():
    """The per-Turn shape: a socket per Turn, against one Session id."""
    async with Door(SCRIPT) as door:
        async with AcpClient.dial(door.url, token=door.token) as client:
            await client.initialize()
            first = await client.new_session(cwd="/repo")
        async with AcpClient.dial(door.url, token=door.token) as client:
            await client.initialize()
            await client.load_session(first, cwd="/repo")
            turn = await client.prompt(first, "again")
        return first, turn, door


first, turn, door = run(_two_dials())
check(door.dials == 2, "two Turns, two dials — the shape the Adapter uses")
check(turn.text == "PONG", "and the second dial runs its Turn on the first's Session id")


async def _door_vanishes():
    async with Door(SCRIPT) as door:
        client_box = {}
        async with AcpClient.dial(door.url, token=door.token) as client:
            await client.initialize()
            client_box["c"] = client
            await client.close()
            return client_box["c"].alive


check(run(_door_vanishes()) is False,
      "a closed link reports itself dead without a process to look at")

# --- the Adapter over a dialled door ----------------------------------------

from agent_connect import worker  # noqa: E402
from agent_connect.adapters.acp import AcpAdapter  # noqa: E402
from agent_connect.events import TurnContext  # noqa: E402
from agent_connect.sessions import SessionStore  # noqa: E402

import tempfile  # noqa: E402


async def _adapter_turn(door, adapter, prompt="hello", cwd=None):
    ctx = TurnContext(prompt=prompt, task_id="t1", room="!r", access_tier="owner",
                      cwd=cwd or "/repo")
    return await worker.run_turn(adapter, ctx)


async def _adapter_over_ws():
    with tempfile.TemporaryDirectory() as tmp:
        async with Door(SCRIPT) as door:
            adapter = AcpAdapter(url=door.url, token=door.token,
                                 store=SessionStore(Path(tmp) / "sessions.json"))
            first = await _adapter_turn(door, adapter)
            # A second Turn from a Worker whose own directory has moved. Over a
            # socket the stored cwd is the remote's, so this must NOT retire it.
            second = await _adapter_turn(door, adapter, cwd="/somewhere/else")
            return first, second, door


first, second, door = run(_adapter_over_ws())
check("PONG" in first, "the Adapter runs a Turn over a dialled door")
check(door.dials == 2, "and dials once per Turn, as it spawns once per Turn")
check("fresh conversation" not in second and "working directory changed" not in second,
      "a Worker whose own cwd moved does not retire a dialled Session: "
      "that directory belongs to the remote")
check("PONG" in second, "and the second Turn answers normally")


async def _adapter_dial_failure():
    with tempfile.TemporaryDirectory() as tmp:
        async with Door(SCRIPT) as door:
            url = door.url
        # Door is closed now: nothing is listening on that port.
        adapter = AcpAdapter(url=url, token="t",
                             store=SessionStore(Path(tmp) / "sessions.json"))
        return await _adapter_turn(None, adapter)


said = run(_adapter_dial_failure())
check("not installed" not in said and "npm install" not in said,
      "a door that is down never produces install advice for a bridge")
check("reach" in said or "connect" in said or "refused" in said,
      "it says the Agent could not be reached")


# --- the directory a dialled Session is opened in ----------------------------


async def _cwd_sent(remote_cwd=None):
    with tempfile.TemporaryDirectory() as tmp:
        async with Door(SCRIPT) as door:
            seen = []
            original = FakeAcpAgent._new_session

            def spy(agent_self, params):
                seen.append(params.get("cwd"))
                return original(agent_self, params)

            FakeAcpAgent._new_session = spy
            try:
                adapter = AcpAdapter(url=door.url, token=door.token,
                                     remote_cwd=remote_cwd,
                                     store=SessionStore(Path(tmp) / "s.json"))
                await _adapter_turn(door, adapter, cwd="/local/repo")
            finally:
                FakeAcpAgent._new_session = original
            return seen


seen = run(_cwd_sent())
check(seen == ["/local/repo"],
      "with no remote directory set, a dialled Session opens in the Worker's own "
      "— which on loopback is a real path on the same filesystem")

seen = run(_cwd_sent(remote_cwd="/srv/agent/workspace"))
check(seen == ["/srv/agent/workspace"],
      "and the remote directory setting is what is sent when the operator gives one")


# --- optional: the same, against a real listener ----------------------------
# Gated like the real-bridge test: it needs somebody's actual door.
#     AGENT_CONNECT_ACP_URL=ws://127.0.0.1:8802/acp \
#     AGENT_CONNECT_ACP_TOKEN=... ACP_LIVE_DOOR=1 .venv/bin/python tests/test_acp_websocket.py
import os  # noqa: E402

if os.environ.get("ACP_LIVE_DOOR") == "1":
    live_url = os.environ.get(URL_ENV, "")
    live_token = os.environ.get(TOKEN_ENV, "")

    async def _live():
        async with AcpClient.dial(live_url, token=live_token) as client:
            agent = await client.initialize()
            sid = await client.new_session(cwd=os.getcwd())
            turn = await client.prompt(sid, "Reply with exactly: TRANSPORT-OK")
            return agent, sid, turn

    agent, sid, turn = run(_live())
    check(agent.name != "", f"the live door identifies itself ({agent.name} {agent.version})")
    check("TRANSPORT-OK" in (turn.text or ""), "and answers a Turn over the socket")

    async def _live_second(sid):
        async with AcpClient.dial(live_url, token=live_token) as client:
            await client.initialize()
            await client.load_session(sid, cwd=os.getcwd())
            return await client.prompt(sid, "What exact word did I ask for? One word.")

    turn = run(_live_second(sid))
    check("TRANSPORT-OK" in (turn.text or ""),
          "and a second dial resumes that Session with its history intact")
else:
    print("  --   live-door checks skipped (set ACP_LIVE_DOOR=1 with URL and TOKEN)")

print("\n" + ("PASS — acp websocket green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
