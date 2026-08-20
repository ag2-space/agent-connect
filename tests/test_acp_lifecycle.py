"""Tests for a Turn that does not end by itself: deadlines and dead bridges.

The other half of the Turn lifecycle — queueing, the endings' wording, the
structured rejection — is in `test_turn_lifecycle.py`, which needs no ACP Agent.
What needs one is everything here: cancellation is a *protocol* call, and the
only way to assert it happened is to ask the agent that received it.

Two observables, as everywhere in this feature:

* **What the agent saw** — its own report. `session/cancel` in the methods it
  received is the difference between cancelling a Turn and killing a process,
  and `permissions[].answer` is how the Client's answer to an outstanding
  permission request is read from outside the Worker.
* **What the room received and what was written as the result** — a partial
  answer with the interruption stated, or a `[no-send]` rejection with nothing
  posted at all.

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: .venv/bin/python tests/test_acp_lifecycle.py
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
    from agent_connect.adapters import acp as acp_adapter
    from agent_connect.adapters.acp import DEFAULT_TIMEOUT, TIMEOUT_ENV, AcpAdapter
    from agent_connect.reporter import NO_SEND, PLACEHOLDER, REPLIED, LadderSettings
    from agent_connect.sessions import SessionSettings, SessionStore
    from _queue import room_ops_at
    from _queue import task as queued_task
    from agent_connect.worker import handle_one, process_one
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_lifecycle.py: {exc}\n"
        "This test has a dependency (see docs/adr/0001). Run it from an\n"
        "environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python tests/test_acp_lifecycle.py"
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
        return room_ops_at(f"http://127.0.0.1:{self.server.server_port}")

    def of(self, kind):
        return [o for o in self.ops if o.get("op") == kind]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Bench:
    """One Worker workspace, one scripted fake ACP Agent, one Session map.

    Each Turn is its own ACP Agent process — that is the Adapter's design — so
    each Turn writes its own report, named after the Task. The report path goes
    on the command line rather than into the environment, because two Turns
    running at once cannot share one environment variable.
    """

    def __init__(self, script: dict, timeout=None, settings=None):
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)
        self.results = self.base / "results"
        self.repo = self.base / "repo"
        self.reports = self.base / "reports"
        for d in (self.results, self.repo, self.reports):
            d.mkdir()
        self.script_path = self.base / "script.json"
        self.set_script(script)
        # One store for the bench's whole life — the Session map is what makes
        # a conversation continue across Turns, and across a crash.
        self.store = SessionStore(self.base / "sessions.json")
        self.timeout = timeout
        self.settings = settings or SessionSettings()
        self.adapter = self.adapter_for("unused")

    def set_script(self, script: dict) -> None:
        self.script_path.write_text(json.dumps(script))

    def adapter_for(self, task_id: str) -> AcpAdapter:
        return AcpAdapter(
            command=[sys.executable, FAKE, str(self.script_path),
                     str(self.reports / f"{task_id}.json")],
            store=self.store,
            session_settings=self.settings,
            timeout=self.timeout,
        )

    def write(self, task_id: str, body: str = "do it", room: str = ROOM_A):
        return queued_task(f"task-{task_id}", body, room=room)

    def handle(self, task_id: str, body: str = "do it", room: str = ROOM_A,
               ops=None, timeout=60) -> str:
        task = self.write(task_id, body, room)
        # The report is per Task, so the Adapter's command is too — and an
        # Adapter is cheap: what has to survive between Tasks is the store.
        self.adapter = self.adapter_for(task_id)
        return asyncio.run(asyncio.wait_for(
            handle_one(task, self.adapter, str(self.repo),
                       None, ops, LadderSettings(throttle=0.0)),
            timeout=timeout,
        ))

    def report(self, task_id: str):
        path = self.reports / f"{task_id}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def sessions(self) -> list:
        path = self.base / "sessions.json"
        return json.loads(path.read_text())["sessions"] if path.exists() else []


SLEEPS = {"defaultTurn": {"actions": [{"type": "message", "text": "half an answer"},
                                      {"type": "sleep", "seconds": 30}],
                          "stopReason": "end_turn"}}

print("\n-- a Turn past its deadline is cancelled THROUGH the protocol --")

bench = Bench(SLEEPS, timeout=0.5)
relay = FakeRelay()
out = bench.handle("t1", "take your time", ops=relay.ops_client)
report = bench.report("t1")

check("session/cancel" in (report or {}).get("methods", []),
      "the agent received session/cancel — the Turn was ended by asking, not by killing")
check(report["cancelled"] == [report["sessions"][0]["sessionId"]],
      "and the cancellation named the Session that was actually running")
check(len(report["prompts"]) == 1,
      "the prompt was sent and returned: a cancelled Turn ends the protocol's way")
check("half an answer" in out,
      "what the agent produced before the deadline is NOT thrown away")
check("ran past its" in out and "deadline" in out,
      "and the interruption is stated plainly, so a partial answer is not read as whole")
check(out.startswith(REPLIED), "it is a reply like any other: one message, delivered")
check("half an answer" in relay.ops[-1]["body"]
      and "deadline" in relay.ops[-1]["body"],
      "the room's one message carries both the partial answer and the interruption")
check(len(relay.of("message")) == 1, "still exactly one message posted")
relay.stop()


print("\n-- a deadline that produced nothing is a structured rejection --")

bench = Bench({"defaultTurn": {"actions": [{"type": "sleep", "seconds": 30}],
                               "stopReason": "end_turn"}}, timeout=0.5)
relay = FakeRelay()
out = bench.handle("t2", "think about it forever", ops=relay.ops_client)
check(out.startswith(NO_SEND),
      f"a timeout with no output writes {NO_SEND}: the broker posts the failure notice")
check("timeout" in out, "the reason is in the archived result")
check(not relay.of("edit"),
      "and the Worker edits nothing into the room — no competing apology")
check(relay.of("message")[-1]["body"] == PLACEHOLDER,
      "the placeholder is left as it stood")
check("session/cancel" in bench.report("t2")["methods"],
      "the Turn was still ended through the protocol, empty or not")
relay.stop()


print("\n-- other Sessions are untouched by one Session's deadline --")

# Two rooms, two ACP Agent processes (this Adapter opens one per Turn), run
# concurrently through the Worker's own per-Session serialisation. The room
# that times out must not take the other one with it — which is the whole
# reason cancellation is a protocol call rather than a kill.
slow = Bench(SLEEPS, timeout=0.5)
fast = Bench({"defaultTurn": {"actions": [{"type": "message", "text": "answered"}],
                              "stopReason": "end_turn"}})


async def _two_rooms():
    a = slow.write("s1", "the slow one", ROOM_A)
    b = fast.write("f1", "the quick one", ROOM_B)
    sessions = {}
    return await asyncio.gather(
        process_one(a, slow.adapter_for("s1"), str(slow.repo), sessions),
        process_one(b, fast.adapter_for("f1"), str(fast.repo), sessions),
    )


timed_out, survivor = asyncio.run(asyncio.wait_for(_two_rooms(), timeout=60))
check("half an answer" in timed_out and "deadline" in timed_out,
      "the room that ran long gets its partial answer and its interruption")
check("answered" in survivor and not survivor.startswith(NO_SEND),
      "the other room's Turn finished normally, in the same Worker, at the same time")
check(fast.report("f1")["cancelled"] == [],
      "and its own agent was never asked to cancel anything")


print("\n-- cancellation answers an outstanding permission request as cancelled --")

# The agent is scripted to ignore the cancellation and carry on asking, which is
# the only way to have a permission request outstanding *after* a Turn has been
# cancelled. The path it names is inside the working directory, so the Policy
# would have allowed it: "cancelled" is therefore the cancellation speaking and
# not the Policy.
bench = Bench({
    "ignoreCancel": True,
    "defaultTurn": {"actions": [
        {"type": "message", "text": "starting"},
        {"type": "sleep", "seconds": 1.0},
        {"type": "permission",
         "toolCall": {"toolCallId": "t1", "title": "Write notes.txt",
                      "rawInput": {"abs_path": "notes.txt"}}},
        {"type": "message", "text": "carried on"}],
        "stopReason": "end_turn"}},
    timeout=0.3,
)
out = bench.handle("p1", "write me a note")
answered = bench.report("p1")["permissions"][0]["answer"]
check(answered == {"outcome": {"outcome": "cancelled"}},
      f"the outstanding permission request is answered cancelled (got {answered})")
check("session/cancel" in bench.report("p1")["methods"],
      "which is the protocol's requirement of a Client that has cancelled a Turn")


print("\n-- an agent that will not stop is ended, and only its own Turn is --")

grace = acp_adapter.CANCEL_GRACE
acp_adapter.CANCEL_GRACE = 0.5  # the allowance, in test time rather than real
try:
    bench = Bench({"ignoreCancel": True,
                   "defaultTurn": {"actions": [
                       {"type": "message", "text": "half an answer"},
                       {"type": "sleep", "seconds": 30}],
                       "stopReason": "end_turn"}}, timeout=0.5)
    out = bench.handle("u1", "never stop", timeout=30)
    check("half an answer" in out,
          "an agent that ignored the cancellation still keeps what it had said")
    check("did not answer the cancellation" in out,
          "and the room is told that ending it took more than asking")
    check("session/cancel" in bench.report("u1")["methods"],
          "asking came first, always: the process is the last resort, never the first")
    # The Worker is still standing: the next Task is answered normally.
    bench.set_script({"defaultTurn": {"actions": [{"type": "message", "text": "still here"}],
                                      "stopReason": "end_turn"}})
    check("still here" in bench.handle("u2", "are you there?"),
          "and the Worker serves the next Task as if nothing had happened")
finally:
    acp_adapter.CANCEL_GRACE = grace


print("\n-- the Local Agent dying is survived, and the conversation is kept --")

bench = Bench({"defaultTurn": {"actions": [{"type": "exit", "code": 3}]}})
relay = FakeRelay()
out = bench.handle("d1", "do something fatal", ops=relay.ops_client)
check(out.startswith(NO_SEND),
      "a bridge that died with nothing to show is a structured rejection")
check("code 3" in out, "with the exit code kept, because an operator needs it")
check("resume it" in out,
      "and the conversation is marked for resumption rather than written off")
check(not relay.of("edit"), "the room gets no failure message from the Worker")
relay.stop()

check(bench.sessions() and bench.sessions()[0]["room"] == ROOM_A,
      "the Session identifier survived the crash, in the map on disk")
dead_session = bench.sessions()[0]["session_id"]

bench.set_script({"defaultTurn": {"actions": [{"type": "message", "text": "recovered"}],
                                  "stopReason": "end_turn"}})
out = bench.handle("d2", "still there?")
check("recovered" in out, "the Worker is alive and the next Task is answered")
check([s["method"] for s in bench.report("d2")["sessions"]] == ["session/load"],
      "and the next Turn RESUMES the conversation the crash interrupted")
check(bench.report("d2")["sessions"][0]["sessionId"] == dead_session,
      "the same Session, by the identifier the agent itself issued")


print("\n-- the deadline is a setting --")

adapter = AcpAdapter(command=["x"])
os.environ.pop(TIMEOUT_ENV, None)
check(adapter.timeout() == DEFAULT_TIMEOUT,
      f"unset, a Turn may run for the documented default ({DEFAULT_TIMEOUT:.0f}s)")
os.environ[TIMEOUT_ENV] = "30"
check(AcpAdapter(command=["x"]).timeout() == 30.0, "the operator can shorten it")
os.environ[TIMEOUT_ENV] = "0"
check(AcpAdapter(command=["x"]).timeout() == 0.0,
      "and 0 means no deadline, for someone who would rather wait than lose the work")
os.environ[TIMEOUT_ENV] = "whenever"
check(AcpAdapter(command=["x"]).timeout() == DEFAULT_TIMEOUT,
      "a setting typed wrong is the default, not a Worker that will not start")
os.environ.pop(TIMEOUT_ENV, None)
check(AcpAdapter(command=["x"], timeout=1.5).timeout() == 1.5,
      "and an explicit value wins over the environment, which is how tests set it")

bench = Bench(SLEEPS, timeout=0)
check(bench.adapter.timeout() == 0, "a Worker with no deadline is a supported Worker")


print("\n" + ("PASS — acp turn lifecycle green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
