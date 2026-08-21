"""A revoked bearer is a wait, not a death (C2-C5, C8).

The historical behaviour was an immediate fatal exit on 401/403. Under a
supervisor that blindly relaunches, that is a silent crash-loop hammering the
gateway until a human notices — and the human is the one who has to do the
re-onboarding that would fix it. So: re-read the durable token source at once (a
rotation may already have happened while this client lagged), and otherwise hold
at a slow cadence, saying so in the status file every pass, until the rotation
lands. Then resume live.

The two edges around it matter as much as the middle. A rotation that names a
*different gateway* is a reconfiguration, not a rotation, and honoring it would
carry the freshly rotated bearer to the old endpoint (C5). And a 401 met by a
best-effort call — an ack, a heartbeat — must reach this recovery instead of
being swallowed as "an optional call failed" (C8), or recovery fires late and
inconsistently while the poll 401s separately.

Run: python3 tests/test_auth_recovery.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import json
import tempfile
from pathlib import Path

from fake_broker import FakeBroker

from ag2_relay_client.client import RelayClient
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.status import AUTH_WAIT, CONNECTED, FATAL

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def wire_task(wire_id="task-1"):
    return {"id": wire_id, "task": "do it", "channel_id": "!room:ag2.space",
            "user_id": "@alice:ag2.space", "access_tier": "owner"}


def state_of(client):
    return json.loads(client.layout.status_path.read_text())


# --- the durable source: hold, re-read, resume live ------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    token_file = Path(tmp) / "relay.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    client = RelayClient(TokenSource(token_file=token_file), tmp, instance="test",
                         auth_recheck_interval=0.05)
    client.prepare()

    broker.on("GET", "/v1/tasks", status=401, json={"error": "unknown token"})
    delay = client.poll_once()
    check(delay == 0.05,
          "a rejected bearer holds the loop at the re-check cadence, not the "
          "backoff — this is a wait for a human, not for a network")
    status = state_of(client)
    check(status["state"] == AUTH_WAIT and status["connected"] is False,
          "and every pass writes an observable status (C2, D2)")
    check("401" in (status["error"] or ""), "which names what happened")
    check(not client._stop.is_set(),
          "the client does not exit: a durable token source means a rotation "
          "can still fix this")

    for _ in range(3):
        client.poll_once()
    check(len(broker.took("GET", "/v1/tasks")) == 4,
          "it keeps re-checking rather than giving up or spinning")

    # The connect flow re-runs and writes a new secret. The *file* is the
    # durable source; nothing restarts.
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CSECOND\n")
    delay = client.poll_once()
    check(delay == 0.0,
          "the pass that finds the rotation asks the loop to come straight back")
    check(client.credentials.secret == "SECOND",
          "the new bearer is live, read through the same parse as startup (C4)")

    broker.forget()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    check(client.poll_once() == 0.0, "and the next poll is healthy")
    check(state_of(client)["state"] == CONNECTED, "the status says connected again")
    check(broker.took("GET", "/v1/tasks")[0].headers["Authorization"] == "Bearer SECOND",
          "carrying the rotated bearer — no restart, no rebuilt client (C3)")
    check(client.next_task(timeout=1) is not None,
          "and delivery resumes where it left off")

# --- C5: a rotation never moves the gateway --------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    token_file = Path(tmp) / "relay.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    client = RelayClient(TokenSource(token_file=token_file), tmp, instance="test",
                         auth_recheck_interval=0.05)
    client.prepare()
    broker.on("GET", "/v1/tasks", status=403, json={"error": "forbidden"})
    check(client.poll_once() == 0.05, "a 403 is the same wait as a 401")

    # A re-onboard against a DIFFERENT gateway. Honoring it would split this
    # process across two gateways and send the fresh bearer to the old one.
    token_file.write_text("REMOTE_TASK_TOKEN=https://elsewhere.example/relay%7CSECOND\n")
    check(client.poll_once() == 0.05,
          "a token file naming a different gateway is not a rotation — the "
          "client keeps waiting rather than hot-swapping (C5)")
    check(client.credentials.secret == "FIRST", "the running bearer is untouched")
    check(client.snapshot()["gateway"].startswith("http://127.0.0.1"),
          "and so is the gateway it talks to")
    check(state_of(client)["state"] == AUTH_WAIT, "the wait continues, visibly")

# --- no durable source: the historical contract, and only here -------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp, instance="test")
    client.prepare()
    broker.on("GET", "/v1/tasks", status=401, json={"error": "revoked"})
    client.poll_once()
    check(state_of(client)["state"] == FATAL,
          "with nothing to re-read, the rejection is terminal and says so")
    check(client._stop.is_set(), "and the loop stops rather than hammering")
    polls = len(broker.took("GET", "/v1/tasks"))
    client._run()  # the loop body: it must return immediately
    check(len(broker.took("GET", "/v1/tasks")) == polls, "a stopped loop does not poll")
    check(client.next_task(timeout=0.05) is None,
          "a library reports this and stops; it does not kill its host process")

# --- C8: the best-effort paths do not get to swallow a revoked bearer ------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    token_file = Path(tmp) / "relay.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    client = RelayClient(TokenSource(token_file=token_file), tmp, instance="test",
                         auth_recheck_interval=0.05)
    client.prepare()
    # The poll is fine; the ack is not. Without the re-raise this looks like a
    # string of harmless ack failures while nothing recovers.
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/tasks/*/ack", status=401, json={"error": "revoked"})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    delay = client.poll_once()
    check(delay == 0.05, "a 401 from the ack path reaches auth recovery (C8)")
    check(state_of(client)["state"] == AUTH_WAIT, "and is visible as such")
    check(client.next_task(timeout=1) is not None,
          "while the task the broker leased is still delivered — recovery does "
          "not tear through a batch the broker is waiting on")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    token_file = Path(tmp) / "relay.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    client = RelayClient(TokenSource(token_file=token_file), tmp, instance="test",
                         auth_recheck_interval=0.05)
    client.prepare()
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    broker.on("POST", "/v1/heartbeat", status=403, json={"error": "revoked"})
    check(client.poll_once() == 0.05, "a 403 from the heartbeat reaches it too (C8)")
    check(state_of(client)["state"] == AUTH_WAIT, "and is visible as such")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    token_file = Path(tmp) / "relay.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    client = RelayClient(TokenSource(token_file=token_file), tmp, instance="test",
                         auth_recheck_interval=0.05)
    client.prepare()
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    client.poll_once()
    task = client.next_task(timeout=1)
    broker.on("POST", "/v1/results", status=401, json={"error": "revoked"})
    client.complete(task.id, "the answer")
    check(client.journal.is_pending(task.id),
          "a result rejected for auth is retained like any other failure (F5)")
    check(client.poll_once() == 0.05,
          "and the rejection reaches recovery on the next turn of the loop (C8)")

print("\n" + ("PASS — auth recovery green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
