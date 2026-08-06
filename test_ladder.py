"""Tests for the Ladder: what the room actually receives.

Everything here is asserted against a **fake relay** — a real local HTTP server
standing in for the relay's room endpoint and recording every Room Op it is
asked for — and against the Task's result file. Those two are what the outside
world can see: a person in a room sees the ops, and the delivery path sees the
result. Nothing asserts on which internal object called which.

The Adapters are stubs emitting the event vocabulary, so no ACP Agent, no
network and no credentials are involved, and this runs under bare `python3`.

Run: python3 test_ladder.py
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import AsyncIterator

from agent_connect import reporter as rep
from agent_connect.adapters.shim import ShimAdapter
from agent_connect.events import (
    COMPLETED,
    FAILED,
    Done,
    MessageChunk,
    Notice,
    PermissionAsked,
    Plan,
    Thinking,
    ToolFinished,
    ToolStarted,
    TurnContext,
)
from agent_connect.reporter import PLACEHOLDER, REPLIED, LadderSettings, TurnReporter
from agent_connect.roomops import RoomOps, room_ops_from_env
from agent_connect.worker import handle_one, process_one

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
    """The relay's `POST /v1/room`, as far as the Worker can tell.

    `ops` is the recording: one dict per op, in the order the room would have
    seen them. `fail_from` makes it start refusing after N ops, which is how
    the degrade-don't-lose-the-answer path is observed.
    """

    def __init__(self, fail_from=None, no_event_id=False):
        self.ops = []
        self.auth = []
        self.fail_from = fail_from
        self.no_event_id = no_event_id
        self._n = 0
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the test output readable
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                relay.auth.append(self.headers.get("Authorization"))
                if self.path != "/v1/room":
                    self.send_response(404)
                    self.end_headers()
                    return
                relay._n += 1
                if relay.fail_from is not None and relay._n > relay.fail_from:
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b'{"error":"nope"}')
                    return
                relay.ops.append(payload)
                body = {} if relay.no_event_id else {"event_id": f"$ev{relay._n}"}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def ops_of(self, kind):
        return [o for o in self.ops if o.get("op") == kind]

    def bodies(self):
        return [o.get("body", "") for o in self.ops]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def relay_ops(**kw):
    relay = FakeRelay(**kw)
    return relay, RoomOps(relay.url, "test-token")


def workspace():
    tmp = Path(tempfile.mkdtemp())
    tasks, results = tmp / "tasks", tmp / "results"
    tasks.mkdir()
    results.mkdir()
    return tasks, results


def write_task(tasks: Path, task_id: str, body: str, **headers) -> Path:
    lines = [f"id: {task_id}", f"task: {body}"]
    lines += [f"{k}: {v}" for k, v in headers.items() if k != "access_tier"]
    lines.append(f"access_tier: {headers.get('access_tier', 'owner')}")
    path = tasks / f"task-{task_id}.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


def ctx_for(room="!room:ag2.space", prompt="do it"):
    return TurnContext(prompt=prompt, task_id="task-1", room=room,
                       access_tier="owner", cwd="/repo")


class Scripted:
    """An Adapter that emits exactly the events a test wants to see handled."""

    def __init__(self, *events):
        self.events = events

    async def turn(self, ctx: TurnContext) -> AsyncIterator:
        for event in self.events:
            await asyncio.sleep(0)
            yield event


class Clock:
    """Time the test moves by hand, so throttling is asserted, not waited on."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def work(*, tools=3, answer="the answer"):
    """A plausible Turn: some thinking, some tool activity, some text."""
    events = [Thinking(text="hmm, maybe worker.py")]
    for i in range(tools):
        events.append(ToolStarted(tool_id=f"t{i}", title=f"Read file{i}.py", action="read"))
        events.append(ToolFinished(tool_id=f"t{i}", title=f"Read file{i}.py"))
    events.append(MessageChunk(text=answer))
    events.append(Done(reason=COMPLETED, text=answer))
    return Scripted(*events)


print("\n-- the placeholder, the edits, and exactly one reply --")

