"""`stop()` during a long poll: the thread leaves at once and the guard is released.

The incident: under launchd a Worker is stopped while its poll thread is inside
a 25 s long poll. A stop that can only wait for that poll to come back runs out
of budget, leaves the singleton guard held (J1, correctly), and the replacement
Worker stands by for the whole freshness window, about 2.5 minutes, on every
restart. Cutting the long poll short is what lets the join succeed.

Run: python3 tests/test_stop.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import tempfile
import time

from fake_broker import FakeBroker

from ag2_relay_client.client import RelayClient
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.status import RECONNECTING

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    # The long poll: held open far longer than any stop budget.
    broker.on("GET", "/v1/tasks", json={"tasks": []}, delay=20.0)
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp,
                         instance="test")
    statuses = []
    client.on_status(lambda snap: statuses.append(snap.get("state")))
    client.start()
    check(wait_for(lambda: broker.took("GET", "/v1/tasks")),
          "the poll thread is parked in a long poll")
    time.sleep(0.2)  # well inside the request, past connect

    before_stop = len(statuses)  # start() itself reports RECONNECTING once
    started = time.monotonic()
    client.stop(timeout=5.0)
    took = time.monotonic() - started
    check(took < 2.0,
          f"stop() returns at once instead of waiting out its join ({took:.2f}s)")
    lock = client.layout.singleton_path
    check(lock.exists() and lock.read_text().strip() == "",
          "the guard is released, so a replacement can poll immediately (J1)")
    check(RECONNECTING not in statuses[before_stop:],
          "a poll cut short by stop() is not reported as a reconnect")
    check(len(broker.took("GET", "/v1/tasks")) == 1,
          "and no second poll leaves after the stop")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    # A restarted client polls normally: the abort is fresh on every start.
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    broker.on("GET", "/v1/tasks", json={"tasks": []}, delay=20.0)
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp,
                         instance="test")
    client.start()
    wait_for(lambda: broker.took("GET", "/v1/tasks"))
    time.sleep(0.2)
    client.stop(timeout=5.0)
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    client.start()
    check(wait_for(lambda: len(broker.took("GET", "/v1/tasks")) >= 3),
          "after a stop, start() polls again: the old abort does not linger")
    client.stop(timeout=5.0)

print("\n" + ("PASS — stop green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
