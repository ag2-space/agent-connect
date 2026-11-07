"""Result statuses and lease fences, as core-api's activity ledger reads them.

`POST /v1/results` carries `status` (`completed`, `no_action_needed`, `failed`)
and echoes the fences the task was served under (`lease_id`,
`execution_generation`), refreshed when the broker re-serves the task. A result
the ledger refuses as superseded is dropped, not retried for ever.

Run: python3 tests/test_result_status.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import tempfile

from fake_broker import FakeBroker

from ag2_relay_client.client import NO_SEND, RelayClient
from ag2_relay_client.credentials import TokenSource

fails = 0


def check(cond, what):
    global fails
    print(("  ok   " if cond else "  FAIL ") + what)
    if not cond:
        fails += 1


def wire_task(wire_id="task-1", **over):
    task = {"id": wire_id, "task": "what is the status?", "source": "ag2space",
            "channel_id": "!room:ag2.space", "user_id": "@alice:ag2.space",
            "access_tier": "owner", "lease_id": "lease-1", "execution_generation": 3}
    task.update(over)
    return task


def client_for(broker, tmp):
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp, instance="test")
    client.prepare()
    return client


def served(broker, client, task):
    broker.on("GET", "/v1/tasks", json={"tasks": [task]})
    client.poll_once()
    return client.next_task(timeout=1)


with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("POST", "/v1/results", json={"ok": True})
    client = client_for(broker, tmp)

    served(broker, client, wire_task())
    client.complete("task-1", "the answer")
    posted = broker.took("POST", "/v1/results")[-1].json
    check(posted == {"id": "task-1", "body": "the answer", "status": "completed",
                     "lease_id": "lease-1", "execution_generation": 3},
          "an answer is `completed` and echoes the fences it was served under")

    served(broker, client, wire_task("task-2"))
    client.no_action("task-2", "an acknowledgement; nothing to say")
    posted = broker.took("POST", "/v1/results")[-1].json
    check(posted == {"id": "task-2", "status": "no_action_needed", "body": NO_SEND,
                     "no_send": True, "outcome": "an acknowledgement; nothing to say",
                     "lease_id": "lease-1", "execution_generation": 3},
          "no reply is `no_action_needed`, with the legacy flag for an older broker")

    served(broker, client, wire_task("task-3"))
    client.fail("task-3", "the bridge died")
    posted = broker.took("POST", "/v1/results")[-1].json
    check(posted["status"] == "failed" and posted["no_send"] is True
          and posted["body"] == f"{NO_SEND} the bridge died",
          "a failure is `failed`: the broker posts its notice, the diagnostics are archived")

    served(broker, client, wire_task("task-4"))
    client.complete("task-4", f"{NO_SEND} nothing for the room")
    posted = broker.took("POST", "/v1/results")[-1].json
    check(posted["status"] == "no_action_needed" and posted["no_send"] is True,
          "a skip marker through `complete` is a no action, as the ledger reads it")

    served(broker, client, wire_task("task-5", lease_id="", execution_generation=None))
    client.complete("task-5", "plain")
    posted = broker.took("POST", "/v1/results")[-1].json
    check("lease_id" not in posted and "execution_generation" not in posted,
          "a task served without fences sends none")

    served(broker, client, wire_task("task-6", execution_generation=True))
    client.complete("task-6", "plain")
    posted = broker.took("POST", "/v1/results")[-1].json
    check("execution_generation" not in posted,
          "a boolean is not a generation")

# --- a re-serve refreshes the fences ----------------------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    served(broker, client, wire_task("task-r"))
    broker.on("GET", "/v1/tasks", json={"tasks": [
        wire_task("task-r", lease_id="lease-2", execution_generation=4, attempt=2)]})
    client.poll_once()
    broker.on("POST", "/v1/results", json={"ok": True})
    client.complete("task-r", "after the re-serve")
    posted = broker.took("POST", "/v1/results")[-1].json
    check(posted.get("lease_id") == "lease-2" and posted.get("execution_generation") == 4,
          "a re-serve while the Turn runs refreshes the fences the result carries")

# --- superseded: dropped, never retried for ever ----------------------------
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    client = client_for(broker, tmp)
    served(broker, client, wire_task("task-s"))
    broker.on("POST", "/v1/results", status=409,
              json={"error": "stale", "error_code": "stale_execution_generation"})
    client.complete("task-s", "too late")
    check(client.journal.is_done("task-s") and not client.journal.pending_results(),
          "a result refused as superseded is retired, not retried")

    served(broker, client, wire_task("task-o"))
    broker.on("POST", "/v1/results", status=409, json={"error": "something else"})
    client.complete("task-o", "kept")
    check(client.journal.is_pending("task-o"),
          "any other 409 keeps the answer and retries it (F5)")

print("\n" + ("PASS — result statuses green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
