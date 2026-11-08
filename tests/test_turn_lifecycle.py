"""Tests for what happens when a Turn does not simply produce an answer.

Three situations, all asserted the way the outside world sees them — the Room
Ops a **real local HTTP relay** recorded, and the outcome the Task was answered
with. Nothing here asserts on which internal object called which.

* **A message arriving while the Session is busy** waits its turn. s.space's
  queued indicator shows that; the Worker posts nothing about it.
* **A Turn that stopped short** carries an explicit line saying so, whatever it
  managed to produce.
* **A Turn that produced nothing** is `FAILED_OUTCOME`: nothing posted or
  edited by the Worker — the broker posts the one failure notice.

The Adapters are stubs emitting the event vocabulary, so this runs under bare
`python3`. Cancellation through the ACP protocol — the other half of this
ticket — is in `test_acp_lifecycle.py`, which needs a real ACP Agent to cancel.

Run: python3 tests/test_turn_lifecycle.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

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
    ANSWERED,
    FAILED_OUTCOME,
    STOP_LINES,
    TurnReporter,
)
from _taskqueue import FakeClient, room_ops_at, task
from agent_connect.worker import handle_one, process_one

ROOM_A = "!alpha:s.space"
ROOM_B = "!beta:s.space"

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
    return relay, room_ops_at(f"http://127.0.0.1:{relay.server.server_port}")


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


print("\n-- a second message waits its turn, silently, and is answered afterwards --")


async def _queueing():
    relay, ops = relay_ops()
    client = FakeClient()
    delivered = (task("task-q1", "the long one", room=ROOM_A),
                 task("task-q2", "the follow-up", room=ROOM_A),
                 task("task-q3", "another room", room=ROOM_B))
    adapter = Gated()
    adapter.gate = asyncio.Event()
    sessions = {}
    running = [
        asyncio.ensure_future(
            handle_one(one, adapter, "/repo", sessions, ops, client=client)
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

check(not mid["ops"] and not relay.ops,
      "nothing is posted about the wait — no queued notice, no placeholder")

check(adapter.started[-1] == "task-q2"
      and adapter.started.index("task-q1") < adapter.started.index("task-q2"),
      f"the queued Task ran after the one it waited for ({adapter.started})")
check(adapter.peak <= 2, "and never two Turns at once on the one Session")
for tid in ("task-q1", "task-q2", "task-q3"):
    check(f"answer for {tid}" in (client.answer(tid) or ""), f"{tid} was answered")
relay.stop()


print("\n-- a Turn that stopped short always says so --")


def run_turn_events(*events, ops=None):
    reporter = TurnReporter(ops)
    outcome = asyncio.run(reporter.run(Scripted(*events), ctx_for()))
    return outcome.body, outcome


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
check(not relay.ops, "the Worker posts nothing itself: the broker delivers the reply")
check(_.kind == ANSWERED, "it is a real reply: answered, and the lease completed")
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


print("\n-- a Turn that produced nothing is FAILED_OUTCOME --")


async def _rejection(done):
    relay, ops = relay_ops()
    reporter = TurnReporter(ops)
    outcome = await reporter.run(
        Scripted(ToolStarted(tool_id="a", title="Read x.py"), done), ctx_for())
    await settle(0.1)
    return relay, outcome


relay, outcome = asyncio.run(_rejection(Done(reason=REFUSED, text="")))
check(outcome.kind == FAILED_OUTCOME, "an empty refusal is FAILED_OUTCOME")
check("refused" in outcome.body, "the reason is in the body, for whoever reads the archive")
check(not relay.ops, "nothing was posted or edited: no Worker-authored failure message")
relay.stop()

relay, outcome = asyncio.run(_rejection(Done(reason=FAILED, text="")))
check(outcome.kind == FAILED_OUTCOME and "Read x.py" in outcome.body
      and "Operator diagnostics" in outcome.body,
      "operator diagnostics are retained only in the archived failure")
check(not relay.ops, "and still nothing reaches the room")
relay.stop()

relay, outcome = asyncio.run(_rejection(
    Done(reason=FAILED, text="", note="agent-connect: the ACP Agent exited (code 3)")))
check(outcome.kind == ANSWERED and "code 3" in outcome.body,
      "a failure the Adapter explained is answered with the explanation")
relay.stop()

outcome = asyncio.run(
    TurnReporter(None).run(Scripted(Done(reason=REFUSED, text="")), ctx_for()))
check(outcome.kind == FAILED_OUTCOME,
      "with no relay at all it is still FAILED_OUTCOME")

outcome = asyncio.run(
    TurnReporter(None).run(
        Scripted(Done(reason=REFUSED, text="I only work for my owner.")), ctx_for()))
check(outcome.kind == ANSWERED and "I only work for my owner." in outcome.body,
      "a refusal that DID say something is delivered: failure is about having "
      "nothing to say, not about the reason")


async def _unposted_rejection():
    reporter = TurnReporter(None)
    return await reporter.run(
        Scripted(Notice(text="🧠 the context was reset"), Done(reason=FAILED, text="")),
        ctx_for())


outcome = asyncio.run(_unposted_rejection())
check("the context was reset" in outcome.body,
      "an announcement the room never got is archived with the failure, not dropped")


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