relay, ops = relay_ops()
clock = Clock()
reporter = TurnReporter(ops, LadderSettings(live=True, throttle=0.0), clock)
body = asyncio.run(reporter.run(work(), ctx_for()))

check(relay.ops and relay.ops[0]["op"] == "message",
      "the first Room Op is a message — the placeholder")
check(relay.ops[0]["body"] == PLACEHOLDER,
      f"and it carries the fleet-wide copy, {PLACEHOLDER!r}")
check(relay.ops[0]["room"] == "!room:ag2.space", "posted into the room that asked")
check(len(relay.ops_of("message")) == 1,
      "exactly one message is ever posted — everything after it is an edit")
check(not relay.ops_of("react"),
      "the Worker places NO reaction: the broker's intake reaction is the broker's")
check(all(o.get("event_id") == "$ev1" for o in relay.ops_of("edit")),
      "every edit targets that same message, so the room sees one reply")
check(relay.ops[-1]["op"] == "edit" and "the answer" in relay.ops[-1]["body"],
      "the last op edits that message into the answer")
check(PLACEHOLDER not in relay.ops[-1]["body"],
      "and the placeholder copy is gone from it")
check(body.startswith(REPLIED),
      f"the result body starts with {REPLIED} so the delivery path posts nothing")
check(all(a == "Bearer test-token" for a in relay.auth),
      "every Room Op carries the relay bearer token the Worker already holds")
relay.stop()


print("\n-- progress is tool activity, throttled, and never the model's thinking --")

relay, ops = relay_ops()
clock = Clock()
reporter = TurnReporter(ops, LadderSettings(live=True, throttle=10.0), clock)


async def timed_turn():
    adapter = Scripted(
        Thinking(text="I should probably rewrite everything"),
        ToolStarted(tool_id="a", title="Read worker.py", action="read"),
        ToolFinished(tool_id="a", title="Read worker.py"),
        ToolStarted(tool_id="b", title="Edit events.py", action="edit"),
        ToolFinished(tool_id="b", title="Edit events.py"),
        ToolStarted(tool_id="c", title="npm test", action="execute"),
        ToolFinished(tool_id="c", title="npm test", status=FAILED),
        Plan(entries=[{"title": "fix it", "status": "in_progress"}]),
        MessageChunk(text="done, "),
        MessageChunk(text="mostly."),
        Done(reason=COMPLETED, text="done, mostly."),
    )
    return await reporter.run(adapter, ctx_for())


body = asyncio.run(timed_turn())
edits = relay.ops_of("edit")
progress = [e["body"] for e in edits[:-1]]

check(len(progress) == 1,
      f"six tool events inside one throttle window produce one progress edit, not six "
      f"(got {len(progress)})")
check(reporter.progress_edits == 1, "and the reporter says so too")
check("Read worker.py" in progress[0],
      "progress names what the agent is doing, not what it is typing")
everything = "\n".join(relay.bodies()) + body
check("rewrite everything" not in everything,
      "the model's internal reasoning NEVER reaches the room")
check("done, mostly." not in progress[0],
      "progress is not the answer being typed out — that is what the final edit is for")
relay.stop()

# The same Turn with time actually passing between tools: more edits, still one
# message. Throttling is a rate, not a cap on caring.
relay, ops = relay_ops()
clock = Clock()
reporter = TurnReporter(ops, LadderSettings(live=True, throttle=5.0), clock)


class Slow:
    async def turn(self, ctx):
        for i in range(4):
            clock.advance(6.0)
            yield ToolStarted(tool_id=f"s{i}", title=f"Run step {i}", action="execute")
        yield Done(reason=COMPLETED, text="finished")


body = asyncio.run(TurnReporter(ops, LadderSettings(live=True, throttle=5.0), clock)
                   .run(Slow(), ctx_for()))
check(len(relay.ops_of("edit")) == 5,
      "four tool starts spread across the throttle window: four progress edits + the answer")
check(len(relay.ops_of("message")) == 1, "still exactly one message in the room")
relay.stop()


print("\n-- one setting reduces the Ladder to placeholder-then-answer --")

