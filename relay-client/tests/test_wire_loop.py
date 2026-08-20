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

from ag2_relay_client.client import NO_SEND, RelayClient
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.status import CONNECTED, RECONNECTING

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
    client = client_for(broker, tmp)
    client.http = JournalWatchingHTTP(client.http, client.layout.journal_path)
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
    at_ack = client.http.journal_at("POST", "/ack")
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
    check(len(broker.took("POST", "/v1/results")) >= 4,
          "and every pass tries again")

    # A 200 that says the broker did not take it is not a success either.
    broker.on("POST", "/v1/results", json={"ok": False, "error": "busy"})
    client.poll_once()
    check(client.journal.is_pending("task-1"),
          "a 200 answering ok:false retains the result too")

    broker.on("POST", "/v1/results", json={"ok": True})
    client.poll_once()
    check(client.journal.is_done("task-1"), "success, and only success, retires it")

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
          "tasks are still delivered while acking is cooled down: the ack is "
          "informational and gates nothing (F2)")

    # ...and it self-heals. A permanent latch means a broker that GAINS the
    # endpoint in a deploy is never picked up until the worker restarts.
    time.sleep(0.45)
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-d")]})
    client.poll_once()
    check(len(broker.took("POST", "/v1/tasks/*/ack")) == 1,
          "after the cooldown the client asks again, and the deploy is picked up")
    check(client._ack_disabled_until == 0.0, "a success clears the cooldown entirely")

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
    check(client._ack_disabled_until > 0.0, "with the same time gate on it")

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

# --- F1's other half: never health-probe with the endpoint that leases -----
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("GET", "/v1/healthz", json={"status": "ok", "healthy": True})
    check(client.healthz()["healthy"] is True, "healthz() answers the liveness question")
    check(not broker.took("GET", "/v1/tasks"),
          "without leasing a single task (F1)")

print("\n" + ("PASS — wire loop green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
