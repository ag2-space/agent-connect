"""One bearer, one poller — the guard, and the two incidents behind it (J1).

The wire cannot tell you that a second client is polling the same queue. It
just splits the lease stream and delivers every task twice, which is what
happened twice on 2026-07-16: an orphaned bridge from a prior install that
outlived its parent by days and kept polling next to the live one, and a
replacement plus a desktop respawn starting in the same millisecond.

So the four properties below are the test plan, and each one is a way the naive
guard fails:

- **atomic acquire** — the simultaneous start must produce exactly one winner,
  so it is run for real here, in threads and in separate processes, not
  simulated by calling two methods in order;
- **liveness is heartbeat freshness, never pid-alive** — the ghost was
  alive-but-stale, and pid recycling makes `kill(pid, 0)` answer yes about a
  different process entirely. The sharp test is the pair: a *fresh* record with
  a dead pid keeps the guard, and a *stale* record with a live pid loses it;
- **a definitive loser stops immediately** — not at the next restart;
- **fail open** — a guard that cannot be evaluated means poll anyway. This is
  the load-bearing one: the risk of a dropped guard is the dual poller that was
  already possible, and the risk of a guard that fails closed is every task
  silently undelivered.

Run: python3 tests/test_singleton.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import ast
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fake_broker import FakeBroker

from ag2_relay_client import singleton
from ag2_relay_client.client import RelayClient
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.singleton import DEGRADED, HELD, IDLE, LOST, PollerGuard
from ag2_relay_client.status import DISPLACED, STANDBY

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Listening(logging.Handler):
    """Everything the library said, so "logged" can be asserted rather than
    assumed. A guard that degrades silently is how the orphaned bridge went
    unnoticed for days."""

    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage()))

    def warned(self, needle):
        return any(level >= logging.WARNING and needle in text
                   for level, text in self.lines)


def wire_task(wire_id="task-1"):
    return {"id": wire_id, "task": "do it", "channel_id": "!room:ag2.space",
            "user_id": "@alice:ag2.space", "access_tier": "owner"}


def client_for(broker, tmp, **kwargs):
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    kwargs.setdefault("instance", "test")
    client = RelayClient(TokenSource(token=f"{broker.url}|SECRET"), tmp, **kwargs)
    client.prepare()
    return client


def record_of(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rewrite(path, **fields):
    """Edit the guard record in place, the way time does."""
    record = record_of(path)
    record.update(fields)
    Path(path).write_text(json.dumps(record), encoding="utf-8")


# --- the guard, alone: one winner, and the loser knows it ------------------
with tempfile.TemporaryDirectory() as tmp:
    lock = Path(tmp) / "poller.lock"
    first = PollerGuard(lock)
    second = PollerGuard(lock)

    check(first.state == IDLE and first.held is False,
          "a guard starts holding nothing")
    check(first.claim() == HELD, "the first client takes the bearer")
    check(second.claim() == LOST, "the second one does not")
    check(second.held is False and second.displaced is False,
          "and knows it is a standby, not a displaced holder — the difference "
          "is whether it may keep asking")

    for _ in range(5):
        check(first.claim() == HELD and second.claim() == LOST,
              "a holder that keeps saying it is alive keeps the guard")

    check(record_of(lock)["owner"] == first.owner, "the record names the holder")
    check(record_of(lock)["pid"] == os.getpid(),
          "and carries its pid — as a diagnostic for a human, never as an input")

    # Release is what a clean stop does, and it is what keeps a restart from
    # waiting out a freshness window it does not need to.
    first.release()
    check(first.state == IDLE, "a released guard holds nothing")
    check(lock.exists() and lock.read_text().strip() == "",
          "the lock file is emptied, not unlinked — unlinking the inode other "
          "processes are about to lock is how a lock file stops being one")
    check(second.claim() == HELD, "and the standby takes it immediately")

    # A release only ever clears this client's own claim.
    first.release()
    check(record_of(lock)["owner"] == second.owner,
          "releasing a guard somebody else holds clears nothing")

# --- liveness is heartbeat freshness. Never pid-alive ---------------------
with tempfile.TemporaryDirectory() as tmp:
    lock = Path(tmp) / "poller.lock"
    holder = PollerGuard(lock, stale_after=0.4, refresh_interval=0.0)
    rival = PollerGuard(lock, stale_after=0.4)
    check(holder.claim() == HELD, "a holder takes the guard")

    # A live one never loses it: it keeps stamping the record, and the rival
    # keeps finding a fresh claim, however many times it asks.
    deadline = time.time() + 1.2
    kept = True
    while time.time() < deadline:
        holder.claim()
        if rival.claim() != LOST:
            kept = False
        time.sleep(0.05)
    check(kept, "a live holder never loses the guard, past several freshness "
                "windows — the rival keeps finding a fresh stamp")

    # ...and a hung one does. The holder stops saying it is alive — a wedged
    # process, the 21-hour DNS stall — while its pid stays perfectly alive.
    check(record_of(lock)["pid"] == os.getpid(),
          "the stale record's pid is this very process: alive, running, and "
          "irrelevant")
    time.sleep(0.5)
    check(rival.claim() == HELD,
          "a holder that stopped heartbeating loses the guard even though its "
          "pid is alive — pid-alive is not liveness (the ghost was "
          "alive-but-stale, and pid recycling makes kill(0) answer about "
          "somebody else entirely)")

    # The other half of the pair: a *fresh* record whose pid is long dead keeps
    # the guard. A guard that consulted the pid would steal this one.
    rewrite(lock, pid=4194304, host="a-machine-that-left")
    check(PollerGuard(lock, stale_after=0.4).claim() == LOST,
          "and a fresh record with a dead pid keeps its claim — freshness is "
          "the only question the guard asks")

# --- no liveness-by-pid anywhere in the source, ever -----------------------
# The behaviour above is the requirement; this is the fence around it. "Is the
# pid still there?" is the obvious thing to reach for the next time this file is
# edited, and it is obvious *and wrong*: it answers yes about a hung process and
# yes about a recycled pid belonging to something else entirely.
GUARD_TREE = ast.parse(
    (_bootstrap.ROOT / "ag2_relay_client" / "singleton.py").read_text())
CALLED = ({node.attr for node in ast.walk(GUARD_TREE)
           if isinstance(node, ast.Attribute)}
          | {node.id for node in ast.walk(GUARD_TREE)
             if isinstance(node, ast.Name)})
for smell in ("kill", "waitpid", "getpgid", "pid_exists", "psutil"):
    check(smell not in CALLED,
          f"the guard's code never asks whether a pid is alive: no {smell!r}")

# --- a definitive loser stops. And stays stopped ---------------------------
with tempfile.TemporaryDirectory() as tmp:
    lock = Path(tmp) / "poller.lock"
    hung = PollerGuard(lock, stale_after=0.3)
    replacement = PollerGuard(lock, stale_after=0.3)
    hung.claim()
    time.sleep(0.4)
    check(replacement.claim() == HELD, "the replacement takes a stale guard")
    check(hung.claim() == LOST, "and the previous holder finds out on its very "
                                "next turn — not at the next restart")
    check(hung.displaced is True,
          "displaced, not standing by: it HELD this bearer, and the incident is "
          "a reaped process and its replacement both polling")

    time.sleep(0.4)  # the replacement now looks stale too
    check(hung.claim() == LOST,
          "a displaced client does not quietly re-acquire when the guard goes "
          "stale again — that is the same incident with extra steps, plus "
          "flapping")
    check(record_of(lock)["owner"] == replacement.owner,
          "and it does not touch the record it lost")

    # Coming back is the consumer's decision, never the loop's: `release()` is
    # what a deliberate stop() calls, and it re-enters the arbitration honestly.
    hung.release()
    check(hung.claim() == HELD,
          "a client that was deliberately stopped and started again may take a "
          "guard nobody live holds")

# --- TOCTOU, for real: simultaneous start, in threads ----------------------
with tempfile.TemporaryDirectory() as tmp:
    lock = Path(tmp) / "poller.lock"
    racers = [PollerGuard(lock) for _ in range(16)]
    barrier = threading.Barrier(len(racers))
    verdicts = []
    verdict_lock = threading.Lock()

    def race(guard):
        barrier.wait()          # everybody leaves the gate together
        verdict = guard.claim()
        with verdict_lock:
            verdicts.append(verdict)

    threads = [threading.Thread(target=race, args=(g,)) for g in racers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    check(verdicts.count(HELD) == 1,
          f"sixteen clients starting at once produce exactly one winner "
          f"({verdicts.count(HELD)} held, {verdicts.count(LOST)} lost, "
          f"{verdicts.count(DEGRADED)} degraded)")
    check(verdicts.count(DEGRADED) == 0,
          "and none of them had to fail open to get there — a race resolved by "
          "degrading would make the count above meaningless")
    winners = [g for g in racers if g.held]
    check(len(winners) == 1 and record_of(lock)["owner"] == winners[0].owner,
          "the record names the winner, and only the winner believes it won")

# --- TOCTOU across processes, which is the incident's actual shape ---------
RACER = """
import json, sys, time
sys.path.insert(0, sys.argv[1])
from ag2_relay_client.singleton import PollerGuard
guard = PollerGuard(sys.argv[2])
start = float(sys.argv[3])
while time.time() < start:      # a shared wall clock is the starting gun
    pass
