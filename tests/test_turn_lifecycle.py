"""Tests for what happens when a Turn does not simply produce an answer.

Three situations, all asserted the way the outside world sees them — the Room
Ops a **real local HTTP relay** recorded, and the body the Task was completed
with. Nothing here asserts on which internal object called which.

* **A message arriving while the Session is busy** is queued, and the person is
  told so before the wait rather than after it.
* **A Turn that stopped short** carries an explicit line saying so, whatever it
  managed to produce.
* **A Turn that produced nothing** is a structured rejection: `[no-send]`, no
  edit, and no failure message from the Worker — the broker owns that one.

The Adapters are stubs emitting the event vocabulary, so this runs under bare
`python3`. Cancellation through the ACP protocol — the other half of this
ticket — is in `test_acp_lifecycle.py`, which needs a real ACP Agent to cancel.

Run: python3 tests/test_turn_lifecycle.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent_connect.events import (
    CANCELLED,
    COMPLETED,
    FAILED,
    REFUSED,
    TIMEOUT,
    TOKEN_LIMIT,
    Done,
    MessageChunk,
    Notice,
    ToolStarted,
    TurnContext,
)
from agent_connect.pending import PendingTurn, SessionQueue, queue_for
from agent_connect.reporter import (
    NO_SEND,
    PLACEHOLDER,
    QUEUED,
    REPLIED,
    STOP_LINES,
    LadderSettings,
    TurnReporter,
)
from _queue import FakeClient, task
from agent_connect.roomops import RoomOps
from agent_connect.worker import handle_one, process_one

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

    def of(self, kind, room=None):
        return [o for o in self.ops
                if o.get("op") == kind and (room is None or o.get("room_id") == room)]

    def bodies(self):
        return " ".join(o.get("body", "") for o in self.ops)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def relay_ops():
    relay = FakeRelay()
    return relay, RoomOps(f"http://127.0.0.1:{relay.server.server_port}", "test-token")


def workspace():
    """The outgoing directory, which is all a workspace is to a Turn now."""
    tmp = Path(tempfile.mkdtemp())
    results = tmp / "results"
    results.mkdir()
    return results


def ctx_for(room=ROOM_A, task_id="task-1"):
    return TurnContext(prompt="do it", task_id=task_id, room=room,
                       access_tier="owner", cwd="/repo")


class Scripted:
    """An Adapter that emits exactly the events a test wants handled."""

    def __init__(self, *events):
        self.events = events

    async def turn(self, ctx):
        for event in self.events:
            await asyncio.sleep(0)
            yield event


class Gated:
    """An Adapter whose Turn stays open until the test opens the gate.

    `started` is the order Turns actually reached the Adapter in, and `peak` is
    how many were open at once — which is how "one Turn at a time per Session,
    but rooms in parallel" is observed without timing anything.
    """

    def __init__(self):
        self.gate = None
        self.started = []
        self.live = 0
        self.peak = 0

    async def turn(self, ctx):
        self.started.append(ctx.task_id)
        self.live += 1
        self.peak = max(self.peak, self.live)
        if self.gate is not None:
            await self.gate.wait()
        self.live -= 1
        answer = f"answer for {ctx.task_id}"
        yield MessageChunk(text=answer)
        yield Done(reason=COMPLETED, text=answer)


async def settle(seconds=0.3):
    """Let everything that can start, start.

    Real seconds rather than a spin of `sleep(0)`: a Room Op is a real HTTP
    request to a real local server, made off-thread, so "everything that can
    happen has happened" is not reachable by yielding to the event loop alone.
    """
    await asyncio.sleep(seconds)


print("\n-- a second message is queued, announced, and answered afterwards --")


async def _queueing():
    relay, ops = relay_ops()
    results = workspace()
    client = FakeClient()
    delivered = (task("task-q1", "the long one", room=ROOM_A),
                 task("task-q2", "the follow-up", room=ROOM_A),
                 task("task-q3", "another room", room=ROOM_B))
    adapter = Gated()
    adapter.gate = asyncio.Event()
    sessions = {}
    running = [
        asyncio.ensure_future(
            handle_one(one, adapter, "/repo", results, sessions, ops,
                       LadderSettings(throttle=0.0), client=client)
        )
        for one in delivered
    ]
    await settle()
    mid = {
        "started": list(adapter.started),
        "peak": adapter.peak,
        "ops": list(relay.ops),
    }
    adapter.gate.set()
    await asyncio.gather(*running)
    return relay, client, adapter, mid


relay, client, adapter, mid = asyncio.run(_queueing())

check("task-q2" not in mid["started"],
      "while the first Turn is open, the second Task has not reached the Adapter")
check("task-q1" in mid["started"] and "task-q3" in mid["started"],
      "the first Task and the other room's Task both started")
check(mid["peak"] == 2,
      f"two rooms ran at once and the queue is per Session (peak {mid['peak']})")

announcements = [o for o in mid["ops"]
                 if o.get("op") == "message" and "queued" in o.get("body", "")]
check(len(announcements) == 1,
      f"the queued Task is announced, once (got {len(announcements)})")
check(announcements and announcements[0]["room_id"] == ROOM_A,
      "in the room that is waiting, and only there")
check(all("queued" not in o.get("body", "") for o in relay.of("edit")),
      "as its own message — never by editing someone else's placeholder")
check(len(relay.of("message", ROOM_A)) == 3,
      "room A sees three messages: two placeholders and the one announcement")
check(len(relay.of("message", ROOM_B)) == 1,
      "and the other room sees only its own placeholder — nothing about the queue")

check(adapter.started[-1] == "task-q2"
      and adapter.started.index("task-q1") < adapter.started.index("task-q2"),
      f"the queued Task ran after the one it waited for ({adapter.started})")
check(adapter.peak <= 2, "and never two Turns at once on the one Session")
for tid in ("task-q1", "task-q2", "task-q3"):
    check(f"answer for {tid}" in (client.answer(tid) or ""), f"{tid} was answered")
check("queued" not in client.answer("task-q2"),
      "and the queue announcement is not repeated in the answer it preceded")
relay.stop()


print("\n-- the announcement comes before the placeholder, not after --")


async def _order():
    relay, ops = relay_ops()
    reporter = TurnReporter(ops, LadderSettings(throttle=0.0))
    ctx = ctx_for()
    await reporter.queued(ctx, ahead=1)
    body = await reporter.run(
        Scripted(MessageChunk(text="here it is"),
                 Done(reason=COMPLETED, text="here it is")),
        ctx,
    )
    return relay, body


relay, body = asyncio.run(_order())
check(relay.ops[0]["op"] == "message" and "queued" in relay.ops[0]["body"],
      "the room hears it is queued first")
check(relay.ops[1]["op"] == "message" and relay.ops[1]["body"] == PLACEHOLDER,
      "then the placeholder for its own answer")
check(relay.ops[-1]["op"] == "edit" and "here it is" in relay.ops[-1]["body"],
      "then the answer, edited into that placeholder as usual")
check(body.startswith(REPLIED), "and the reply completes the lease as usual")
relay.stop()

# A wait nobody could be told about is not carried out to the answer instead:
# "your message is queued", read next to the answer, is noise.


async def _no_relay():
    reporter = TurnReporter(None, LadderSettings())
    ctx = ctx_for()
    await reporter.queued(ctx, ahead=1)
    return await reporter.run(
        Scripted(MessageChunk(text="hi"), Done(reason=COMPLETED, text="hi")), ctx)


body = asyncio.run(_no_relay())
check("queued" not in body and "hi" in body,
      "with no relay the queue announcement is dropped rather than stapled to the answer")


print("\n-- a Turn that stopped short always says so --")


def run_turn_events(*events, ops=None, settings=None):
    reporter = TurnReporter(ops, settings or LadderSettings(throttle=0.0))
    return asyncio.run(reporter.run(Scripted(*events), ctx_for())), reporter


relay, ops = relay_ops()
body, _ = run_turn_events(
    MessageChunk(text="half an answer"),
    Done(reason=TIMEOUT, text="half an answer",
         note="⏱ agent-connect: this turn ran past its 5-second deadline."),
    ops=ops,
)
check("half an answer" in body, "a timeout keeps what the agent had produced")
check("ran past its 5-second deadline" in body,
      "and states the interruption alongside it")
check("half an answer" in relay.ops[-1]["body"]
      and "ran past its 5-second deadline" in relay.ops[-1]["body"],
      "the room gets both, in the one message it was already reading")
check(body.startswith(REPLIED),
      "it is a real reply: delivered, and the lease completed")
relay.stop()

body, _ = run_turn_events(
    MessageChunk(text="half an answer"),
    Done(reason=TOKEN_LIMIT, text="half an answer"),
)
check(STOP_LINES[TOKEN_LIMIT] in body,
      "a stop reason with no words from the Adapter still produces an explicit line")

body, _ = run_turn_events(
    MessageChunk(text="mid-sentence"), Done(reason=CANCELLED, text="mid-sentence"))
check(STOP_LINES[CANCELLED] in body, "a cancelled Turn says it was cancelled")

body, _ = run_turn_events(
    MessageChunk(text="all of it"), Done(reason=COMPLETED, text="all of it"))
check(body.strip().endswith("all of it"),
      "a Turn that simply finished gains no ending line at all")
check(not any(line in body for line in STOP_LINES.values()),
      "— none of them, so the line means something when it appears")

body, _ = run_turn_events(
    MessageChunk(text="partial"),
    Done(reason="something-upstream-invented", text="partial"),
)
check(STOP_LINES[FAILED] in body,
      "and a reason nobody here has heard of is still not silence")


print("\n-- a Turn that produced nothing is a structured rejection --")


async def _rejection(done):
    relay, ops = relay_ops()
    # Live progress off, so that "nothing was edited" is about the ending
    # rather than about a progress edit that legitimately happened earlier.
    reporter = TurnReporter(ops, LadderSettings(live=False))
    body = await reporter.run(Scripted(ToolStarted(tool_id="a", title="Read x.py"), done),
                              ctx_for())
    return relay, body, reporter


relay, body, reporter = asyncio.run(_rejection(Done(reason=REFUSED, text="")))
check(body.startswith(NO_SEND),
      f"the result body starts with {NO_SEND}, which the relay client archives "
      "and delivers nowhere")
check(reporter.rejected, "and the reporter says the Turn was rejected")
check("refused" in body, "the reason is in the body, for whoever reads the archive")
check(not relay.of("edit"),
      "nothing was edited into the room: no Worker-authored failure message")
check(relay.of("message") and relay.of("message")[-1]["body"] == PLACEHOLDER,
      "the placeholder is left exactly as it was — the broker's notice is the reply")
check(REPLIED not in body,
      "and the lease is NOT completed as replied, because nothing was replied")
relay.stop()

relay, body, _ = asyncio.run(_rejection(
    Done(reason=TIMEOUT, text="",
         note="⏱ agent-connect: it had produced nothing by then.")))
check(body.startswith(NO_SEND) and "produced nothing" in body,
      "an empty timeout is the same rejection, carrying what the Adapter said")
check(not relay.of("edit"), "and still edits nothing into the room")
relay.stop()

relay, body, _ = asyncio.run(_rejection(
    Done(reason=FAILED, text="", note="agent-connect: the ACP Agent exited (code 3)")))
check(body.startswith(NO_SEND) and "code 3" in body,
      "a bridge that died is a rejection too, with the exit code kept for the operator")
check("Read x.py" in body,
      "and what the Turn did manage to do is archived with it")
relay.stop()

body = asyncio.run(
    TurnReporter(None, LadderSettings()).run(
        Scripted(Done(reason=REFUSED, text="")), ctx_for()))
check(body.startswith(NO_SEND),
      "with no relay at all it is still a rejection — the marker is what carries it")

body = asyncio.run(
    TurnReporter(None, LadderSettings()).run(
        Scripted(Done(reason=REFUSED, text="I only answer my owner.")), ctx_for()))
check(not body.startswith(NO_SEND) and "I only answer my owner." in body,
      "a refusal that DID say something is delivered: the rejection is about "
      "having nothing to say, not about the reason")

# An announcement that never reached the room is not lost inside a rejection.
async def _unposted_rejection():
    reporter = TurnReporter(None, LadderSettings())
    return await reporter.run(
        Scripted(Notice(text="🧠 the context was reset"), Done(reason=FAILED, text="")),
        ctx_for())


body = asyncio.run(_unposted_rejection())
check("the context was reset" in body,
      "an announcement the room never got is archived with the rejection, not dropped")


print("\n-- the pending-Turn structure --")

queue = SessionQueue((ROOM_A, "owner"))
first = queue.arrive(ctx_for(task_id="one"))
check(not first.queued and first.ahead == 0, "the first arrival waits for nobody")
second = queue.arrive(ctx_for(task_id="two"))
third = queue.arrive(ctx_for(task_id="three"))
check(second.queued and second.ahead == 1, "the second knows one Turn is ahead of it")
check(third.ahead == 2, "and the third knows there are two")


async def _order_kept():
    q = SessionQueue((ROOM_A, "owner"))
    order = []
    told = []

    async def announce(turn):
        told.append(turn.ctx.task_id)

    async def one(name, hold):
        async with q.arrive(ctx_for(task_id=name), on_wait=announce):
            order.append(("in", name))
            check(q.running is not None and q.running.ctx.task_id == name,
                  f"the queue names {name} as the Turn currently running")
            await hold.wait()
            order.append(("out", name))

    gate = asyncio.Event()
    a = asyncio.ensure_future(one("a", gate))
    await settle()
    b = asyncio.ensure_future(one("b", gate))
    await settle()
    running_while_busy = [n for k, n in order if k == "in"]
    gate.set()
    await asyncio.gather(a, b)
    return order, told, running_while_busy, q


order, told, running_while_busy, q = asyncio.run(_order_kept())
check(running_while_busy == ["a"], "only one Turn is inside the Session at a time")
check(told == ["b"], "and only the one that had to wait was announced")
check(order == [("in", "a"), ("out", "a"), ("in", "b"), ("out", "b")],
      "the queue is first in, first out: messages are answered in the order sent")
check(q.running is None and not q.outstanding,
      "and the Session is left free, with nothing counted against the next arrival")


async def _announcement_that_failed():
    q = SessionQueue((ROOM_A, "owner"))

    async def explode(turn):
        raise RuntimeError("the relay fell over")

    try:
        async with q.arrive(ctx_for(task_id="a")):
            pass
        async with q.arrive(ctx_for(task_id="b"), on_wait=explode):
            pass
    except RuntimeError:
        pass
    return q


q = asyncio.run(_announcement_that_failed())
check(not q.outstanding and q.running is None,
      "a Turn that never started leaves no phantom in the queue behind it")

registry = {}
check(queue_for(registry, (ROOM_A, "owner")) is queue_for(registry, (ROOM_A, "owner")),
      "one Session key, one queue, for the life of the Worker")
check(queue_for(registry, (ROOM_A, "owner")) is not queue_for(registry, (ROOM_A, "other")),
      "and a different Access Tier queues separately, as it is a different Session")
check(queue_for(registry, (ROOM_A, "owner")) is not queue_for(registry, (ROOM_B, "owner")),
      "as does a different room — which is what keeps rooms out of each other's way")

check(isinstance(PendingTurn(ctx_for(), SessionQueue(), 0), PendingTurn),
      "a pending Turn is an object with the running Turn beside it — the handle "
      "mid-Turn Steering will need, and the reason this is not a bare lock")

print("\n" + ("PASS — turn lifecycle green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
