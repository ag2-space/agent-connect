"""The Worker on the real Relay Client, at the seam between them.

Everything else in this suite drives the Worker through `_taskqueue.FakeClient`,
which is the right trade almost everywhere — the Worker's knowledge of the wire
is five methods, and a test of Session queueing should not need a broker. But a
hand-written stand-in only proves the Worker agrees with *itself*. This file
runs the same drain loop against `ag2_relay_client.RelayClient` itself, with
only its HTTP replaced, so the two things this ticket claims are asserted
against the object that will really be there:

* **Every Task that comes off the queue is answered, once.** A `complete`
  reaches the broker's `/v1/results`; a `reject` reaches the same endpoint as
  the documented dead-letter shape. Neither is a body this side invented — the
  library writes them, and the journal underneath is what makes them survive.
* **Nothing is dropped in between.** The Worker takes a Task, runs it, and the
  library's ledger of ids owed an answer goes back to empty. An id left in it is
  a lease left to expire, a redelivery, and a dead-letter five attempts later.

The HTTP is replaced rather than mocked out of the client: `RelayClient` takes
its transport at construction, so this uses the seam the library already offers
instead of reaching inside it. Everything above that seam is the real thing —
the poll, the intake, the journal, the id validation, the result retention, the
status reporter — and the fixtures are raw broker JSON, so a Task gets here the
way a Task gets here.

Run: python3 tests/test_worker_queue.py   (no dependencies)
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import tempfile
import time
from pathlib import Path

from ag2_relay_client import RelayClient, TokenSource
from ag2_relay_client.status import (
    AUTH_WAIT,
    CONNECTED,
    DISPLACED,
    FATAL,
    RECONNECTING,
    STANDBY,
    STOPPED,
)
from agent_connect import status as status_module
from agent_connect.events import Done, MessageChunk
from agent_connect.status import RELAY_FIELDS, StatusFile
from agent_connect.worker import EMPTY_TASK, RelayStop, serve

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


tmp = Path(tempfile.mkdtemp())
_runs = 0


class RecordingHTTP:
    """The broker, as far as the library can tell: it serves and it accepts.

    The wire itself — leases, redelivery, ack-404s, backoff — is the library's
    own suite's business (`relay-client/tests/test_wire_loop.py` drives a real
    `http.server` through all of it). All that is needed here is somewhere for a
    Task to come from and somewhere for a result to go.
    """

    base_url = "https://broker.example/relay"

    def __init__(self):
        self.serving = []
        self.posted = []

    def serve(self, *raw):
        """Hand these out on the next poll, and then no more."""
        self.serving.extend(raw)

    def get(self, path, params=None, timeout=None):
        served, self.serving = self.serving, []
        return {"tasks": served}

    def post(self, path, payload=None, timeout=None):
        self.posted.append((path, dict(payload or {})))
        return {"ok": True}

    def results(self):
        return [body for path, body in self.posted if path == "/v1/results"]


def client():
    """A real `RelayClient`, prepared but not polling, on its own state dir."""
    global _runs
    _runs += 1
    http = RecordingHTTP()
    made = RelayClient(
        TokenSource(token="https://broker.example/relay|s3cret"),
        state_dir=tmp / f"state-{_runs}",
        instance="test",
        http=http,
    )
    made.prepare()
    return made, http


class Answers:
    """An Adapter that says one thing and stops."""

    def __init__(self, text="the answer"):
        self.text = text
        self.seen = []

    async def turn(self, ctx):
        self.seen.append(ctx)
        yield MessageChunk(text=self.text)
        yield Done(text=self.text)


def outgoing():
    global _runs
    path = tmp / f"results-{_runs}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def wire(task_id, body, room="", tier="owner"):
    """One task as the broker's JSON, which is the only shape it ever has."""
    raw = {"id": task_id, "task": body, "access_tier": tier}
    if room:
        raw["channel_id"] = room
    return raw


def deliver(relay, http, *raw):
    """Serve these, and let the real poll do the accepting.

    One turn of the library's own loop: journal, ack, queue, in that order and
    through that code. Half of what is asserted below is that the journal's
    ledger empties out again, and a test that seeded the ledger by hand would
    be asserting against its own seeding.
    """
    http.serve(*raw)
    relay.poll_once()


print("\n-- an answered Task reaches /v1/results, through the real client --")

relay, http = client()
deliver(relay, http, wire("q-1", "summarise it", room="!room:ag2.space"))
adapter = Answers()
results = outgoing()


