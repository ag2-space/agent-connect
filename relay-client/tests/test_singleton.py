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
from ag2_relay_client.client import NO_SEND, RelayClient
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


def wire_task(wire_id="task-1", **over):
    task = {"id": wire_id, "task": "do it", "channel_id": "!room:ag2.space",
            "user_id": "@alice:ag2.space", "access_tier": "owner"}
    task.update(over)
    return task


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

    # B3: and neither does a stamp from the FUTURE. `age = now - stamp` was
    # never bounded below, so a negative age was always under the window and
    # every other client on the bearer answered LOST for as long as the clock
    # was wrong — with no error anywhere to fail open on. Realistic on a
    # desktop: an RTC ahead of NTP at boot, a VM or snapshot restore, a state
    # dir synced or restored from another machine, a laptop resume.
    for label, stamp in (("an hour", time.time() + 3600),
                         ("a year", time.time() + 365 * 86400),
                         ("infinitely", float("inf"))):
        ahead = Path(tmp) / f"ahead-{label.replace(' ', '-')}.lock"
        ahead.write_text(json.dumps(
            {"owner": "somebody", "pid": 1, "heartbeat_ts": stamp}))
        check(PollerGuard(ahead).claim() == HELD,
              f"a record stamped {label} ahead does not silence this bearer "
              f"forever — freshness cannot arbitrate against a clock, so J1's "
              f"fail-open rule takes the guard (B3)")
    # `json.loads` accepts `Infinity` and `NaN` as bare tokens, and both used to
    # reach the arithmetic as floats.
    for token in ("Infinity", "NaN"):
        odd = Path(tmp) / f"{token}.lock"
        odd.write_text('{"owner": "somebody", "pid": 1, "heartbeat_ts": %s}' % token)
        check(PollerGuard(odd).claim() == HELD,
              f"and neither does a JSON {token} stamp, which json.loads accepts")

    # A stamp a couple of seconds ahead is ordinary clock skew between two
    # processes and is still a live claim: the tolerance is not a licence to
    # take the guard from anybody whose clock is a hair fast.
    skewed = Path(tmp) / "skewed.lock"
    skewed.write_text(json.dumps(
        {"owner": "somebody", "pid": 1, "heartbeat_ts": time.time() + 1.0}))
    check(PollerGuard(skewed).claim() == LOST,
          "while a stamp a second ahead is ordinary skew, and keeps its claim")

    # B8: `os.write` may write fewer bytes than it was handed, and the return
    # value used to be dropped — a short write plus an ftruncate to the intended
    # length leaves NUL padding inside the JSON, which reads as a free guard.
    written = (_bootstrap.ROOT / "ag2_relay_client" / "singleton.py").read_text()
    check("os.write(handle" not in written.replace("step = os.write(handle", ""),
          "the guard's write checks how much of the record actually landed")

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
    # B9: a standby is not a retry. The recheck interval used to be written into
    # `backoff_s`, whose own constant says "It is not backoff — nothing failed",
    # so a supervisor could not tell a client that is failing from a pair of
    # clients arbitrating correctly.
    check(snapshot["backoff_s"] == 0.0 and snapshot["recheck_s"] == 7.0,
          "and says it in its own field: nothing failed, so nothing is backing "
          "off (B9)")

    # B5: of the four calls a consumer can make straight onto the wire, the
    # heartbeat is the one a standby must not make. It announces this client as
    # the bearer's live worker and carries an `inflight` the broker's presence
    # sweep schedules against — two clients announcing that about one bearer is
    # the split brain the guard exists to prevent.
    broker.forget()
    check(other.heartbeat() is False, "a standby refuses to heartbeat (B5, J1)")
    check(broker.requests == [], "and sends nothing at all")
    # `complete` and `reject` stay open on purpose: they deliver an answer for a
    # lease this client genuinely holds, and F5 plus J1's fail-open rule agree
    # that losing a user's answer to a lock is the worse outcome. `healthz`
    # leases nothing, claims nothing and marks no presence.
    broker.on("GET", "/v1/healthz", json={"status": "ok", "healthy": True})
    check(other.healthz()["healthy"] is True,
          "while healthz stays open — it leases nothing and claims nothing")

    # The standby is a standby, not a corpse: when the holder releases, it
    # takes over by itself.
    poller.stop()
    check(other.poll_once() == 0.0 and broker.took("GET", "/v1/tasks"),
          "when the holder stops cleanly, the standby takes the bearer over "
          "with nobody intervening")

