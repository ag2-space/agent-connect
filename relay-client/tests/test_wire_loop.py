"""The loop, against a broker on localhost: lease, journal, ack, results.

Every scenario here is an incident replayed. The redelivery test is the
2026-06-30 / 2026-07-01 reconnect floods, where the gateway replayed its unacked
pool and a client without dedup re-executed 500 historical tasks. The restart
test is the answer that was written and then lost between the worker's
completion and the POST. The ack-404 tests are the two things one 404 means, and
the day treating the per-task one as "this broker has no ack route" blinded a
whole host's `received` state.

Run: python3 tests/test_wire_loop.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import json
import tempfile
import time
from pathlib import Path

from fake_broker import FakeBroker

from ag2_relay_client import journal as journal_module
from ag2_relay_client.client import NO_SEND, RelayClient
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.state import StateLayout
from ag2_relay_client.status import CONNECTED, RECONNECTING
from ag2_relay_client.transport import RelayHTTP

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def wire_task(wire_id="task-1", **over):
    """A task as the broker's documented envelope serves it."""
    task = {
        "id": wire_id,
        "task": "[AG2Space @alice:ag2.space] what is the status?",
        "source": "ag2space",
        "channel_id": "!room:ag2.space",
        "user_id": "@alice:ag2.space",
        "access_tier": "owner",
        "priority": "normal",
        "timestamp": "2026-08-20T10:00:00Z",
    }
    task.update(over)
    return task


def client_for(broker, tmp, **kwargs):
    """A client pointed at `broker`, with the routine routes answered.

    The ack and heartbeat routes are programmed by default because they are
    best-effort in the library and a test that has not said otherwise is not
    testing them; the ones that are program their own answers over these.
    """
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    kwargs.setdefault("instance", "test")
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp, **kwargs)
    client.prepare()
    return client


class Timeouts:
    """Every timeout the client asked for, per path.

    A wrapper and not an assertion on the socket, because the fake broker
    answers instantly: nothing in this suite would notice if the long poll's
    socket timeout were set to the long-poll window itself, and that is the one
    value where being wrong turns every idle poll into an error and every idle
    poll into a lost lease (F1).
    """

    def __init__(self, inner):
        self.inner = inner
        self.seen = []

    @property
    def base_url(self):
        return self.inner.base_url

    def get(self, path, params=None, timeout=None):
        self.seen.append((path, timeout))
        return self.inner.get(path, params=params, timeout=timeout)

    def post(self, path, payload=None, timeout=None):
        self.seen.append((path, timeout))
        return self.inner.post(path, payload, timeout=timeout)

    def for_path(self, fragment):
        return [t for path, t in self.seen if fragment in path]


def bury(client):
    """Age a killed client's singleton record, the way its death would.

    A process that dies does not release the bearer's guard (J1) — it simply
    stops re-stamping it, and the next client takes it over once the record goes
    stale. Every "and then the process dies here" below means that, so it is
    written down once here rather than by giving the replacement a special
    client that skips the guard.
    """
    if client.guard is None or not client.layout.singleton_path.exists():
        return
    record = json.loads(client.layout.singleton_path.read_text())
    record["heartbeat_ts"] = time.time() - 10_000
    client.layout.singleton_path.write_text(json.dumps(record))


class JournalWatchingHTTP:
    """A request path that remembers what the journal held at each call.

    F2 is an *ordering* requirement — ack only after local acceptance is
    durable — and ordering is invisible in a log of requests. So this records
    the journal file as it was when each request left, which is the only way to
    fail the test that ack-then-crash-before-durability would pass.
    """

    def __init__(self, inner, journal_path):
        self.inner = inner
        self.journal_path = Path(journal_path)
        self.seen = []

    @property
    def base_url(self):
        return self.inner.base_url

    def get(self, path, params=None, timeout=None):
        self._note("GET", path)
        return self.inner.get(path, params=params, timeout=timeout)

    def post(self, path, payload=None, timeout=None):
        self._note("POST", path)
        return self.inner.post(path, payload, timeout=timeout)

    def _note(self, method, path):
        try:
            text = self.journal_path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        self.seen.append((method, path, text))

    def journal_at(self, method, path_fragment):
        for seen_method, path, text in self.seen:
            if seen_method == method and path_fragment in path:
                return text
        return None