async def _one_pass(relay, adapter, until):
    """Run the drain loop until `until()` is true, then stop it."""
    loop = asyncio.ensure_future(serve(adapter, "/repo", relay, 0.02))
    for _ in range(400):
        await asyncio.sleep(0.01)
        if until():
            break
    alive = not loop.done()
    loop.cancel()
    return alive


async def _second_pass(relay, http, adapter, raw):
    """Answer one Task, serve another *into the running loop*, answer that too.

    The liveness assertion this replaces was `not loop.done()` against a `serve`
    whose body is an unconditional `while True` — it could only fail if `serve`
    raised, which is a different claim from the one it was written under. A loop
    that is still turning is a loop that answers the next thing it is given, so
    that is what is asserted: the second Task is delivered after the first has
    been answered, through the same running loop.
    """
    loop = asyncio.ensure_future(serve(adapter, "/repo", relay, 0.02))
    for _ in range(400):
        await asyncio.sleep(0.01)
        if http.results():
            break
    first = len(http.results())
    http.serve(raw)
    relay.poll_once()
    for _ in range(400):
        await asyncio.sleep(0.01)
        if len(http.results()) > first:
            break
    loop.cancel()
    return [r.get("id") for r in http.results()]


answered = asyncio.run(_second_pass(relay, http, adapter, wire("q-1b", "and again")))
check(answered == ["q-1", "q-1b"],
      "the drain loop answers the Task that arrives after the first one — a "
      "loop that is still turning is one that takes the next thing it is given")
check(http.results()[0] == {"id": "q-1", "body": "the answer"},
      "each off the queue once, under the broker's own id, carrying exactly "
      "what the Turn produced")
check(relay.inflight() == [],
      "and the ledger of ids owed an answer is empty again — an id left in it "
      "is a lease left to expire and a Turn that runs a second time")
check(adapter.seen and adapter.seen[0].room == "!room:ag2.space",
      "the Turn was built from the delivered Task, room and all")


print("\n-- a Task nothing could answer is dead-lettered, not dropped --")

relay, http = client()
deliver(relay, http, wire("q-2", ""))
adapter = Answers()
results = outgoing()
asyncio.run(_one_pass(relay, adapter, lambda: bool(http.results())))
check(http.results() == [{"id": "q-2", "status": "rejected",
                          "error_code": EMPTY_TASK}],
      "the reject travels as the protocol's documented dead-letter shape — the "
      "flow sparrow never implemented, and the reason the old code could only "
      "write `[no-send] empty task` and hope")
check(adapter.seen == [], "the Adapter was never started for it")
check(relay.inflight() == [], "and the broker is not left waiting on it either")


print("\n-- an upload with no caption is not one of them --")

# The whole chain, through the real client: an uncaptioned upload from Element
# sends `caption == filename`, so the body the broker forwards is *only* a media
# marker — and the library strips markers unconditionally, because "the
# attachment tuple is where that task's content is". So the Worker is handed an
# empty body and one attachment. Judged on the body alone that reads as empty,
# and empty is terminal: `reject` dead-letters it, the broker posts a
# failure notice, and someone who dropped a screenshot into a room is told their
# message could never be answered while the Local Agent never saw it.
relay, http = client()
deliver(relay, http, wire("q-6", "[ag2space-media: https://broker.example/media/1 "
                                 "name=Screenshot.png mime=image/png]",
                          room="!shot:ag2.space"))
adapter = Answers()
results = outgoing()
asyncio.run(_one_pass(relay, adapter, lambda: bool(http.results())))
check([r.get("id") for r in http.results()] == ["q-6"], "it is answered, once")
check("status" not in http.results()[0],
      "and it is a `complete`, not the dead-letter — a Task carrying a file has "
      "content, whatever its body says")
check(len(adapter.seen) == 1, "the Adapter ran for it")
seen = adapter.seen[0] if adapter.seen else None
check(seen is not None and "Screenshot.png" in seen.prompt,
      "and was asked about the file by name, in band, because nobody typed a "
      "word for it to be asked about otherwise")
check(seen is not None and len(seen.attachments) == 1
      and seen.attachments[0].filename == "Screenshot.png",
      "the file itself travels beside the prompt, in the Adapter boundary's own "
      "vocabulary — `filename`, not the library's `name`")
check(relay.inflight() == [], "and nothing is left owed to the broker")


print("\n-- the answer is the library's to deliver, and it keeps it --")

