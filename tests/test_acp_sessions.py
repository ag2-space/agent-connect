"""Tests for Sessions: the conversation a room continues, and how it ends.

Asserted at the Worker's handle-one-Task seam, against a real fake ACP Agent
child process. Two observables, and nothing else:

* **What the agent saw** — its own report. Each Turn is a separate ACP Agent
  process (the Adapter opens one per Turn), so each Turn gets its own report
  file, and "did this Turn resume or start over?" is `session/load` versus
  `session/new` in the methods the agent received. That is the Session count and
  its parameters, observed from outside the Worker.
* **What the room received** — every Room Op a real local HTTP relay recorded.
  Replay suppression is asserted as the *absence* of ops: a resumed Session
  replaying a dozen tool calls must produce exactly the two ops a silent Turn
  produces, and none of their bodies may carry a word of the old transcript.

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: .venv/bin/python tests/test_acp_sessions.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from agent_connect.adapters.acp import AcpAdapter
    from agent_connect.events import TurnContext
    from agent_connect.reporter import PLACEHOLDER, REPLIED, LadderSettings
    from agent_connect.roomops import RoomOps
    from agent_connect.sessions import (
        SessionRecord,
        SessionSettings,
        SessionStore,
        store_path,
        workspace_dir,
    )
    from agent_connect.worker import handle_one
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_sessions.py: {exc}\n"
        "This test has a dependency (see docs/adr/0001). Run it from an\n"
        "environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python tests/test_acp_sessions.py"
    )

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")
ROOM_A = "!alpha:ag2.space"
ROOM_B = "!beta:ag2.space"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# ---------------------------------------------------------------------------
# The fake relay: a real HTTP server, recording the Room Ops it receives.
# ---------------------------------------------------------------------------

class FakeRelay:
    """The relay's `POST /v1/room`, as far as the Worker can tell."""

    def __init__(self):
        self.ops = []
        self._n = 0
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                relay.ops.append(json.loads(self.rfile.read(length) or b"{}"))
                relay._n += 1
                raw = json.dumps({"event_id": f"$ev{relay._n}"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def ops_client(self):
        return RoomOps(f"http://127.0.0.1:{self.server.server_port}", "test-token")

    def of(self, kind):
        return [o for o in self.ops if o.get("op") == kind]

    def bodies(self):
        return " ".join(o.get("body", "") for o in self.ops)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


ANSWERS = {"defaultTurn": {"actions": [{"type": "message", "text": "answered"}],
                           "stopReason": "end_turn"}}


class Bench:
    """One Worker workspace and one Session map, across as many Tasks as asked.

    Unlike `test_acp_adapter.py`'s bench, this one is meant to be driven several
    times: the Session map on disk is the thing under test, so it is the same
    file for every Task, and `restart()` is what an operator restarting the
    Worker looks like from here — a new Adapter and a new store object reading
    the same file.
    """

    def __init__(self, script=None, settings=None):
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)
        self.tasks = self.base / "tasks"
        self.results = self.base / "results"
        self.repo = self.base / "repo"
        self.reports = self.base / "reports"
        for d in (self.tasks, self.results, self.repo, self.reports):
            d.mkdir()
        self.script_path = self.base / "script.json"
        self.store_path = self.base / "sessions.json"
        self.settings = settings or SessionSettings()
        self.set_script(script or ANSWERS)
        self.restart()

    def set_script(self, script: dict) -> None:
        self.script_path.write_text(json.dumps(script))

    def restart(self) -> None:
        """What the operator restarting the Worker leaves behind: the file."""
        self.adapter = AcpAdapter(
            command=[sys.executable, FAKE, str(self.script_path)],
            store=SessionStore(self.store_path),
            session_settings=self.settings,
        )

    def handle(self, task_id: str, body: str = "do it", room: str = ROOM_A,
               tier: str = "owner", ops=None, cwd=None) -> str:
        lines = [f"id: {task_id}", f"channel_id: {room}", f"task: {body}",
                 f"access_tier: {tier}"]
        path = self.tasks / f"task-{task_id}.txt"
        path.write_text("\n".join(lines) + "\n")
        os.environ["FAKE_ACP_REPORT"] = str(self.reports / f"{task_id}.json")
        try:
            asyncio.run(asyncio.wait_for(
                handle_one(path, self.adapter, str(cwd or self.repo), self.results,
                           None, ops, LadderSettings(throttle=0.0)),
                timeout=30,
            ))
        finally:
            os.environ.pop("FAKE_ACP_REPORT", None)
        return (self.results / f"task-{task_id}.txt").read_text()

    def report(self, task_id: str):
        path = self.reports / f"{task_id}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def sessions(self) -> list:
        """The Session map as it is on disk, which is all a restart inherits."""
        if not self.store_path.exists():
            return []
        return json.loads(self.store_path.read_text())["sessions"]

    def opened(self, task_id: str) -> list:
        return [s["method"] for s in (self.report(task_id) or {}).get("sessions", [])]

    def session_id(self, task_id: str) -> str:
        sessions = (self.report(task_id) or {}).get("sessions") or [{}]
        return sessions[-1].get("sessionId") or ""


print("\n-- a follow-up in the same room continues the conversation --")

bench = Bench({"sessionPrefix": "alpha", **ANSWERS})
bench.handle("a1", "read worker.py")
bench.handle("a2", "now the same for reporter.py")

check(bench.opened("a1") == ["session/new"],
      "the first Task in a room opens one Session")
check("session/load" not in (bench.report("a1") or {})["methods"],
      "and does not try to resume anything, because there was nothing to resume")
check(bench.opened("a2") == ["session/load"],
      "the follow-up resumes rather than opening a second Session")
check(bench.session_id("a2") == bench.session_id("a1") == "alpha-1",
      "and it is the same Session, by the identifier the agent itself issued")
check("now the same for reporter.py" in
      bench.report("a2")["prompts"][0]["prompt"][0]["text"],
      "the follow-up's prompt reaches the resumed Session")
check(len(bench.sessions()) == 1 and bench.sessions()[0]["turns"] == 2,
      "the Session map holds one Session for the room, two Turns old")
check(bench.sessions()[0]["room"] == ROOM_A
      and bench.sessions()[0]["access_tier"] == "owner",
      "keyed by the room AND the Access Tier, never by the room alone")


print("\n-- two rooms, two conversations, neither seeing the other --")

bench.set_script({"sessionPrefix": "beta", **ANSWERS})
bench.handle("b1", "different room", room=ROOM_B)
check(bench.opened("b1") == ["session/new"],
      "a first Task in another room opens its own Session, resuming nothing")
check(bench.session_id("b1") == "beta-1",
      "and it is a different Session from the first room's")
check(sorted(s["room"] for s in bench.sessions()) == [ROOM_A, ROOM_B],
      "the map now holds one Session per room")

bench.set_script({"sessionPrefix": "alpha", **ANSWERS})
bench.handle("a3", "and again", room=ROOM_A)
check(bench.opened("a3") == ["session/load"] and bench.session_id("a3") == "alpha-1",
      "the first room's next Task resumes the FIRST room's Session — no crossing over")
check({s["room"]: s["session_id"] for s in bench.sessions()}
      == {ROOM_A: "alpha-1", ROOM_B: "beta-1"},
      "and the two conversations stay apart on disk")


print("\n-- a different Access Tier is a different Session --")

check(TurnContext(prompt="x", room=ROOM_A, access_tier="owner").session_key
      != TurnContext(prompt="x", room=ROOM_A, access_tier="other").session_key,
      "the Session key distinguishes two Tiers in one room")

store = SessionStore(Path(tempfile.mkdtemp()) / "s.json")
store.remember((ROOM_A, "owner"), "owner-session", "/repo", 1)
check(store.get((ROOM_A, "other")) is None,
      "a lower-trust request in that room finds no Session to inherit")
check(store.get((ROOM_A, "owner")).session_id == "owner-session",
      "while the owner's own conversation is exactly where it was")

before = bench.sessions()
out = bench.handle("t1", "read the owner's context", room=ROOM_A, tier="other")
check("only answer my owner" in out, "a non-owner Task in that room is refused")
check(bench.report("t1") is None,
      "the ACP Agent is never started for it, so no Session of any kind is opened")
check(bench.sessions() == before,
      "and the owner's Session is untouched — not resumed, not counted against")


print("\n-- the conversation survives the Worker restarting --")

bench.restart()
bench.handle("a4", "still there?", room=ROOM_A)
check(bench.opened("a4") == ["session/load"] and bench.session_id("a4") == "alpha-1",
      "a restarted Worker resumes the same Session, from the map on disk")
check([s for s in bench.sessions() if s["room"] == ROOM_A][0]["turns"] == 4,
      "and goes on counting Turns where it left off")


print("\n-- resuming produces NOTHING in the room --")

REPLAY = [
    {"sessionUpdate": "agent_message_chunk",
     "content": {"type": "text", "text": "OLD-TRANSCRIPT-LINE"}},
    {"sessionUpdate": "agent_thought_chunk",
     "content": {"type": "text", "text": "OLD-THOUGHT"}},
] + [
    {"sessionUpdate": "tool_call", "toolCallId": f"old{i}",
     "title": f"OLD-TOOL-{i}", "status": "completed", "kind": "read"}
    for i in range(6)
]

bench = Bench({"sessionPrefix": "replay", **ANSWERS})
bench.handle("r1", "start the conversation")
bench.set_script({"sessionPrefix": "replay", "loadSessionReplay": REPLAY, **ANSWERS})
relay = FakeRelay()
out = bench.handle("r2", "carry on", ops=relay.ops_client)
check(bench.opened("r2") == ["session/load"], "the Session was resumed (so there WAS a replay)")
check(len(relay.ops) == 2,
      "and the room received exactly two ops — the placeholder and the answer — "
      f"despite {len(REPLAY)} replayed updates (got {len(relay.ops)})")
check([o["op"] for o in relay.ops] == ["message", "edit"],
      "one message and one edit: no progress edit was driven by replayed history")
check(not relay.of("react"), "and no reaction, here as anywhere")
check("OLD-TRANSCRIPT-LINE" not in relay.bodies()
      and "OLD-TOOL-0" not in relay.bodies()
      and "OLD-THOUGHT" not in relay.bodies(),
      "not one word of the old transcript reached the room")
check("OLD-TRANSCRIPT-LINE" not in out and "OLD-TOOL-0" not in out,
      "nor the result — replayed history is not this Turn's answer either")
check("answered" in relay.ops[-1]["body"] and out.startswith(REPLIED),
      "while the live answer arrives exactly as it would have")
relay.stop()

# The control: the same events, live, are anything but silent. Without it the
# assertion above could be passing because nothing works.
bench.set_script({"sessionPrefix": "replay",
                  "defaultTurn": {"actions": [
                      {"type": "tool_call", "toolCallId": f"t{i}",
                       "title": f"LIVE-TOOL-{i}", "status": "completed"}
                      for i in range(6)] + [{"type": "message", "text": "answered"}],
                      "stopReason": "end_turn"}})
relay = FakeRelay()
bench.handle("r3", "do live work", ops=relay.ops_client)
check(len(relay.ops) > 2 and "LIVE-TOOL-" in relay.bodies(),
      "the same activity happening live DOES reach the room — suppression is "
      "the resumption's, not a Ladder that never worked")
relay.stop()


print("\n-- a conversation that cannot be resumed falls back, and says so --")

bench = Bench({"sessionPrefix": "refuse", **ANSWERS})
bench.handle("f1", "start")
bench.set_script({"sessionPrefix": "refuse",
                  "loadSessionError": {"code": -32000, "message": "session expired"},
                  **ANSWERS})
relay = FakeRelay()
out = bench.handle("f2", "carry on", ops=relay.ops_client)
check(bench.opened("f2") == ["session/load", "session/new"],
      "a refused resumption opens a fresh Session instead of failing the request")
check("answered" in out, "and the person gets their answer")
check(len(relay.of("message")) == 2, "the room is told, in a message of its own")
notice = relay.of("message")[1]["body"]
check("fresh conversation" in notice and "no longer have the earlier context" in notice,
      "which says the context was reset, in plain words")
check("session expired" in notice,
      "and carries what the agent said, so an operator can tell why")
check(PLACEHOLDER not in relay.ops[-1]["body"] and "answered" in relay.ops[-1]["body"],
      "the placeholder was still edited into the answer, not into the announcement")
check(bench.sessions()[0]["session_id"] == "refuse-1"
      and bench.sessions()[0]["turns"] == 1,
      "and the map now holds the fresh Session, one Turn old")
relay.stop()

bench = Bench({"sessionPrefix": "noload", **ANSWERS})
bench.handle("g1", "start")
bench.set_script({"sessionPrefix": "noload", "agentCapabilities": {"loadSession": False},
                  **ANSWERS})
relay = FakeRelay()
out = bench.handle("g2", "carry on", ops=relay.ops_client)
check("session/load" not in bench.report("g2")["methods"],
      "an agent that never advertised resumption is not asked to resume")
check(bench.opened("g2") == ["session/new"] and "answered" in out,
      "the Task is answered from a fresh Session all the same")
check("fresh conversation" in relay.of("message")[1]["body"],
      "and the room is told, by the same route")
relay.stop()


print("\n-- memory is bounded, and the boundary is announced --")

bench = Bench({"sessionPrefix": "budget", **ANSWERS}, settings=SessionSettings(turns=1))
bench.handle("h1", "one")
relay = FakeRelay()
bench.handle("h2", "two", ops=relay.ops_client)
check(bench.opened("h2") == ["session/new"],
      "a Session past its Turn budget is retired, not resumed")
check("session/load" not in bench.report("h2")["methods"],
      "and the retired one is not even asked — the budget is the Worker's to keep")
check(len(relay.of("message")) == 2, "retirement is announced in the room")
check("budget of 1 turns" in relay.of("message")[1]["body"],
      "naming the budget it reached, so it is predictable rather than mysterious")
relay.stop()

bench = Bench({"sessionPrefix": "idle", **ANSWERS}, settings=SessionSettings(idle=0.001))
bench.handle("i1", "one")
relay = FakeRelay()
bench.handle("i2", "two", ops=relay.ops_client)
check(bench.opened("i2") == ["session/new"], "an idle Session is retired too")
check("idle for" in relay.of("message")[1]["body"],
      "and the room is told it was idleness that ended it")
relay.stop()

bench = Bench({"sessionPrefix": "forever", **ANSWERS},
              settings=SessionSettings(turns=0, idle=0))
bench.handle("j1", "one")
bench.handle("j2", "two")
bench.handle("j3", "three")
check(bench.opened("j3") == ["session/load"] and bench.sessions()[0]["turns"] == 3,
      "an operator who asks for no limits gets none: 0 means unbounded, both ways")

bench = Bench({"sessionPrefix": "off", **ANSWERS}, settings=SessionSettings(memory=False))
relay = FakeRelay()
bench.handle("k1", "one", ops=relay.ops_client)
bench.handle("k2", "two")
check(bench.opened("k1") == bench.opened("k2") == ["session/new"],
      "with memory off every Task opens its own Session, as before Sessions existed")
check(not bench.store_path.exists(),
      "and nothing is written to disk — the setting is a real off switch")
check(len(relay.of("message")) == 1,
      "with nothing to announce: a Worker that never remembers is not forgetting")
relay.stop()


print("\n-- a Session's working directory is fixed when it opens --")

bench = Bench({"sessionPrefix": "cwd", **ANSWERS})
bench.handle("m1", "one")
elsewhere = bench.base / "other-repo"
elsewhere.mkdir()
relay = FakeRelay()
bench.handle("m2", "two", ops=relay.ops_client, cwd=elsewhere)
check(bench.opened("m2") == ["session/new"],
      "a Task from a differently-configured Worker gets a new Session, not a wrong one")
check(bench.report("m2")["sessions"][0]["cwd"] == str(elsewhere),
      "opened in the directory this Task actually runs in")
check("working directory changed" in relay.of("message")[1]["body"],
      "and the room is told why it started over")
relay.stop()


print("\n-- the Session map itself --")

path = Path(tempfile.mkdtemp()) / "nested" / "sessions.json"
store = SessionStore(path)
check(store.get((ROOM_A, "owner")) is None, "an absent map is an empty map, not an error")
store.remember((ROOM_A, "owner"), "s-1", "/repo", 1)
check(path.exists(), "and is created, parent directories and all, on first write")
again = SessionStore(path)
check(again.get((ROOM_A, "owner")) == SessionRecord(
    session_id="s-1", cwd="/repo", turns=1,
    updated_at=store.get((ROOM_A, "owner")).updated_at),
    "a fresh store reads back exactly what the last one wrote")
store.forget((ROOM_A, "owner"))
check(SessionStore(path).get((ROOM_A, "owner")) is None, "and forgetting is durable too")

path.write_text("{ this is not json")
broken = SessionStore(path)
check(broken.get((ROOM_A, "owner")) is None and broken.degraded,
      "a corrupt map is an empty map: a person's request is not failed over it")
broken.remember((ROOM_A, "owner"), "s-2", "/repo", 1)
check(SessionStore(path).get((ROOM_A, "owner")).session_id == "s-2",
      "and it repairs itself on the next write")

check(json.loads(SessionStore(path).path.read_text())["sessions"][0]["room"] == ROOM_A,
      "the file is plain JSON an operator can read")

settings = SessionSettings.from_env({"AGENT_CONNECT_SESSION_TURNS": "5",
                                     "AGENT_CONNECT_SESSION_IDLE": "60",
                                     "AGENT_CONNECT_SESSION_MEMORY": "0"})
check((settings.turns, settings.idle, settings.memory) == (5, 60.0, False),
      "the settings are read from the environment")
check(SessionSettings.from_env({}) == SessionSettings(),
      "and an empty environment is the documented defaults")
sloppy = SessionSettings.from_env({"AGENT_CONNECT_SESSION_TURNS": "lots",
                                   "AGENT_CONNECT_SESSION_IDLE": "-1"})
check((sloppy.turns, sloppy.idle) == (SessionSettings().turns, SessionSettings().idle),
      "a setting typed wrong falls back to the default rather than stopping the Worker")

old = SessionRecord(session_id="s", cwd="/repo", turns=3, updated_at=1000.0)
check(SessionSettings(turns=3).retirement(old, 1000.0),
      "a Session at its budget is retired")
check(not SessionSettings(turns=4).retirement(old, 1000.0),
      "one below it is not")
check("idle for" in SessionSettings(idle=60).retirement(old, 1100.0),
      "an idle Session is retired, in words that say why")
check(not SessionSettings(turns=0, idle=0).retirement(old, 10 ** 9),
      "and zero means no limit, however old the Session is")

check(store_path({"AGENT_CONNECT_SESSION_STORE": "/tmp/x.json"}) == Path("/tmp/x.json"),
      "the map's location can be named outright")
check(store_path({"AGENT_CONNECT_WORKSPACE": "/ws"}) == Path("/ws/sessions.json"),
      "and otherwise lives in the workspace the Worker already has")
check(workspace_dir({}) == Path.home() / ".agent-connect" / "workspace",
      "which defaults where it always did")


print("\n" + ("PASS — sessions green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