# --- one task's whole life, and the ordering inside it ---------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    layout = StateLayout(tmp, "test")
    # Handed in at construction, because `_http` is sealed: a public, writable
    # `client.http` is `client.http.post("/v1/rooms/X/media", ...)` — every
    # egress rule bypassed in one line — which is the hatch ticket 04 closed on
    # `RoomOps` and this closes here.
    watching = JournalWatchingHTTP(
        RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), layout.journal_path)
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp,
                         instance="test", http=watching)
    client.prepare()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/results", json={"ok": True})

    delay = client.poll_once()
    check(delay == 0.0, "a healthy poll asks the loop to come straight back")

    polled = broker.took("GET", "/v1/tasks")[0]
    check(polled.query == "wait=25",
          "the long poll asks for 25s — under the server's cap of 30 (F1)")
    check(polled.headers.get("Authorization") == "Bearer SECRET", "with the bearer")

    task = client.next_task(timeout=1)
    check(task is not None and task.id == "task-1", "the task comes out of the queue")
    check(task.room_id == "!room:ag2.space" and task.access_tier == "owner",
          "carrying the room and the attestation, as data")

    # F2: the ack left AFTER the journal entry was durable, never before. An
    # ack-then-crash the other way round leaves the broker showing "received"
    # for work no surviving process knows about.
    at_ack = watching.journal_at("POST", "/ack")
    check(at_ack is not None and "task-1" in at_ack and "accepted" in at_ack,
          "the ack goes out only after the journal entry is durable (F2)")
    check(broker.took("POST", "/v1/tasks/task-1/ack"),
          "and it goes out, under the task's own id")

    check(client.journal.room_for("task-1") == "!room:ag2.space",
          "the task->room sidecar is captured at accept (F7)")
    check(client.inflight() == ["task-1"], "the id is in flight until it is answered (E2)")

    client.complete("task-1", "the answer")
    posted = broker.took("POST", "/v1/results")
    check(len(posted) == 1 and posted[0].json == {"id": "task-1", "body": "the answer"},
          "complete() POSTs the documented result shape")
    check(client.journal.is_done("task-1"), "and a successful POST retires the id")
    check(client.inflight() == [], "which empties the in-flight ledger")
    check(client.journal.room_for("task-1") == "",
          "the sidecar is retired with it (F5, F7)")

# --- F3: a redelivery is re-completed, never handed to the consumer twice ---
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/results", json={"ok": True})
    client.poll_once()
    client.next_task(timeout=1)
    client.complete("task-1", "the answer")

    broker.forget()
    # The gateway replays its unacked pool on reconnect: the same id, a bumped
    # attempt. This is the 500-task flood in miniature.
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(attempt=2, lease_id="l-2")]})
    broker.on("POST", "/v1/results", json={"ok": True, "duplicate": True})
    client.poll_once()

    check(client.next_task(timeout=0.1) is None,
          "a redelivered task the client already answered never reaches the consumer")
    replayed = broker.took("POST", "/v1/results")
    check(len(replayed) == 1 and replayed[0].json == {"id": "task-1", "body": NO_SEND},
          "it is re-completed upstream with a skip marker instead (F3, H1)")
    check(client.journal.is_done("task-1"), "and the id stays done")
    check(client.inflight() == [], "a re-completed redelivery leaves nothing in flight")

    # Twice more, because a flood is not one task.
    broker.forget()
    # Fresh ids: "already answered" is the case above, and reusing an
    # id here would test it twice instead of testing this.
    flood = [wire_task(f"flood-{n}") for n in range(3)]
    broker.on("GET", "/v1/tasks", json={"tasks": flood})
    client.poll_once()
    check(len([t for t in iter(lambda: client.next_task(0.05), None)]) == 3,
          "tasks the client has never seen are delivered normally")
    for n in range(3):
        client.complete(f"flood-{n}", f"answer {n}")
    broker.forget()
    broker.on("GET", "/v1/tasks", json={"tasks": flood})
    broker.on("POST", "/v1/results", json={"ok": True, "duplicate": True})
    client.poll_once()
    check(client.next_task(timeout=0.1) is None,
          "and a replay of all of them re-executes none of them")
    check(len(broker.took("POST", "/v1/results")) == 3,
          "each one is re-completed once")

# --- the kill between accept and complete ----------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    first = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    first.poll_once()
    task = first.next_task(timeout=1)
    check(task is not None, "the task is delivered")

    # The answer is produced, and the POST fails — a 502 from the edge, a
    # dropped link, a kill -9 a millisecond later. All the same shape.
    broker.on("POST", "/v1/results", status=502, body="bad gateway")
    first.complete("task-1", "the answer")
    check(first.journal.is_pending("task-1"),
          "a failed result POST retains the answer (F5)")
    on_disk = json.loads(first.layout.journal_path.read_text().splitlines()[0])
    check(on_disk["result"]["body"] == "the answer",
          "and the answer itself is on disk, not only in memory")

    # The process dies here. `first` is never stopped, never drained again.
    bury(first)
    broker.forget()
    broker.on("POST", "/v1/results", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(attempt=2)]})
    second = client_for(broker, tmp)

    check(second.journal.is_pending("task-1"),
          "the restarted client reads the unfinished answer out of the journal")
    second.poll_once()

    delivered = broker.took("POST", "/v1/results")
    check(delivered and delivered[0].json == {"id": "task-1", "body": "the answer"},
          "which it re-completes upstream — the user's answer, not a placeholder")
    check(second.next_task(timeout=0.1) is None,
          "and the task is never re-executed: the consumer never sees it (F3)")
    check(second.journal.is_done("task-1"), "the id ends done")
    check(second.inflight() == [], "and nothing is left in flight")