# H5, at the seam that hands results across: a blank body is "not ready", not an
# answer, and the real client raises on one. The Worker must never be able to
# hand it one — which is why an empty Turn is a reject rather than a `complete`
# with nothing in it.
relay, http = client()
deliver(relay, http, wire("q-3", "say nothing"))


class Silent:
    async def turn(self, ctx):
        yield Done(text="")


results = outgoing()
asyncio.run(_one_pass(relay, adapter=Silent(), until=lambda: bool(http.results())))
body = http.results()[0].get("body", "")
check(body.startswith("[no-send]"),
      "a Turn that produced nothing completes the lease with the skip marker, "
      "so the broker posts the failure notice and the Worker posts none")
check(body.strip(), "and never with a blank body, which the client refuses (H5)")


print("\n-- one Task, one answer, whatever the Turn did --")

relay, http = client()
deliver(relay, http,
        wire("q-4", "explode", room="!a:ag2.space"),
        wire("q-5", "fine", room="!b:ag2.space"))


class Selective:
    async def turn(self, ctx):
        if "explode" in ctx.prompt:
            raise RuntimeError("adapter blew up")
        yield Done(text="ok")


results = outgoing()
asyncio.run(_one_pass(relay, Selective(), lambda: len(http.results()) == 2))
answered = {r["id"] for r in http.results()}
check(answered == {"q-4", "q-5"}, "both Tasks were answered")
check(len(http.results()) == 2, "each exactly once")
check(relay.inflight() == [], "and nothing is still owed to the broker")


print("\n-- the status hook is the one the Worker registers on --")

relay, http = client()
status = StatusFile(tmp / "hooked" / "status.json", heartbeat=10.0)
status.serving(detail="serving")
relay.on_status(status.relay)
relay.poll_once()
doc = json.loads((tmp / "hooked" / "status.json").read_text())
check(set(doc.get("relay") or {}) == set(RELAY_FIELDS),
      "one poll outcome and the Worker's own file carries the connection state")
check((doc["relay"] or {}).get("connected") is True,
      "a healthy round trip reads as connected")
check("s3cret" not in json.dumps(doc),
      "and the bearer is nowhere in it")

# The projection is a promise — "a field the library grows and this file should
# carry is a line added here" — and it was quietly broken twice: `recheck_s`
# (how long a standby waits) and `acks_paused_s` (the field a supervisor needs
# to tell "quiet" from "every lease is being requeued") arrived in the library
# and never in the projection. Asserted against the snapshot itself, so the next
# field the library grows fails here rather than going missing in silence.
check(set(RELAY_FIELDS) == set(relay.snapshot()) - {"instance"},
      "the projection is every field of the library's snapshot but `instance`, "
      "which this document already carries under its own name")

# The other half of the same promise: the *values* `relay.state` can take are a
# documented enum, and two of them — `standby` and `displaced` — were missing
# from both documents for as long as the singleton guard has existed. The one an
# operator sees for two and a half minutes after every unclean restart was the
# one with no name in the contract.
documented = (_bootstrap.ROOT / "README.md").read_text() + status_module.__doc__
check(all(f"`{name}`" in documented or f" {name} " in documented
          for name in (CONNECTED, RECONNECTING, AUTH_WAIT, FATAL, STANDBY,
                       DISPLACED, STOPPED)),
      "every state the library can write is named in the README and in the "
      "module that projects it — an undocumented state is one an observer has "
      "to guess at")


print("\n-- stopping the client is what releases the bearer's guard --")

# J1: the singleton guard is released only when `stop()`'s join actually joined,
# because a guard released while the loop may still poll is two pollers on one
# bearer. So a Worker that does not wait long enough leaves the record to go
# stale, and its replacement stands by for `STALE_AFTER_S` + `STANDBY_RECHECK_S`
# — near three minutes of messages going nowhere — every single restart.
relay, http = client()
relay.start()
for _ in range(200):
    if relay.guard.held:
        break
    time.sleep(0.01)
check(relay.guard.held, "a started client holds the bearer's guard")
record = Path(relay.guard.path)
stopping = RelayStop(budget=5.0)
stopping.watch(relay)
began = time.monotonic()
stopping.finish()
check(not relay.guard.held and time.monotonic() - began < 5.0,
      "the Worker's stop joins the poll thread and the guard is given up, so a "
      "replacement polls immediately instead of waiting out a freshness window")
check(not record.read_text().strip(),
      "and the record on disk is emptied rather than left to age out")