# A holder that is displaced stops polling immediately — on its own thread,
# mid-flight, which is the shape of the reaped-process incident.
#
# The holder here is made to *hang*, which is the incident's actual shape and
# not what this scenario used to do: it used to rely on the guard's refresh
# throttle being longer than the freshness window, so a perfectly live client
# went stale for being punctual. That is B2 in miniature, and the guard now
# refuses to be configured that way — `refresh_interval` is capped at a third of
# `stale_after` — so a hung holder has to actually hang. A broker that takes
# 1.2 s to answer the long poll does it: the loop is inside one bounded call,
# reaches no stamp, and ages out exactly as J1 wants.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("GET", "/v1/tasks", json={"tasks": []}, delay=1.2)
    reaped = client_for(broker, tmp, idle_gap=0.02, singleton_stale_after=0.3)
    reaped.start()
    time.sleep(0.6)
    check(len(broker.requests) > 0, "the holder is polling, and is inside a poll")

    replacement = client_for(broker, tmp, singleton_stale_after=0.3)
    check(replacement.poll_once() == 0.0,
          "a replacement takes the guard from a holder that has stopped saying "
          "it is alive — pid-alive is not liveness, and this one's pid is fine")

    stopped_at = None
    for _ in range(200):
        time.sleep(0.05)
        if reaped.snapshot()["state"] == DISPLACED:
            stopped_at = len(broker.took("GET", "/v1/tasks"))
            break
    check(stopped_at is not None,
          "and the displaced client notices on its next turn, without being told")
    time.sleep(0.5)
    check(stopped_at is not None
          and len(broker.took("GET", "/v1/tasks")) == stopped_at,
          "then stops polling immediately — a displaced poller that finishes "
          "its turn is the double delivery, arriving late")
    check(reaped.snapshot()["error"] and "another poller" in reaped.snapshot()["error"],
          "the status file says why it went quiet, in words")

    # B4: the documented way back. DISPLACED stops the loop from inside `_run`,
    # which used to leave `_thread` pointing at a dead thread — so `start()`,
    # which is what the README means by "coming back is the consumer's
    # decision", raised `RuntimeError: this client is already started`. Only
    # `stop()` cleared it, and nothing said you had to call it first.
    # The replacement is the live holder now, and it stays live: a window it
    # cannot go stale inside, and one fresh stamp before the question is asked.
    reaped.guard.stale_after = replacement.guard.stale_after = 30.0
    replacement.poll_once()
    restarted = None
    try:
        reaped.start()
    except RuntimeError as exc:
        restarted = exc
    check(restarted is None,
          "a displaced client can be started again without stop() first (B4)")
    check(reaped.guard.displaced is False,
          "and it re-enters the arbitration honestly rather than re-acquiring "
          "on its own — which would be the same incident with extra steps")
    for _ in range(60):
        time.sleep(0.05)
        if reaped.snapshot()["state"] == STANDBY:
            break
    check(reaped.snapshot()["state"] == STANDBY,
          "so it stands by behind the live holder, which is the whole point: "
          "coming back must not mean polling alongside whoever took over")
    reaped.stop()

# B2: a live holder must never lose the guard, and the turn has to be bounded
# for that to be true. The measured failures were 125.0 s (one owed result, the
# backoff at its cap) and 120.9 s with no failure at all — against a 120 s
# window. Two things fixed it: the loop bounds its own turn, and it re-stamps
# the record wherever it demonstrably got to instead of once per turn.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    # A result POST that takes far longer than the whole freshness window. This
    # is the shape of the real one: `add_result` does the room's outbound send
    # inside the request, so a large reply outlasts the timeout.
    broker.on("POST", "/v1/results", json={"ok": True}, delay=0.9)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    holder = client_for(broker, tmp, singleton_stale_after=0.6)
    holder.poll_once()
    holder.next_task(timeout=1)

    rival = PollerGuard(holder.layout.singleton_path, stale_after=0.6)
    broker.on("GET", "/v1/tasks", json={"tasks": []}, delay=0.5)
    started = time.monotonic()
    holder.complete("task-1", "an answer that takes a while to hand over")
    holder.poll_once()
    turn = time.monotonic() - started
    check(turn > 0.6,
          f"the turn ran longer than the whole freshness window ({turn:.2f}s of "
          f"0.60s) — which is the case that displaced a live holder")
    check(rival.claim() == LOST,
          "and the holder still has the guard: it stamped at the phase "
          "boundaries it passed, so the window bounds the longest single call "
          "and not the sum of them (B2, J1)")
    check(holder.guard.state == HELD and holder.guard.displaced is False,
          "so a client that was alive the whole time is not displaced")