# --- F5: retention is not a retry count. Only success retires --------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    client.poll_once()
    client.next_task(timeout=1)

    broker.on("POST", "/v1/results", status=500, body="nope")
    client.complete("task-1", "the answer")
    for _ in range(3):
        broker.on("GET", "/v1/tasks", json={"tasks": []})
        client.poll_once()
    check(client.journal.is_pending("task-1"),
          "three failed passes later the answer is still owed, not forgotten")
    tried = len(broker.took("POST", "/v1/results"))
    time.sleep(1.05)   # past the first rung of this result's own ladder
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    client.poll_once()
    check(len(broker.took("POST", "/v1/results")) > tried,
          "and it is tried again — on its own ladder now, not once per pass: a "
          "result that fails costs its own id a delay, and costs the answers "
          "behind it nothing")

    # A 200 that says the broker did not take it is not a success either.
    broker.on("POST", "/v1/results", json={"ok": False, "error": "busy"})
    client._result_retry_at.clear()   # its own ladder is not what is under test
    client.poll_once()
    check(client.journal.is_pending("task-1"),
          "a 200 answering ok:false retains the result too")

    broker.on("POST", "/v1/results", json={"ok": True})
    client._result_retry_at.clear()
    client.poll_once()
    check(client.journal.is_done("task-1"), "success, and only success, retires it")

# --- A3: one poisoned result must not sit on every answer behind it --------
# `pending_results()` is oldest-first, so a `break` on the first failure puts
# the poisoned answer permanently at the head of the queue: everything behind it
# is durable, owed, and never sent. It is unreachable by `Reconciler` (which
# only inspects accepted ids), never re-offered (a redelivery of a pending id is
# a no-op), and `inflight` only grows — E3's scar, arriving through the front
# door. The real trigger is a reply large enough to outlast RESULT_TIMEOUT_S,
# because `add_result` does the room's outbound send inside the request.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks",
              json={"tasks": [wire_task("task-a"), wire_task("task-b")]})
    client.poll_once()
    check(len([t for t in iter(lambda: client.next_task(0.1), None)]) == 2,
          "two tasks are delivered")

    # task-a's answer is the one the broker will not take. task-b's is fine, and
    # task-b is behind it in the queue.
    broker.on("POST", "/v1/results", status=500, body="that one, never")
    client.complete("task-a", "the poisoned answer")
    broker.forget()
    broker.on("POST", "/v1/results", json={"ok": True})
    client.complete("task-b", "the answer behind it")

    posted = [r.json["id"] for r in broker.took("POST", "/v1/results")]
    check("task-b" in posted,
          "the answer behind the failing one is POSTed on the same pass — a "
          "per-result failure is not a broker-wide verdict (A3, F5, E3)")
    check(client.journal.is_done("task-b"), "and it is retired")
    check(client.journal.is_pending("task-a"),
          "while the failing one is retained, not dropped (F5)")
    check(client.inflight() == ["task-a"],
          "so the in-flight ledger has a way back down, which is the whole of "
          "E3: it must not grow monotonically")

    # And again with the poisoned answer *due* rather than merely gated: it is
    # retried, it fails again, and the queue still moves. A `break` here is what
    # made the failure permanent — `pending_results()` is oldest-first, so the
    # poisoned answer is at the head on every pass forever.
    broker.forget()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-c")]})
    client.poll_once()
    client.next_task(timeout=1)
    time.sleep(1.05)                  # past the first rung of task-a's ladder
    broker.on("POST", "/v1/results", status=500, body="that one, never")
    broker.on("POST", "/v1/results", json={"ok": True})
    client.complete("task-c", "a third answer, behind the poisoned one")
    check(client.journal.is_done("task-c"),
          "an answer behind a due-and-still-failing one is delivered, not "
          "queued behind it forever (A3)")
    check(client.journal.is_pending("task-a"), "and the failing one is still owed")

    client._result_retry_at.clear()   # skip its backoff; the ladder is not this
    broker.on("POST", "/v1/results", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    client.poll_once()
    check(client.journal.is_done("task-a"),
          "and the head of the queue is still retried, so nothing is lost")

# --- F4: the two things a 404 on the ack route means -----------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp, ack_cooldown=0.4)
    # One task's lease is gone; every other task on this host is fine. Treating
    # this as "the broker has no ack route" is what blinded a whole host.
    broker.on("POST", "/v1/tasks/task-stale/ack", status=404,
              json={"error": "not leased to you"})
    broker.on("GET", "/v1/tasks",
              json={"tasks": [wire_task("task-stale"), wire_task("task-fresh")]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/task-stale/ack")) == 1,
          "the stale task is acked once")
    check(len(broker.took("POST", "/v1/tasks/task-fresh/ack")) == 1,
          "and the per-task 404 does not stop the next task being acked (F4)")
    check(client.journal.is_accepted("task-stale"),
          "a lost lease does not throw away the task — the broker may still "
          "want the answer, and the result POST is what completes anything")

    # A broker with no ack route at all: a bare 404, and every ack pauses.
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", status=404, body="no route")
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-a")]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/*/ack")) == 1, "the first ack tries")
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-b"), wire_task("task-c")]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/*/ack")) == 1,
          "and the ones behind it do not — acking is cooled down, not hammered")
    check(client.next_task(timeout=0.1) is not None,
          "tasks are still delivered while acking is cooled down: the ack does "
          "not gate the handoff (F2)")
    # A2: the cooldown is not free and must not be silent. Against this broker
    # an un-acked lease is extended on liveness alone twice and then requeued
    # with `attempt` bumped, so everything accepted inside a 300 s pause is
    # re-served under a running Turn and eventually dead-lettered. The old
    # assertion here read "the ack is informational and gates nothing", which is
    # the belief that hid it.
    check(json.loads(client.layout.status_path.read_text())["acks_paused_s"] > 0,
          "and the pause is in the status file, with how much of it is left — a "
          "delivery outage a supervisor cannot see is the 2026-07-25 shape (D2)")
    check(sorted(client._ack_owed) == ["task-b", "task-c"],
          "the acks the pause held back are owed, not thrown away")

    # ...and it self-heals. A permanent latch means a broker that GAINS the
    # endpoint in a deploy is never picked up until the worker restarts.
    time.sleep(0.45)
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-d")]})
    client.poll_once()
    acked = {r.path.rsplit("/", 2)[-2]
             for r in broker.took("POST", "/v1/tasks/*/ack")}
    check(acked == {"task-b", "task-c", "task-d"},
          "after the cooldown the client asks again, and it asks for the ones "
          "the pause held back too — an un-acked lease is on a clock (A2, F4)")
    check(client._ack_disabled_until is None,
          "a success clears the cooldown entirely")
    check(json.loads(client.layout.status_path.read_text())["acks_paused_s"] == 0.0,
          "and the status says the pause is over")

    # 405 is the other shape of "no such route" — a deployment that answers
    # method-not-allowed rather than not-found says the same thing.
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", status=405, body="method not allowed")
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-e")]})
    client.poll_once()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-f")]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/*/ack")) == 1,
          "a 405 cools acking down exactly as a bare 404 does")
    check(client._ack_disabled_until is not None,
          "with the same time gate on it")