print(guard.claim())
"""
with tempfile.TemporaryDirectory() as tmp:
    lock = Path(tmp) / "poller.lock"
    start_at = time.time() + 1.5   # enough for eight interpreters to boot
    racers = [
        subprocess.Popen(
            [sys.executable, "-c", RACER, str(_bootstrap.ROOT), str(lock),
             str(start_at)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for _ in range(8)
    ]
    said = [proc.communicate(timeout=60)[0].strip() for proc in racers]
    check(said.count(HELD) == 1,
          f"eight separate processes racing on one wall clock produce exactly "
          f"one winner too ({said})")
    check(record_of(lock)["pid"] != os.getpid(),
          "and the record belongs to the process that won, not to this one")

# --- fail open: a guard that cannot be evaluated does not get a vote -------
with tempfile.TemporaryDirectory() as tmp:
    listener = Listening()
    logging.getLogger("ag2_relay_client").addHandler(listener)

    # The most literal I/O error there is: the path the guard needs is not a
    # file it can open.
    blocked = Path(tmp) / "poller.lock"
    blocked.mkdir()
    guard = PollerGuard(blocked)
    check(guard.claim() == DEGRADED,
          "a guard whose file cannot be opened degrades — it does not report a "
          "loss, because a non-answer is not a loss")
    check(guard.state == DEGRADED and guard.displaced is False,
          "and it is not displaced by its own I/O error")
    check(listener.warned("could not be evaluated"),
          "and it says so out loud: a lock that silently stopped working is "
          "indistinguishable from a lock that is working")

    # A platform without POSIX locking is the same answer for the same reason.
    saved, singleton.fcntl = singleton.fcntl, None
    try:
        check(PollerGuard(Path(tmp) / "elsewhere.lock").claim() == DEGRADED,
              "a platform with no fcntl has no atomic guard — so it polls, "
              "rather than pretending to a guarantee it cannot make")
    finally:
        singleton.fcntl = saved

    # An unreadable record is not evidence that anybody is polling.
    torn = Path(tmp) / "torn.lock"
    torn.write_text("{not json at all")
    check(PollerGuard(torn).claim() == HELD,
          "a corrupt guard record is treated as an empty guard, not as a live "
          "poller — in doubt, poll")
    ownerless = Path(tmp) / "ownerless.lock"
    ownerless.write_text(json.dumps({"pid": 1, "heartbeat_ts": time.time()}))
    check(PollerGuard(ownerless).claim() == HELD,
          "and so is a record that names no owner")

    # A record with no timestamp cannot claim freshness: freshness is the only
    # liveness test, and a claim that declines to make it does not get to fall
    # back on anything else.
    undated = Path(tmp) / "undated.lock"
    undated.write_text(json.dumps({"owner": "someone", "pid": os.getpid()}))
    check(PollerGuard(undated).claim() == HELD,
          "a record with no heartbeat is infinitely stale, whatever its pid")

    # A holder whose *renew* fails keeps polling: it wrote the record last, and
    # a claim that could not be checked has not been lost.
    working = PollerGuard(Path(tmp) / "working.lock")
    check(working.claim() == HELD, "a guard is taken normally")
    working.path = Path(tmp)  # every later open() is an I/O error
    check(working.claim() == DEGRADED and working.held is True,
          "a renew that cannot read the guard keeps polling — being wrong here "
          "costs a doubled message; being wrong the other way costs the user "
          "every answer")

    logging.getLogger("ag2_relay_client").removeHandler(listener)

# --- and now the same four properties, through the client ------------------
# Two clients, one bearer, one state dir: exactly one of them talks to the
# broker at all.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    poller = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    broker.on("POST", "/v1/results", json={"ok": True})
    poller.poll_once()
    check(poller.next_task(timeout=1) is not None, "the holder polls and delivers")
    check(poller.snapshot()["singleton"] == HELD, "and says it holds the bearer")

    broker.forget()
    other = client_for(broker, tmp, standby_recheck=7.0)
    delay = other.poll_once()
    check(broker.requests == [],
          "the second client on the same bearer makes no request at all — not "
          "a poll, not a heartbeat: every task it leased would be delivered "
          "twice, and the broker would never mention it")
    check(delay == 7.0, "it stands by, and asks again on its own")
    check(other.next_task(timeout=0.1) is None, "with nothing to hand anybody")
    snapshot = other.snapshot()
    check(snapshot["state"] == STANDBY and snapshot["singleton"] == LOST,
          "and says which of the two it is, in the file a supervisor reads")

    # The standby is a standby, not a corpse: when the holder releases, it
    # takes over by itself.
    poller.stop()
    check(other.poll_once() == 0.0 and broker.took("GET", "/v1/tasks"),
          "when the holder stops cleanly, the standby takes the bearer over "
          "with nobody intervening")

# A holder that is displaced stops polling immediately — on its own thread,
# mid-flight, which is the shape of the reaped-process incident.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    reaped = client_for(broker, tmp, idle_gap=0.02, singleton_stale_after=0.3)
    reaped.start()
    time.sleep(0.3)
    check(len(broker.took("GET", "/v1/tasks")) > 0, "the holder is polling")

    # Its record is now stale — it stamped once and the freshness window is
    # 0.3 s — so a replacement takes the bearer.
    replacement = client_for(broker, tmp, singleton_stale_after=0.3)
    check(replacement.poll_once() == 0.0, "a replacement takes the stale guard")

    stopped_at = None
    for _ in range(100):
        time.sleep(0.05)
        if reaped.snapshot()["state"] == DISPLACED:
            stopped_at = len(broker.took("GET", "/v1/tasks"))
            break
    check(stopped_at is not None,
          "and the displaced client notices on its next turn, without being told")
    time.sleep(0.3)
    check(stopped_at is not None
          and len(broker.took("GET", "/v1/tasks")) == stopped_at,
          "then stops polling immediately — a displaced poller that finishes "
          "its turn is the double delivery, arriving late")
    check(reaped.snapshot()["error"] and "another poller" in reaped.snapshot()["error"],
          "the status file says why it went quiet, in words")
    reaped.stop()

# A guard the client cannot evaluate must not cost a single task.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    listener = Listening()
    logging.getLogger("ag2_relay_client").addHandler(listener)
    broken = client_for(broker, tmp)
    broken.layout.singleton_path.mkdir(parents=True, exist_ok=True)

    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task("task-open")]})
    broker.on("POST", "/v1/results", json={"ok": True})
    check(broken.poll_once() == 0.0, "a client whose guard is broken still polls")
    task = broken.next_task(timeout=1)
    check(task is not None and task.id == "task-open",
          "and still delivers — a lock bug never silences task delivery")
    check(broken.snapshot()["singleton"] == DEGRADED,
          "while saying that it is polling unguarded, which is not the same "
          "operational situation as polling with the guard held")
    check(listener.warned("could not be evaluated"), "and logging it")
    logging.getLogger("ag2_relay_client").removeHandler(listener)

# Turning the guard off is allowed, and it is loud.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    listener = Listening()
    logging.getLogger("ag2_relay_client").addHandler(listener)
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    unguarded = client_for(broker, tmp, singleton=False)
    check(unguarded.poll_once() == 0.0, "a client with singleton=False polls")
    check(unguarded.snapshot()["singleton"] == "off", "and says the guard is off")
    check(listener.warned("no singleton guard"),
          "having warned that nothing then prevents a second poller — the wire "
          "will not tell anybody either")
    other = client_for(broker, tmp, singleton=False)
    check(other.poll_once() == 0.0,
          "which is exactly what that means: two pollers, one bearer")
    logging.getLogger("ag2_relay_client").removeHandler(listener)

print("\n" + ("PASS — singleton green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
