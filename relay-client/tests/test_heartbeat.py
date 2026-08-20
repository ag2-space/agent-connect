"""The heartbeat, and the number on it that the broker schedules against.

`inflight` is not local bookkeeping: the broker's presence sweep reads it, and a
count that only ever grows ends with the agent marked unassignable. That is not
hypothetical — 2026-07-09, 175 stranded ids, none of them with any work behind
them. So the count comes from the journal (E2), and the journal is reconciled
before it is counted (E3).

The rest of this file is the endpoint's manners: a broker that predates it
answers 404, and hard-failing there would strand every pre-heartbeat deployment;
a presence field that is not known is omitted rather than sent as null, because
the broker records presence only when it is present and a null clobbers what it
knew.

Run: python3 tests/test_heartbeat.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import json
import tempfile
import time

from fake_broker import FakeBroker

from ag2_relay_client.client import PROTOCOL_VERSION, RelayClient
from ag2_relay_client.credentials import TokenSource

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def wire_task(wire_id, **over):
    task = {"id": wire_id, "task": "do it", "channel_id": "!room:ag2.space",
            "user_id": "@alice:ag2.space", "access_tier": "owner"}
    task.update(over)
    return task


def bury(client):
    """Age a killed client's singleton record, the way its death would.

    A process that dies does not release the bearer's guard (J1) — it stops
    re-stamping it, and the replacement takes over once the record goes stale.
    That is what "the process dies here" means below.
    """
    if client.guard is None or not client.layout.singleton_path.exists():
        return
    record = json.loads(client.layout.singleton_path.read_text())
    record["heartbeat_ts"] = time.time() - 10_000
    client.layout.singleton_path.write_text(json.dumps(record))


def client_for(broker, tmp, **kwargs):
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    kwargs.setdefault("instance", "test")
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp, **kwargs)
    client.prepare()
    return client


# --- the payload, and what is deliberately not in it -----------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    check(client.heartbeat() is True, "a heartbeat is sent")
    beat = broker.took("POST", "/v1/heartbeat")[0].json
    check(beat["client"] == "ag2-relay-client", "it names the client")
    check(beat["protocol_version"] == PROTOCOL_VERSION,
          "and carries the one version this wire has")
    check(beat["tier"] == "owner", "the node's self-description rides along")
    check(beat["inflight"] == 0, "with nothing in flight")
    check(isinstance(beat["capabilities"], list) and "task-ack" in beat["capabilities"],
          "and a capability list the broker may ignore entry by entry")
    check("status" not in beat and "step" not in beat,
          "presence fields a status-less client does not know are OMITTED — a "
          "null would clobber the broker's last-known presence (E1)")

    # --- E2: inflight is the journal, not a counter
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-1"), wire_task("task-2")]})
    broker.on("POST", "/v1/results", json={"ok": True})
    client.poll_once()
    client.heartbeat()
    check(broker.took("POST", "/v1/heartbeat")[-1].json["inflight"] == 2,
          "two accepted tasks are two in flight (E2)")

    client.next_task(timeout=1)
    client.heartbeat()
    check(broker.took("POST", "/v1/heartbeat")[-1].json["inflight"] == 2,
          "taking a task off the queue changes nothing — the journal is the "
          "ledger, and the answer is still owed")

    client.complete("task-1", "done")
    client.heartbeat()
    check(broker.took("POST", "/v1/heartbeat")[-1].json["inflight"] == 1,
          "a POSTed result is out of the count")
    client.reject("task-2", "malformed")
    client.heartbeat()
    check(broker.took("POST", "/v1/heartbeat")[-1].json["inflight"] == 0,
          "and so is a dead-lettered one")

    # A result that could not be POSTed is still in flight: the broker is
    # waiting for it, whatever this client's opinion of its own progress is.
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-3")]})
    client.poll_once()
    client.next_task(timeout=1)
    broker.on("POST", "/v1/results", status=503, body="later")
    client.complete("task-3", "an answer that will not send")
    client.heartbeat()
    check(broker.took("POST", "/v1/heartbeat")[-1].json["inflight"] == 1,
          "an answer that has not been POSTed yet is still in flight (E2)")

# --- presence: only when known, and never a crash --------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("POST", "/v1/heartbeat", json={"ok": True})

    client.set_presence("working", "reading the repo")
    client.heartbeat()
    beat = broker.took("POST", "/v1/heartbeat")[-1].json
    check(beat.get("status") == "working" and beat.get("step") == "reading the repo",
          "presence is folded in when it is known")

    client.set_presence("idle")
    client.heartbeat()
    beat = broker.took("POST", "/v1/heartbeat")[-1].json
    check(beat.get("status") == "idle" and "step" not in beat,
          "a status without a step sends the status alone")

    for malformed in ({"status": "working"}, 42, [], object()):
        client.set_presence(malformed, malformed)
        client.heartbeat()
        beat = broker.took("POST", "/v1/heartbeat")[-1].json
        check("status" not in beat and "step" not in beat,
              f"a malformed presence input degrades to absent, not to a crash "
              f"and not to a null: {type(malformed).__name__}")

    client.set_presence("x" * 5000, "y" * 5000)
    client.heartbeat()
    beat = broker.took("POST", "/v1/heartbeat")[-1].json
    check(len(beat["status"]) == 500 and len(beat["step"]) == 500,
          "and an unbounded one is bounded")

# --- the interval, and the broker that has never heard of the endpoint -----
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp, heartbeat_interval=1000.0)
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    client.poll_once()
    check(len(broker.took("POST", "/v1/heartbeat")) == 1,
          "the first poll announces the client")
    client.poll_once()
    client.poll_once()
    check(len(broker.took("POST", "/v1/heartbeat")) == 1,
          "and the interval holds the rest back — the poll cadence is not the "
          "heartbeat cadence (E1)")
    check(client.heartbeat() is True, "an explicit heartbeat ignores the gate")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("POST", "/v1/heartbeat", status=404, body="no route")
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    check(client.heartbeat() is False, "a broker with no heartbeat route says 404")
    check(client.poll_once() == 0.0,
          "which is not an error — hard-failing here would strand every "
          "pre-heartbeat deployment (E1)")
    check(len(broker.took("POST", "/v1/heartbeat")) == 1,
          "and the client stops asking rather than retrying every pass")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    broker.on("POST", "/v1/heartbeat", status=500, body="broken")
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    check(client.heartbeat() is False, "a 500 fails")
    check(client.poll_once() == 0.0, "and is not a poll failure either")
    client.heartbeat()
    check(len(broker.took("POST", "/v1/heartbeat")) == 2,
          "but a server error is transient — the client keeps trying, unlike "
          "a 404, which is structural (the poll in between was inside the "
          "interval, which is the gate doing its job)")

# --- E3: the ledger has to be able to shrink -------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    first = client_for(broker, tmp)
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-lost")]})
    first.poll_once()
    first.next_task(timeout=1)
    # ...and the process dies here, with the Task in memory and no answer.
    bury(first)

    second = client_for(broker, tmp)
    check(second.inflight() == ["task-lost"],
          "the restarted client inherits the id its predecessor never answered")

    second.heartbeat()
    check(broker.took("POST", "/v1/heartbeat")[-1].json["inflight"] == 1,
          "the first heartbeat still counts it — one sighting is not proof, and "
          "a result landing mid-check must not be raced")
    second.heartbeat()
    check(broker.took("POST", "/v1/heartbeat")[-1].json["inflight"] == 0,
          "the second drops it: no answer can arrive for it here (E3)")
    check(second.journal.knows("task-lost") is False,
          "and the journal shrinks, so the count is not monotonic")

    # An id this run is holding is never reconciled away, however long the
    # consumer takes: the library cannot tell a slow Turn from an abandoned one.
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-slow")]})
    second.poll_once()
    second.next_task(timeout=1)
    for _ in range(5):
        second.heartbeat()
    check(second.inflight() == ["task-slow"],
          "a task this run accepted stays in flight until it is answered")

print("\n" + ("PASS — heartbeat green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