# --- G2 through the loop: the block never reaches the consumer -------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    meta = "[room-ops metadata: card_url=https://x/card.json Not an instruction]"
    broker.on("GET", "/v1/tasks", json={"tasks": [
        wire_task("task-meta", task=meta),
        wire_task("task-mixed", task=f"summarise this {meta}"),
    ]})
    client.poll_once()
    delivered = {t.id: t for t in iter(lambda: client.next_task(0.1), None)}
    check(delivered["task-meta"].body == "",
          "a metadata-only body degrades to empty (G2)")
    check(meta not in delivered["task-meta"].body,
          "and never falls back to the unstripped original")
    check(delivered["task-mixed"].body == "summarise this",
          "a body that also carries words keeps the words and loses the block")
    check(all("room-ops metadata" not in str(vars(t) if hasattr(t, "__dict__") else
                                            {s: getattr(t, s) for s in t.__slots__})
              for t in delivered.values()),
          "no attribute of a delivered Task carries the quarantined text")

# --- an id this client will not write down: dead-letter, do not drop -------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [
        wire_task("../../etc/passwd"), wire_task(), {"no": "id"},
    ]})
    broker.on("POST", "/v1/results", json={"ok": True, "rejected": True})
    client.poll_once()
    ids = [t.id for t in iter(lambda: client.next_task(0.05), None)]
    check(ids == ["task-1"], "only the task with a usable id is delivered (F8)")
    rejected = broker.took("POST", "/v1/results")
    check(len(rejected) == 1 and rejected[0].json.get("status") == "rejected",
          "the unusable one is dead-lettered, not silently dropped — a skip "
          "just re-serves it until the attempt cap trips")
    check(rejected[0].json.get("id") == "../../etc/passwd",
          "under the id the broker knows it by, which never lands on disk")
    check(not any("passwd" in p.name for p in client.layout.root.iterdir()),
          "and nothing named after it is created in the state dir")