relay, ops = relay_ops()
body = asyncio.run(
    TurnReporter(ops, LadderSettings(live=False), Clock()).run(work(tools=5), ctx_for())
)
check(len(relay.ops_of("message")) == 1, "the placeholder is still posted")
check(len(relay.ops_of("edit")) == 1, "and edited exactly once — into the answer")
check("the answer" in relay.ops[-1]["body"], "which is the answer")
check(body.startswith(REPLIED), "the lease is still completed with the marker")
relay.stop()

check(LadderSettings.from_env({}).live, "live progress is on by default")
for off in ("0", "false", "no", "off", "OFF"):
    check(not LadderSettings.from_env({rep.LIVE_ENV: off}).live,
          f"AGENT_CONNECT_LIVE_PROGRESS={off!r} switches it off")
check(LadderSettings.from_env({rep.LIVE_ENV: "1"}).live,
      "AGENT_CONNECT_LIVE_PROGRESS=1 leaves it on")
check(LadderSettings.from_env({rep.THROTTLE_ENV: "9.5"}).throttle == 9.5,
      "the throttle is a setting")
check(LadderSettings.from_env({rep.CEILING_ENV: "120"}).ceiling == 120,
      "the edit ceiling is a setting")
check(LadderSettings.from_env({rep.THROTTLE_ENV: "banana"}).throttle == rep.DEFAULT_THROTTLE,
      "a setting typed wrong falls back to the default rather than killing the Worker")


print("\n-- the answer summarises what was done, including what was refused --")

relay, ops = relay_ops()
adapter = Scripted(
    ToolStarted(tool_id="a", title="Read worker.py", action="read"),
    ToolFinished(tool_id="a", title="Read worker.py"),
    ToolStarted(tool_id="b", title="npm test", action="execute"),
    ToolFinished(tool_id="b", title="npm test", status=FAILED),
    PermissionAsked(title="Write /etc/passwd", allowed=False,
                    reason="outside the session's working directory"),
    PermissionAsked(title="Read worker.py", allowed=True, reason="inside"),
    MessageChunk(text="I could not finish."),
    Done(reason=COMPLETED, text="I could not finish."),
)
body = asyncio.run(TurnReporter(ops, LadderSettings(throttle=0.0), Clock()).run(adapter, ctx_for()))
answer = relay.ops[-1]["body"]
check("I could not finish." in answer, "the answer is the answer")
check("Read worker.py" in answer and "npm test" in answer,
      "with a compact summary of what was read and run")
check("npm test (failed)" in answer, "and a tool call that failed says so")
check("Write /etc/passwd" in answer and "outside the session's working directory" in answer,
      "AND the operations the Permission Policy rejected — a blocked agent is not a lazy one")
check(answer.count("Write /etc/passwd") == 1,
      "said once: the Adapter's note no longer repeats what the summary carries")
check("refused" in answer.lower(), "in words that say it was refused")
check("Read worker.py — inside" not in answer,
      "an allowed request is not noise in the summary")
check(len(answer.splitlines()) <= 6, "the summary is compact — a reply, not a log")
relay.stop()


print("\n-- an announcement is its own message, never the placeholder --")

ANNOUNCE = "🧠 agent-connect: starting a fresh conversation."
relay, ops = relay_ops()
adapter = Scripted(
    Notice(text=ANNOUNCE),
    ToolStarted(tool_id="a", title="Read worker.py", action="read"),
    MessageChunk(text="here it is"),
    Done(reason=COMPLETED, text="here it is"),
)
body = asyncio.run(TurnReporter(ops, LadderSettings(throttle=0.0), Clock()).run(adapter, ctx_for()))
messages = relay.ops_of("message")
check(len(messages) == 2, "the announcement is posted, so the room is two messages")
check(messages[0]["body"] == PLACEHOLDER and messages[1]["body"] == ANNOUNCE,
      "the placeholder first, then the announcement as its own message")
check(messages[1]["room"] == "!room:ag2.space", "into the room it is about")
check(all(ANNOUNCE not in o["body"] for o in relay.ops_of("edit")),
      "and NOT by editing the placeholder — that message belongs to the answer")
check(relay.ops[-1]["op"] == "edit" and "here it is" in relay.ops[-1]["body"],
      "so the answer still lands where it was going")
