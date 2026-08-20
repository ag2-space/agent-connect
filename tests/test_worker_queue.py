"""The Worker on the real Relay Client, at the seam between them.

Everything else in this suite drives the Worker through `_queue.FakeClient`,
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
from pathlib import Path

from ag2_relay_client import RelayClient, TokenSource
from agent_connect.events import Done, MessageChunk
from agent_connect.status import RELAY_FIELDS, StatusFile
from agent_connect.worker import EMPTY_TASK, serve

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
    loop = asyncio.ensure_future(serve(adapter, "/repo", results, relay, 0.02))
    for _ in range(400):
        await asyncio.sleep(0.01)
        if until():
            break
    alive = not loop.done()
    loop.cancel()
    return alive


alive = asyncio.run(_one_pass(relay, adapter, lambda: bool(http.results())))
check(alive, "the drain loop is still turning after the Task it answered")
check(len(http.results()) == 1, "one Task off the queue, one result POSTed")
check(http.results()[0] == {"id": "q-1", "body": "the answer"},
      "under the broker's own id, carrying exactly what the Turn produced")
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

print("\n" + ("PASS — the Worker on the real client green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