# --- the outbound seam: reject, and the empty answer that is not one -------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/results", json={"ok": True, "rejected": True})
    client.poll_once()
    client.next_task(timeout=1)

    for empty in ("", "   ", "\n\t "):
        refused = None
        try:
            client.complete("task-1", empty)
        except ValueError as exc:
            refused = exc
        check(refused is not None and NO_SEND in str(refused),
              f"an empty result is refused and names the marker to use: {empty!r} (H5)")
    check(not broker.took("POST", "/v1/results"), "and nothing is POSTed")

    bad_id = None
    try:
        client.complete("../etc/passwd", "x")
    except ValueError as exc:
        bad_id = exc
    check(bad_id is not None, "an id that is not a wire slug is refused at egress too (F8)")

    client.reject("task-1", "unreadable attachment")
    body = broker.took("POST", "/v1/results")[0].json
    check(body == {"id": "task-1", "status": "rejected",
                   "error_code": "UNREADABLE_ATTACHMENT"},
          "reject() uses the documented dead-letter shape")
    check(client.journal.is_done("task-1"), "and the id is finished with")
    check(len(broker.took("POST", "/v1/results")) == 1,
          "the worker posts no give-up message of its own — the broker owns "
          "the terminal-failure notice, and a second one doubles it")

# --- D2: every poll outcome leaves something to read -----------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    client.poll_once()
    status = json.loads(client.layout.status_path.read_text())
    check(status["state"] == CONNECTED and status["connected"] is True,
          "a healthy poll writes a connected status")
    check(status["last_ok_ts"] > 0, "with the timestamp a supervisor renders")
    check(status["gateway"].startswith("http://127.0.0.1"),
          "and the gateway it is pointed at")
    connected_at = status["last_ok_ts"]

    broker.on("GET", "/v1/tasks", status=500, body="down")
    delay = client.poll_once()
    status = json.loads(client.layout.status_path.read_text())
    check(delay == 1.0 and status["backoff_s"] == 1.0,
          "a failed poll backs off from 1s (D1) and says so")
    check(status["state"] == RECONNECTING and status["connected"] is False,
          "the state says reconnecting, not silence")
    check(status["error"] and "500" in status["error"], "the error is readable")
    check(status["last_ok_ts"] == connected_at,
          "and last_ok_ts survives the reconnecting write — 'last connected N "
          "seconds ago' is the number an operator actually needs")
    check(client.poll_once() == 2.0, "the backoff doubles while it stays down")
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    check(client.poll_once() == 0.0, "and one healthy round-trip resets it (D1)")

    # D3: a gateway URL with credentials in it never lands in the file.
    creds = TokenSource(token=f"http://user:pass@127.0.0.1:1/relay?token=abc|SECRET")
    with tempfile.TemporaryDirectory() as other:
        loud = RelayClient(creds, other, instance="redact")
        loud.prepare()
        loud._update_status(RECONNECTING, error="x")
        written = loud.layout.status_path.read_text()
        check("pass" not in written and "token=abc" not in written,
              "the persisted status carries no URL credentials (D3)")

    # D4: a consumer hook that raises is the consumer's problem, not the loop's.
    seen = []

    def angry(snapshot):
        seen.append(snapshot["state"])
        raise RuntimeError("the consumer's hook is broken")

    client.on_status(angry)
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    check(client.poll_once() == 0.0, "a raising status hook does not break the poll (D4)")
    check(seen == [CONNECTED], "though it was called")

# --- the loop as a loop: start, poll, stop ---------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp, idle_gap=0.02)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    broker.on("POST", "/v1/results", json={"ok": True})
    client.start()
    task = client.next_task(timeout=5)
    check(task is not None and task.id == "task-1",
          "a started client delivers on its own thread")
    client.complete(task.id, "answered from the consumer thread")
    check(client.journal.is_done("task-1"),
          "and complete() works from a thread that is not the poll thread")
    client.stop()
    check(json.loads(client.layout.status_path.read_text())["state"] == "stopped",
          "a stopped client says so")
    polls = len(broker.took("GET", "/v1/tasks"))
    time.sleep(0.15)
    check(len(broker.took("GET", "/v1/tasks")) == polls,
          "and stops polling — a poller that outlives its stop double-delivers")