# B2's other half: the wait between turns is chunked, so a client idling out a
# backoff is not mistaken for a hung one. A 60s backoff cap under a 150s window
# was the largest single term in the arithmetic, and it bought nothing — a loop
# that is genuinely hung never reaches the wait at all.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    waiting = client_for(broker, tmp, singleton_stale_after=0.6)
    waiting.guard.claim()
    waiting.guard.refresh_interval = 0.0
    rival = PollerGuard(waiting.layout.singleton_path, stale_after=0.6)
    stamped = record_of(waiting.layout.singleton_path)["heartbeat_ts"]
    waiting._wait(0.9)
    check(record_of(waiting.layout.singleton_path)["heartbeat_ts"] > stamped,
          "the guard is re-stamped through a wait longer than the window")
    check(rival.claim() == LOST, "so an idling holder keeps its bearer")

# B6: `stop()` releases only after the loop has actually left. A join that timed
# out used to release anyway — emptying the record while the poll thread was
# still inside `GET /v1/tasks`, so another client polled alongside it, which is
# the precise thing the release-after-join ordering exists to prevent.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("GET", "/v1/tasks", json={"tasks": []}, delay=1.0)
    slow = client_for(broker, tmp, idle_gap=0.01)
    slow.start()
    time.sleep(0.3)
    slow.stop(timeout=0.05)     # shorter than the poll it is inside
    record = record_of(slow.layout.singleton_path) if \
        slow.layout.singleton_path.read_text().strip() else None
    check(record is not None and record.get("owner") == slow.guard.owner,
          "a stop() whose join timed out leaves the guard held rather than "
          "handing the bearer to a second poller while this one may still be "
          "inside a poll (B6)")
    time.sleep(1.2)             # let the thread finish and leave

# ...and a clean stop, where the join really joined, does release.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    quick = client_for(broker, tmp, idle_gap=0.01)
    quick.start()
    time.sleep(0.2)
    quick.stop()
    check(quick.layout.singleton_path.read_text().strip() == "",
          "a clean stop still releases, so a restart does not wait out a "
          "freshness window it does not need")

# B1: a standby that takes over must re-read the journal. Nothing on the
# STANDBY -> HELD edge used to, so a fresh holder answered an already-answered
# task a second time (F3 defeated) and its first accept rewrote the file from
# stale memory, erasing the previous holder's `done` ledger. The longer it had
# stood by, the more history it reverted.
with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    holder = client_for(broker, tmp)
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task()]})
    broker.on("POST", "/v1/results", json={"ok": True})
    holder.poll_once()

    # The standby boots now — while `task-1` is merely accepted — and then
    # stands by for as long as the holder lives. Everything the holder does to
    # the journal from here is invisible to it until something re-reads.
    standby = client_for(broker, tmp, standby_recheck=0.01)
    check(standby.poll_once() == 0.01, "the standby stands by")
    check(standby.journal.done_ids() == [],
          "with the journal as it was at its boot: nothing answered yet")

    holder.next_task(timeout=1)
    holder.complete("task-1", "the holder's answer")
    check(holder.journal.is_done("task-1"), "the holder answers and retires it")

    holder.stop()               # a clean handover: the guard is released
    broker.forget()
    broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
    broker.on("POST", "/v1/heartbeat", json={"ok": True})
    broker.on("POST", "/v1/results", json={"ok": True, "duplicate": True})
    broker.on("GET", "/v1/tasks", json={"tasks": [wire_task(attempt=2)]})
    check(standby.poll_once() == 0.0, "the standby takes the bearer over")

    check(standby.next_task(timeout=0.2) is None,
          "and does NOT hand the already-answered task to its consumer — the "
          "journal is re-read on the takeover, so F3 still has a memory to "
          "check against (B1)")
    replayed = broker.took("POST", "/v1/results")
    check(len(replayed) == 1 and replayed[0].json["body"] == NO_SEND,
          "it re-completes the lease upstream instead")
    check(standby.journal.is_done("task-1"),
          "and the `done` ledger the previous holder built survives — it used "
          "to be erased by the new holder's first write")
    on_disk = holder.layout.journal_path.read_text()
    check("task-1" in on_disk and '"done"' in on_disk,
          "on disk too, which is where the next restart reads it from")

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