class LateLeaver:
    """A client whose poll thread leaves `after` seconds from now.

    The real one is inside a 25-second long poll almost all of the time, and
    `stop()` releases the guard only if the join outlived it. Two seconds — the
    budget this replaces — could never do that, so `guard.release()` was
    unreachable code and every restart paid the standby.
    """

    def __init__(self, after: float):
        self.leaves_at = time.monotonic() + after
        self.released = False
        self.waited = 0.0

    def stop(self, timeout: float = 5.0) -> None:
        began = time.monotonic()
        while time.monotonic() < self.leaves_at and time.monotonic() - began < timeout:
            time.sleep(0.01)
        self.waited = time.monotonic() - began
        self.released = time.monotonic() >= self.leaves_at


late = LateLeaver(after=0.6)
short = RelayStop(budget=0.2)
short.watch(late)
short.finish()
check(not late.released,
      "a budget shorter than the poll it has to outlive releases nothing — "
      "which is exactly what `stop(timeout=2.0)` did against a 25s long poll")

late = LateLeaver(after=0.6)
enough = RelayStop(budget=3.0)
enough.watch(late)
enough.finish()
check(late.released, "a budget that can actually succeed releases the guard")

# And it is begun at the signal rather than at the end, so the wait overlaps
# whatever the Worker is still doing to put itself away.
late = LateLeaver(after=0.6)
overlapped = RelayStop(budget=3.0)
overlapped.watch(late)
overlapped.begin()
time.sleep(0.7)                             # the loop tearing down
began = time.monotonic()
overlapped.finish()
check(late.released and time.monotonic() - began < 0.3,
      "a stop that began at the signal has already finished by the time the "
      "Worker gets round to waiting for it")

check(RelayStop(budget=1.0).finish() is None,
      "and a Worker that never got as far as having a client stops cleanly")

print("\n-- a Relay Client that stopped for good takes the Worker with it --")

# The failure `serve` already names about its queue reader, reached from the
# other side: the reader is fine and the thing it reads from has ended. The
# library stops its poll thread on exactly two verdicts — a bearer rejected with
# nothing to re-read (`fatal`), and a singleton guard another poller took
# (`displaced`) — and its own contract is that coming back is the consumer's
# decision. Nothing above it asked. The state went into the status file's
# `relay` block, where it was true and unread, while the document's own `state`
# went on saying `serving` and the heartbeat went on beating it, receiving
# nothing, for ever.


class Ended:
    """A client whose queue is empty because its poll loop has stopped."""

    def __init__(self, state):
        self.state = state
        self.reads = 0

    def next_task(self, timeout=None):
        self.reads += 1
        time.sleep(0.01)
        return None

    def snapshot(self):
        return {"state": self.state}


async def _drains_until_it_cannot(client):
    try:
        await asyncio.wait_for(serve(Answers(), "/repo", client, 0.01), timeout=5)
    except RuntimeError as exc:
        return str(exc)
    except asyncio.TimeoutError:
        return ""


for state in (FATAL, DISPLACED):
    said = asyncio.run(_drains_until_it_cannot(Ended(state)))
    check(state in said and "can no longer be given work" in said,
          f"a client in `{state}` ends the drain loop with the reason in the "
          f"sentence, so the status file gets an `error` and the process exits")

# The other half, and the one that must not regress: everything else the library
# can report is the library *waiting*, and waiting is not dying. A `standby`
# keeps asking for a bearer another poller holds — that is how a holder which
# died is taken over from with no operator in the loop — and `auth-wait` holds
# at a slow cadence until a rotation lands. A Worker that exited on those would
# turn its own patience into a restart loop.
for state in (RECONNECTING, AUTH_WAIT, STANDBY, STOPPED, CONNECTED):
    waiting = Ended(state)
    check(asyncio.run(_drains_until_it_cannot(waiting)) == "" and waiting.reads > 1,
          f"`{state}` is the library waiting, and the Worker waits with it")

# And a caller driving Tasks by hand may have no snapshot to ask at all.


class ByHand:
    """The consumer surface without the observer half of it."""

    def next_task(self, timeout=None):
        time.sleep(0.01)
        return None


check(asyncio.run(_drains_until_it_cannot(ByHand())) == "",
      "a client with no snapshot is a caller driving Tasks by hand, and the "
      "absence of an observer is not a verdict about the wire")


print("\n" + ("PASS — the Worker on the real client green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