# --- A1: a re-serve of a task the consumer still holds must be re-acked ----
# The client's model came from WORKER-PROTOCOL.md ("the ack never touches the
# lease") and the doc is wrong. `take()` re-leases with no `acknowledged_ts`
# carried over, so every re-serve starts un-acked, and an un-acked lease is
# extended on liveness alone only twice before it is requeued with `attempt`
# bumped. A ten-minute Turn under one backoff window therefore burned all five
# attempts and was dead-lettered at about minute fifteen while the consumer was
# still working — and the eventual `complete()` POSTed into a broker with no
# lease and no room for it, which answers 200. The client retired it as
# delivered and the user's answer was gone, with no error at either end.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    client.poll_once()
    held = client.next_task(timeout=1)
    check(held is not None, "the consumer has the task and has not answered yet")

    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    # The lease expired under a slow Turn and the broker re-served it: same id,
    # bumped attempt, a brand new un-acked lease.
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(attempt=2)]})
    client.poll_once()

    check(len(broker.took("POST", "/v1/tasks/task-1/ack")) == 1,
          "the re-served task is acked again — that ack is the only thing that "
          "keeps the new lease alive past the un-acked extend grace (A1)")
    check(client.next_task(timeout=0.1) is None,
          "and it is not handed to the consumer a second time (F3)")
    check(client.inflight() == ["task-1"],
          "it is still the same one piece of work, owed once")

    # The same holds while the answer is written but the POST has not landed:
    # the lease is still this client's, still un-acked after the re-serve, and
    # the drain needs it alive long enough to complete it.
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    broker.on("POST", "/v1/results", status=503, body="later")
    client.complete("task-1", "the answer, which the broker will not take yet")
    check(client.journal.is_pending("task-1"), "the answer is owed")
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(attempt=3)]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/task-1/ack")) == 1,
          "a re-serve of an id whose answer is waiting is re-acked too — that "
          "lease has to outlive the retries the answer is going through (A1)")

    # The same hole swallowed the per-task `not leased` 404: that ack is skipped
    # and never retried, so the task went un-acked for its whole life.
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", status=404,
              json={"error": "not leased to you"})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-churn")]})
    client.poll_once()
    client.next_task(timeout=1)
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-churn", attempt=2)]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/task-churn/ack")) == 1,
          "an ack the lease churn refused is asked again on the next re-serve, "
          "rather than never (F4's per-task 404 is not a permanent skip)")

    # And a re-ack goes out even while F4's cooldown is running: the cooldown is
    # for a route that is not there, and a re-serve is a live lease about to be
    # requeued. It is naturally rate-limited — once per visibility window per
    # task — so it can never become the hammering the cooldown exists to stop.
    client._ack_disabled_until = time.monotonic() + 300.0
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(attempt=3)]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/task-1/ack")) == 1,
          "a re-ack is not swallowed by the ack cooldown (A1, A2)")

# --- A4: a journal write that fails must not stop the poll (D4) ------------
# D4 is explicit: nothing in the poll iteration before the GET may raise.
# `write_private_atomic` can — ENOSPC, a read-only mount, a state dir removed
# under a running client — and `_drain_results`'s retire and `_heartbeat`'s
# reconcile both reach it. Reproduced: the result POST *succeeded* and only the
# retire failed, so every later pass re-POSTed and re-raised at the same line;
# the loop backed off to 60 s and never polled again, every in-flight lease
# expired, and the status file still said `reconnecting`.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/results", status=502, body="bad gateway")
    client.poll_once()
    client.next_task(timeout=1)
    client.complete("task-1", "the answer")
    check(client.journal.is_pending("task-1"), "an answer is owed to the broker")

    def refuse_to_write(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    # The next pass POSTs it successfully and then cannot write the retire —
    # which is exactly the reproduced shape, and the worst one, because the
    # answer *did* land and the loop stopped anyway.
    saved_write = journal_module.write_private_atomic
    journal_module.write_private_atomic = refuse_to_write
    client._result_retry_at.clear()   # its own ladder is not what is under test
    broker.forget()
    broker.on("POST", "/v1/results", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    try:
        delay = client.poll_once()
    finally:
        journal_module.write_private_atomic = saved_write

    check(broker.took("POST", "/v1/results"), "the result POST goes out")
    check(delay == 0.0,
          "and the journal write that failed behind it does not back the loop "
          "off — before this, the loop backed off to 60s and never polled "
          "again, with every in-flight lease quietly expiring (A4, D4)")
    check(broker.took("GET", "/v1/tasks"),
          "the poll itself still goes out, which is the whole of D4's rule: "
          "nothing before the GET may be what stops delivery")
    check(json.loads(client.layout.status_path.read_text())["state"] == CONNECTED,
          "and the status says connected, not a reconnecting it never comes "
          "back from")
    check(client.journal.is_done("task-1"),
          "the retire held in memory, so the id is not re-POSTed forever; the "
          "file is a version behind in the direction that only costs a "
          "duplicate the broker dedups")

    # A write that fails while a *task* is being taken in costs that task and
    # not the batch: the broker re-serves it and F3 absorbs the duplicate.
    journal_module.write_private_atomic = refuse_to_write
    broker.forget()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-x"), wire_task("task-y")]})
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    try:
        delay = client.poll_once()
    finally:
        journal_module.write_private_atomic = saved_write
    check(delay == 0.0, "a leased task that cannot be journalled does not back "
                        "the loop off either")
    check(broker.took("GET", "/v1/tasks"),
          "and the client keeps polling, which is what keeps the leases it "
          "already holds alive (F1)")

# --- A5: the ack must not gate delivery, and a batch must not gate the poll -
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    # Every ack costs the fake broker 150 ms. Inline and serialised, ten of them
    # were 1.5 s of delivery latency before the first Task reached the consumer
    # — and at the ack's own 10 s timeout, a ten-task batch is 100 s of poll
    # iteration, which is a lost lease for every task still in flight (F1, F2).
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True}, delay=0.15)
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp,
                         instance="test", intake_budget=0.4)
    client.prepare()
    broker.on("GET", "/v1/tasks",
              json={"tasks": [wire_task(f"batch-{n}") for n in range(10)]})
    started = time.monotonic()
    client.poll_once()
    turn = time.monotonic() - started

    delivered = [t.id for t in iter(lambda: client.next_task(0.05), None)]
    check(len(delivered) == 10, "the whole batch reaches the consumer")
    check(turn < 1.5,
          f"and the turn is bounded by the intake budget, not by the batch "
          f"length ({turn:.2f}s for ten 150ms acks)")
    acked = len(broker.took("POST", "/v1/tasks/*/ack"))
    check(0 < acked < 10,
          f"only what fitted in the budget was acked this turn ({acked} of 10)")
    check(len(client._ack_owed) == 10 - acked,
          "and the rest are owed to the next turn, not dropped — 'we will ack "
          "it eventually' is a real deadline, not bookkeeping")

    broker.on("GET", "/v1/tasks", json={"tasks": []})
    for _ in range(6):
        if not client._ack_owed:
            break
        client.poll_once()
    check(not client._ack_owed,
          "later turns drain the backlog, oldest first")