check(ANNOUNCE not in relay.ops[-1]["body"],
      "and is not made to carry the announcement a second time")
check(body.startswith(REPLIED), "the reply is still complete: one answer, delivered")
relay.stop()

# No room to announce in — the announcement must not simply vanish.
body = asyncio.run(TurnReporter(None, LadderSettings()).run(
    Scripted(Notice(text=ANNOUNCE), MessageChunk(text="here it is"),
             Done(reason=COMPLETED, text="here it is")),
    ctx_for()))
check(ANNOUNCE in body and "here it is" in body,
      "with no relay at all the announcement rides out on the result, ahead of the answer")
check(body.index(ANNOUNCE) < body.index("here it is"),
      "ahead of it, because it is context for reading the answer")

# A relay that refuses the announcement is the same case, arrived at later.
relay, ops = relay_ops(fail_from=1)
body = asyncio.run(TurnReporter(ops, LadderSettings()).run(
    Scripted(Notice(text=ANNOUNCE), MessageChunk(text="here it is"),
             Done(reason=COMPLETED, text="here it is")),
    ctx_for()))
check(ANNOUNCE in body, "a relay that refuses the announcement does not lose it either")
check(not body.startswith(REPLIED), "and the answer is not marked delivered when it was not")
relay.stop()

# An empty notice is not a blank message in someone's room.
relay, ops = relay_ops()
asyncio.run(TurnReporter(ops, LadderSettings()).run(
    Scripted(Notice(text="   "), MessageChunk(text="hi"), Done(reason=COMPLETED, text="hi")),
    ctx_for()))
check(len(relay.ops_of("message")) == 1, "an empty notice posts nothing")
relay.stop()


print("\n-- an answer too long for an edit still arrives in full --")

long_answer = "x" * 500
relay, ops = relay_ops()
adapter = Scripted(MessageChunk(text=long_answer), Done(reason=COMPLETED, text=long_answer))
body = asyncio.run(
    TurnReporter(ops, LadderSettings(ceiling=100), Clock()).run(adapter, ctx_for())
)
check(len(relay.ops_of("message")) == 1, "the placeholder was still posted")
check(long_answer not in relay.ops[-1]["body"],
      "the long answer does NOT travel as an edit")
check(relay.ops[-1]["op"] == "edit" and len(relay.ops[-1]["body"]) < 200,
      "the placeholder is edited into a short pointer instead")
check(not body.startswith(REPLIED),
      "the result is not marked replied — the delivery path is what posts the answer")
check(long_answer in body, "and it carries the answer in full, untruncated")
relay.stop()

# The ceiling is about the whole body, summary included.
relay, ops = relay_ops()
body = asyncio.run(
    TurnReporter(ops, LadderSettings(ceiling=100_000), Clock()).run(
        Scripted(MessageChunk(text=long_answer), Done(reason=COMPLETED, text=long_answer)),
        ctx_for(),
    )
)
check(long_answer in relay.ops[-1]["body"], "under the ceiling it travels as an edit again")
check(body.startswith(REPLIED), "and the lease is completed with the marker")
relay.stop()


print("\n-- shimmed Adapters get a placeholder and an answer, and nothing between --")


class SyncStub:
    def run(self, task, sandbox, cwd):
        return "codex says hello"


relay, ops = relay_ops()
body = asyncio.run(
    TurnReporter(ops, LadderSettings(live=True, throttle=0.0), Clock()).run(
        ShimAdapter("codex", SyncStub()), ctx_for()
    )
)
check(len(relay.ops_of("message")) == 1, "a shimmed Adapter gets the placeholder too")
check(len(relay.ops_of("edit")) == 1, "and exactly one edit: no invented progress")
check("codex says hello" in relay.ops[-1]["body"], "which is its answer")
check(body.startswith(REPLIED), "and it completes the lease the same way")
relay.stop()


print("\n-- the Ladder degrades; it never eats the answer --")

# No relay at all: the behaviour every existing test relies on.
body = asyncio.run(TurnReporter(None, LadderSettings(), Clock()).run(work(), ctx_for()))
check(not body.startswith(REPLIED) and "the answer" in body,
      "with no relay the answer travels as the result body, as it always did")