# --- the socket timeout, which no test used to look at ---------------------
# The fake broker answers instantly, so setting the long poll's socket timeout
# to the long-poll window itself left the whole suite green — and in production
# turns every idle poll into a timeout, every timeout into a backoff, and every
# backoff into the lost leases F1 is about.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    timeouts = Timeouts(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")))
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp,
                         instance="test", http=timeouts)
    client.prepare()
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    client.poll_once()
    check(timeouts.for_path("/v1/tasks") == [client.poll_wait + client.socket_margin],
          "the long poll's socket timeout is the window plus the margin — a "
          "timeout at or below the window makes every idle poll an error (F1)")
    check(all(t == 10.0 for t in timeouts.for_path("/v1/heartbeat")),
          "and a side call gets a side call's timeout, so it can never hold up "
          "the poll behind it")

# --- C2: the authenticated session is not a public escape hatch ------------
# `RelayHTTP` is a bearer token with a `.post`, and a public attribute holding
# one is `client.http.post("/v1/rooms/X/media", {"content_b64": ...})` — every
# rule in `egress.py` bypassed in one line. Ticket 04 sealed the identical hatch
# on `RoomOps`; this is the same seal. A caller with its own transport hands it
# in at construction, which is the only supported route.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    check(not hasattr(client, "http"), "`client.http` is gone")
    check(not hasattr(client, "__dict__"),
          "and there is no instance dict for the seal to be walked around")
    for attempt in ("http", "_http"):
        refused = None
        try:
            setattr(client, attempt, object())
        except AttributeError as exc:
            refused = exc
        check(refused is not None,
              f"`client.{attempt} = ...` is refused — a session that can be "
              f"*replaced* is the same hatch with a different spelling")
    deleted = None
    try:
        del client._http
    except AttributeError as exc:
        deleted = exc
    check(deleted is not None, "and it cannot be deleted out of the way either")

# --- A8: the clocks the loop gates on are monotonic -------------------------
# An NTP step backwards used to extend the ack cooldown by the size of the step
# and suspend the heartbeat for as long — and against this broker a pause on
# acking is a pause on delivery. The behaviour is asserted above; this is the
# fence around it, because `time.time()` is the obvious thing to reach for.
CLIENT_SOURCE = (Path(_bootstrap.ROOT) / "ag2_relay_client" / "client.py").read_text()
check("time.time()" not in CLIENT_SOURCE,
      "no wall clock gates anything in the loop: a duration measured on a "
      "clock that can step is not a duration (A8)")

# --- F1's other half: never health-probe with the endpoint that leases -----
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/healthz", json={"status": "ok", "healthy": True})
    check(client.healthz()["healthy"] is True, "healthz() answers the liveness question")
    check(not broker.took("GET", "/v1/tasks"),
          "without leasing a single task (F1)")

# --- the outbound half, reachable at last ----------------------------------
# `Outbound`, `RoomOps`, `egress` and `markers` shipped complete and *unwired*:
# `complete` POSTed a raw `{"id", "body"}`, so the marker grammar this library
# owns was applied by nobody and a `[file:]` an agent wrote travelled to the
# room as literal text naming a local path. These are the four joins that make
# the outbound half part of the client rather than beside it.