# A Task with no room identifier cannot be edited into: same fallback.
relay, ops = relay_ops()
body = asyncio.run(TurnReporter(ops, LadderSettings(), Clock()).run(work(), ctx_for(room="")))
check(not relay.ops, "a Task carrying no room gets no Room Op at all")
check("the answer" in body and not body.startswith(REPLIED),
      "and its answer still reaches the result")
relay.stop()

# The relay refuses the placeholder: no event to edit, answer via the result.
relay, ops = relay_ops(fail_from=0)
body = asyncio.run(TurnReporter(ops, LadderSettings(), Clock()).run(work(), ctx_for()))
check("the answer" in body and not body.startswith(REPLIED),
      "a relay that refuses the placeholder costs the Ladder, not the answer")
check(not ops.available, "and the Room Ops switch themselves off rather than retry per Task")
relay.stop()

# The relay accepts the placeholder and then dies before the final edit.
relay, ops = relay_ops(fail_from=1)
body = asyncio.run(
    TurnReporter(ops, LadderSettings(live=False), Clock()).run(work(), ctx_for())
)
check("the answer" in body and not body.startswith(REPLIED),
      "a relay that dies before the final edit still lets the answer through the result")
relay.stop()

# A relay that posts but will not say what it posted: nothing to edit.
relay, ops = relay_ops(no_event_id=True)
body = asyncio.run(TurnReporter(ops, LadderSettings(), Clock()).run(work(), ctx_for()))
check("the answer" in body and not body.startswith(REPLIED),
      "a post whose event identifier never came back falls back rather than editing blind")
relay.stop()


print("\n-- through the Worker's seam, end to end --")

relay, ops = relay_ops()
tasks, results = workspace()
path = write_task(tasks, "L1", "summarise worker.py",
                  channel_id="!room:ag2.space", sender_name="Nikita", access_tier="owner")
asyncio.run(
    handle_one(path, work(), "/repo", results, None, ops, LadderSettings(throttle=0.0))
)
result = (results / "task-L1.txt").read_text()
check(len(relay.ops_of("message")) == 1,
      "one Task through the Worker posts one message into the room")
check(relay.ops[0]["body"] == PLACEHOLDER, "the placeholder, by its fleet-wide copy")
check(relay.ops[-1]["op"] == "edit" and "the answer" in relay.ops[-1]["body"],
      "edited, finally, into the answer")
check(result.startswith(REPLIED), "and the result completes the lease")
check("the answer" in result, "while still archiving what was said")
relay.stop()

# The Worker's default — no ops passed — is byte-for-byte what it was before.
tasks, results = workspace()
path = write_task(tasks, "L2", "summarise worker.py", channel_id="!r:ag2.space")
asyncio.run(process_one(path, Scripted(MessageChunk(text="plain"),
                                       Done(reason=COMPLETED, text="plain")),
                        "/repo", results))
check((results / "task-L2.txt").read_text() == "plain\n",
      "a Worker given no relay writes exactly the answer, as before the Ladder")


print("\n-- reading the relay out of the environment --")

check(room_ops_from_env({}) is None, "no token, no Ladder")
check(room_ops_from_env({"AGENT_CONNECT_TOKEN": "  "}) is None, "a blank token is no token")
made = room_ops_from_env({"AGENT_CONNECT_TOKEN": "secret"})
check(made is not None and made.token == "secret", "the Portal token is the relay credential")
check(made.url == "https://chat.ag2.space/relay",
      "and the default gateway is the one the installer wires the relay client to")
combined = room_ops_from_env({"REMOTE_TASK_TOKEN": "https://relay.example|s3cret"})
check(combined.url == "https://relay.example" and combined.token == "s3cret",
      "the combined onboarding token carries its own gateway")
explicit = room_ops_from_env(
    {"REMOTE_TASK_TOKEN": "https://relay.example|s3cret",
     "REMOTE_TASK_URL": "https://other.example/relay/"}
)
check(explicit.url == "https://other.example/relay",
      "an explicit REMOTE_TASK_URL wins, trailing slash and all")

print("\n" + ("PASS — the Ladder green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