check("egress_roots" in CLIENT_SOURCE and "Outbound(" in CLIENT_SOURCE,
      "the client builds its own allowlist and its own Outbound — the "
      "outbound half is reachable by consuming the client, not by wiring it up")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    workspace = Path(tmp) / "work"
    workspace.mkdir()
    (workspace / "report.md").write_text("# what I found\n")
    (Path(tmp) / "secret.key").write_text("-----BEGIN PRIVATE KEY-----\n")

    client = client_for(broker, Path(tmp) / "state", egress_roots=[workspace])

    # Construction is the whole of the egress policy (the spec's enumerated
    # surface: "egress allowlist roots, immutable after construction").
    check(client.room_ops.allowlist.roots == (str(workspace.resolve()),),
          "the roots the client was built with are the roots it may send from")
    try:
        client.room_ops.allowlist._roots = ("/",)  # noqa: SLF001 — the attack
        widened = True
    except AttributeError:
        widened = False
    check(not widened, "and nothing widens them afterwards")
    try:
        client._room_ops = None  # noqa: SLF001 — nor replaces the holder
        replaced = True
    except AttributeError:
        replaced = False
    check(not replaced, "nor replaces the object that holds them")

    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(
        "task-out", source_event_id="$intake")]})
    broker.on("POST", "/v1/rooms/*/media", json={"mxc": "mxc://ag2/1"})
    broker.on("POST", "/v1/results", json={"ok": True})
    client.poll_once()

    # I2: the wire loop is what tells Room Ops which events it was served, and
    # the only caller of `note_intake_event` used to be a test. Both halves are
    # asserted — a refusal that came from the op failing anyway would prove
    # nothing, so the working case is right beside it.
    broker.on("POST", "/v1/room", json={"ok": True})
    broker.on("POST", "/v1/room", json={"ok": True})
    check(client.room_ops.react("!room:ag2.space", "$something-else", "👍") is True,
          "a reaction the consumer asks for on its own account goes")
    check(client.room_ops.react("!room:ag2.space", "$intake", "🫡") is False,
          "but an event this client was *served* is never reacted to — the "
          "broker already put the intake reaction on it, and a second one is "
          "the room seeing double (I2)")
    check(len(broker.took("POST", "/v1/room")) == 1,
          "and the refused one never reached the wire at all")

    prepared = client.complete(
        "task-out", "Here it is.\n\n[file: report.md]", base_dir=workspace)
    uploads = broker.took("POST", "/v1/rooms/%21room%3Aag2.space/media")
    check(len(uploads) == 1 and uploads[0].json.get("filename") == "report.md",
          "a file the answer names is uploaded from an allowlisted path, in "
          "this process, before the result POST")
    posted = [r.json for r in broker.took("POST", "/v1/results")]
    check(len(posted) == 1 and posted[0]["body"] == "Here it is.",
          "and the marker is gone from the body: the room reads prose")
    check(prepared.uploaded == ("report.md",),
          "what went is reported back to the consumer")

    # The regression this ticket exists to close, from the other direction.
    prepared = client.complete(
        "task-out", "Sure.\n\n[file: %s]" % (Path(tmp) / "secret.key"),
        base_dir=workspace)
    check(not broker.took("POST", "/v1/rooms/%21room%3Aag2.space/media")[1:],
          "a path outside every root is refused in-process — nothing is "
          "uploaded and no bytes are read")
    seen = [r.json for r in broker.took("POST", "/v1/results")]
    body = (seen[-1] if seen else {}).get("body", "")
    check("secret.key" in body and "attachment not sent" in body,
          "and the refusal is in the result body, so the room is told by name")
    check("[file:" not in body,
          "with no marker left to reach the room as literal text — which is "
          "exactly what the retired staging protocol did")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    # F6: a result POST that fails and is retried must not re-upload.
    workspace = Path(tmp) / "work"
    workspace.mkdir()
    (workspace / "chart.png").write_bytes(b"chart")
    client = client_for(broker, Path(tmp) / "state", egress_roots=[workspace])
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-f6")]})
    broker.on("POST", "/v1/rooms/*/media", json={"mxc": "mxc://ag2/2"})
    broker.on("POST", "/v1/results", status=500, body=b"nope")
    client.poll_once()
    client.complete("task-f6", "Chart.\n[file: chart.png]", base_dir=workspace)
    client._result_retry_at.clear()          # skip the per-result backoff gate
    broker.on("POST", "/v1/results", json={"ok": True})
    client._drain_results()
    check(len(broker.took("POST", "/v1/rooms/%21room%3Aag2.space/media")) == 1,
          "a retried result POST re-derives the same body and uploads nothing "
          "more — one chart in the room, not two (F6)")
    check(client.outbound.already_sent("task-f6") == (),
          "and the ledger is retired by the POST that finally succeeded, "
          "because only success may retire one (F5)")


print("\n" + ("PASS — wire loop green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
