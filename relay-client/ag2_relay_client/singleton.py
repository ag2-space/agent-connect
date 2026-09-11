"""One bearer, one poller — and the wire will never tell you there are two (J1).

A bearer's queue tolerates exactly one concurrent poller. The broker does not
detect a second one and does not reject it: two pollers simply split the lease
stream, and **every task is delivered twice**. So the guarantee cannot come from
the wire; it has to come from whoever holds the bearer. It lives here, inside
the library, so that every consumer — the Worker today, a file shim later —
inherits it without having to remember it.

Two incidents wrote this file (2026-07-16). One was an orphaned bridge from a
*prior* install, reparented to init, that outlived its parent by **days** and
kept polling alongside the live one; owners saw every message answered twice.
The other was a replacement process and a desktop respawn starting at the same
instant — two pollers on one bearer, from a race measured in milliseconds.

Four properties make a guard against those two incidents correct. They are the
requirement, and each one is here for a reason that cost somebody a day:

- **Atomic acquire.** The simultaneous-start race must be able to produce only
  one winner, so every decision is made under an exclusive lock on the guard
  file — read, judge and write in one critical section. A read-then-write with
  a gap in the middle is the second incident, reimplemented.

- **Liveness is heartbeat freshness, never pid-alive.** The observed ghost was
  *alive but stale*: `kill(pid, 0)` said yes for days about a process that had
  stopped doing anything useful. Pid recycling makes it worse — the answer can
  be yes about an entirely different process. So the only question this file
  ever asks is "when did the holder last say it was alive?", and a holder that
  stopped saying so loses the guard, however alive its pid may be. There is no
  `os.kill` in this module, deliberately; the pid in the record is a diagnostic
  for a human reading the file and is never an input to a decision.

- **A definitive loser stops polling immediately.** Not at the next restart, not
  when convenient: the moment the guard says the claim belongs to someone else,
  the loop stops. The scar is the reaped process whose replacement had already
  started — both pulling from the same queue while the first one finished what
  it thought was its turn.

- **Fail open.** A guard that cannot be evaluated — an I/O error, a filesystem
  without locking, a bug in this file — answers `DEGRADED`, which means *poll
  anyway*. This is the load-bearing one. A lock is a delivery blocker by
  construction, and a client silenced by its own lock is a worse outage than the
  duplicate delivery the lock exists to prevent: the dual-poller risk is the
  pre-existing condition, and being wrong about it costs a doubled message,
  while being wrong the other way costs the user every answer. When in doubt,
  poll.

**How the file is written, and why not the way everything else here is.** The
guard record is updated *in place* on the locked descriptor rather than through
`write_private_atomic`. The journal and the status file are replaced by rename,
which swaps the inode underneath — and an inode swap is exactly what breaks a
lock file: two processes end up holding exclusive locks on two different inodes,
each convinced it is alone. For the same reason `release()` empties the file
instead of unlinking it. Nothing here is fsynced either: this record's value is
its *freshness*, and a record that survived a power cut describes a process that
did not.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .locking import acquire_exclusive, release_exclusive

log = logging.getLogger(__name__)

#: This client holds the bearer's guard and may poll.
HELD = "held"
#: Another poller holds it. Definitive: this client must not poll.
LOST = "lost"
#: The guard could not be evaluated. **Poll anyway** — see the fail-open rule
#: above. Never confuse this with `LOST`: a non-answer is not a loss.
DEGRADED = "degraded"
#: Never claimed, or released. Not a verdict `claim()` returns.
IDLE = "idle"

#: How old a holder's last "I am alive" may be before another client may take
#: the guard from it.
#:
#: The arithmetic, because it is not a taste question — and because the number
#: this replaces was arrived at by leaving most of the turn out. The old note
#: counted "one long poll (35 s) after a full backoff delay (60 s)" and called
#: it ~95 s, but `_drain_results` (one result timeout *per owed result*) and
#: `_heartbeat` both run before the poll on every turn. Measured against the
#: real loop: 125.0 s steady with one owed result and the backoff at its cap,
#: and 120.9 s with no failure at all — eight answers and a slow `/v1/results`
#: — at which point a standby took the guard from a holder whose thread was
#: alive throughout, and the holder's next `_renew` saw a foreign owner and
#: went DISPLACED permanently. A live holder losing the guard is the exact
#: inverse of this file's purpose, and it is a J1 fail-*closed*.
#:
#: Two things changed, so this window no longer has to bound a whole turn. The
#: loop now bounds itself (a wall-clock budget on the result drain and on the
#: acks a batch makes), and it re-stamps this record at every phase boundary
#: and through the wait between turns (`RelayClient._keep_guard_fresh`). What
#: has to fit inside the window is therefore the longest single call the loop
#: can be *inside*, not the sum of the calls it makes:
#:
#:     the long poll's socket timeout   35 s   (wait 25 + margin 10; the
#:                                              longest blocking call there is)
#:   + the refresh throttle below       20 s   (a stamp skipped a moment before
#:                                              that call began)
#:   + one missed stamp                 35 s   (the guard file transiently
#:                                              unlockable — DEGRADED writes
#:                                              nothing — so the next chance is
#:                                              a phase later)
#:   ------------------------------------------
#:                                      90 s
#:
#: 150 leaves that two thirds of headroom for a slow disk, a stepped clock and
#: a filesystem that made the stamp slower than the poll. The cost is stated
#: plainly: it is what an unclean kill costs before a replacement can take
#: over. A clean `stop()` releases and costs nothing.
STALE_AFTER_S = 150.0

#: How often the holder rewrites its own record. The claim is checked every turn
#: — that is how a loss is noticed immediately — but the *write* is throttled to
#: this, because a fast loop (a test, an instant broker) would otherwise rewrite
#: the file thousands of times to say the same thing. Never allowed to exceed a
#: third of the freshness window; see `PollerGuard.__init__`.
REFRESH_S = 20.0

#: How far ahead of this client's clock a holder's stamp may be and still be
#: read as a liveness claim. Two processes share a wall clock but never a
#: perfect one — a few seconds of NTP slew, or two hosts writing one synced
#: state dir — and that much is ordinary. Anything further ahead is not a
#: liveness claim at all; see `PollerGuard.acquire`.
FUTURE_SKEW_S = 5.0

#: How long to wait for the lock itself before giving up and degrading. The
#: critical sections here are a read and a small write, so contention resolves
#: in microseconds; this bound exists because nothing in the poll loop may block
#: unboundedly (F1), not because waiting a second is ever expected.
LOCK_WAIT_S = 1.0


class PollerGuard:
    """The singleton-per-bearer guard over one state dir's lock file.

        guard = PollerGuard(layout.singleton_path)
        if guard.claim() != LOST:
            ...poll...
        guard.release()

    One object per client. `claim()` acquires on the first call and renews on
    every later one, so a caller can simply ask it once per turn of its loop;
    the verdict it returns is the whole answer.

    A guard that loses a claim it *held* is displaced permanently: it answers
    `LOST` from then on without touching the disk. That is deliberate. The
    incident behind it is a reaped process and its replacement both polling, and
    a guard that quietly re-acquired after being taken from it would reproduce
    exactly that, plus flapping. Coming back is a decision for whoever owns the
    process — `release()` clears it, so a consumer that deliberately stops and
    starts again re-enters the arbitration honestly — and never a decision the
    loop makes on its own.
    """

    def __init__(
        self,
        path,
        stale_after: float = STALE_AFTER_S,
        refresh_interval: float = REFRESH_S,
        lock_wait: float = LOCK_WAIT_S,
    ):
        self.path = Path(path)
        self.stale_after = float(stale_after)
        #: Never longer than a third of the window this guard is judged by,
        #: whatever the caller asked for. A holder that re-stamps less often
        #: than it is aged out is guaranteed to lose its own guard for being
        #: punctual — B2 in miniature — and the shape is easy to reach by
        #: accident, because `stale_after` is a constructor knob and this is
        #: not.
        self.refresh_interval = min(float(refresh_interval), self.stale_after / 3.0)
        self.lock_wait = float(lock_wait)
        #: What identifies *this* client in the record. Random per object, not
        #: per pid: a pid is neither unique over time nor unique enough within
        #: one — two clients in one process are two pollers.
        self.owner = uuid.uuid4().hex
        self._held = False
        self._displaced = False
        self._state = IDLE
        self._last_write = 0.0
        self._last_logged: Optional[str] = None

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<PollerGuard {self._state} {self.path}>"

    @property
    def state(self) -> str:
        """The last verdict, or `IDLE` before the first claim."""
        return self._state

    @property
    def held(self) -> bool:
        """True while this client is the poller of record."""
        return self._held

    @property
    def displaced(self) -> bool:
        """True once a claim this client *held* was taken by another poller.

        The distinction matters to the caller: a client that never held the
        guard is a standby and may keep asking, while a displaced one has to
        stop. Both are `LOST`; only this tells them apart.
        """
        return self._displaced

    # --- the one call a loop needs ----------------------------------------

    def claim(self) -> str:
        """Acquire the guard, or confirm this client still has it.

        Returns `HELD`, `LOST` or `DEGRADED`. `DEGRADED` means *poll anyway*.
        """
        if self._displaced:
            return LOST
        return self._renew() if self._held else self.acquire()

    def touch(self) -> str:
        """Say "the loop is still turning" between two of its bounded calls.

        Exactly `claim()`, under a name that says what the caller means. It is
        not a second liveness channel and must never become one: there is no
        thread in this module and nothing stamps on a timer, so a stamp only
        ever happens where the poll loop itself reached, and a loop hung inside
        any one call reaches no stamp and ages out exactly as it should. That
        is the property the whole guard rests on.

        It exists because stamping once per turn quietly made the freshness
        window a bound on the *whole* turn rather than on the longest call in
        it — see `STALE_AFTER_S`. The verdict is the caller's to ignore here:
        it is decided once, at the top of the turn.
        """
        return self.claim()

    def acquire(self) -> str:
        """Take the guard if nobody live holds it."""
        if self._displaced:
            return LOST
        try:
            with self._locked() as handle:
                record = _read_record(handle, self.path)
                now = time.time()
                if record is None:
                    # No record, or one no reader could make sense of. An
                    # unreadable claim is not evidence that anybody is polling,
                    # and the fail-open rule says what to do with a non-answer.
                    return self._take(handle, now, "the guard was free")
                owner = record.get("owner")
                if owner == self.owner:  # our own record, from a previous run
                    return self._take(handle, now, "the guard was already ours")
                age = now - _stamp(record)
                if age < -FUTURE_SKEW_S:
                    # A stamp from the future is a clock, not a liveness claim,
                    # and freshness — the only test this file makes — cannot
                    # age it out. Unbounded below, `age` stayed negative
                    # forever: measured, a record one hour ahead answered LOST,
                    # one year ahead answered LOST, and a JSON `Infinity` (which
                    # `json.loads` accepts) answered LOST. None of those is
                    # exotic on a desktop — an RTC ahead of NTP at boot, a VM
                    # or snapshot restore, a state dir synced or restored from
                    # another machine, a laptop resume — and every one of them
                    # silenced every other client on the bearer indefinitely,
                    # with no error anywhere to fail open on. J1 is explicit
                    # that a lock bug must never be what silences delivery, so
                    # this is the fail-OPEN branch: take the guard.
                    return self._take(
                        handle, now,
                        "the record is stamped %.0fs in the FUTURE (pid %s on "
                        "%s) — a clock, not a liveness claim, and freshness "
                        "cannot arbitrate against it. Taking the guard rather "
                        "than standing by forever; if two pollers result, that "
                        "is the risk J1 chooses over a silenced bearer"
                        % (-age, record.get("pid"), record.get("host")))
                if age < self.stale_after:
                    return self._verdict(LOST, _standby_line(record, age))
                return self._take(
                    handle, now,
                    "the previous holder (pid %s on %s) last said it was alive "
                    "%.0fs ago, past the %.0fs freshness window — taking the "
                    "guard from it" % (record.get("pid"), record.get("host"),
                                       age, self.stale_after))
        except Exception as exc:  # noqa: BLE001 — fail open, always
            return self._degrade(exc)

    def release(self) -> None:
        """Give the guard up, so a restart does not wait out the staleness.

        Only ever clears *this* client's record: another poller's claim is left
        exactly where it is, whatever this object believes about itself. Never
        raises — a release that fails costs a replacement one staleness window,
        and nothing else.
        """
        try:
            with self._locked() as handle:
                record = _read_record(handle, self.path)
                if record is not None and record.get("owner") == self.owner:
                    # Emptied, not unlinked. Unlinking the inode other processes
                    # are about to lock is how a lock file stops being a lock:
                    # they would each hold an exclusive lock on a different
                    # inode and each conclude they were alone.
                    os.ftruncate(handle, 0)
        except Exception as exc:  # noqa: BLE001 — D4: never a blocker
            log.info("could not release the poller guard (%s); a replacement "
                     "waits out the %.0fs freshness window instead",
                     _describe(exc), self.stale_after)
        self._held = False
        self._displaced = False
        self._state = IDLE
        self._last_logged = None

    def snapshot(self) -> Dict[str, Any]:
        """What this guard believes, as data — for status and for incidents."""
        return {
            "state": self._state,
            "owner": self.owner,
            "held": self._held,
            "displaced": self._displaced,
            "path": str(self.path),
        }

    # --- internals ---------------------------------------------------------

    def _renew(self) -> str:
        """Confirm the claim, and re-stamp it if it is time.

        The read happens every turn even when the write does not: noticing a
        loss *immediately* is the third property, and a throttled read would
        delay it by up to a refresh interval.
        """
        try:
            with self._locked() as handle:
                record = _read_record(handle, self.path)
                now = time.time()
                if record is None:
                    # The file was emptied or wiped under us — a cleared state
                    # dir, someone else's stale release. Nobody is claiming it,
                    # so this is not a loss: write ours back and keep polling.
                    return self._take(handle, now, "the guard record was gone")
                if record.get("owner") != self.owner:
                    self._held = False
                    self._displaced = True
                    return self._verdict(LOST, _displaced_line(record))
                if now - self._last_write >= self.refresh_interval:
                    self._write(handle, {**record, "heartbeat_ts": now}, now)
                return self._verdict(HELD, None)
        except Exception as exc:  # noqa: BLE001 — fail open, always
            # Still held: a claim that could not be *checked* has not been lost,
            # and this client is the one that last wrote the record.
            return self._degrade(exc)

    def _take(self, handle: int, now: float, why: str) -> str:
        self._write(handle, {
            "owner": self.owner,
            # Diagnostics only, both of them. Nothing in this module reads the
            # pid back — see the module docstring on why pid-alive is not a
            # liveness test — and the host is here because a state dir that
            # turns out to be shared between machines is invisible otherwise.
            "pid": os.getpid(),
            "host": _hostname(),
            "acquired_ts": now,
            "heartbeat_ts": now,
        }, now)
        self._held = True
        return self._verdict(HELD, why)

    def _write(self, handle: int, record: Dict[str, Any], now: float) -> None:
        """Replace the record on the locked descriptor, in place.

        In place, and not through `write_private_atomic`, because a rename
        swaps the inode and the lock is on the inode — see the module
        docstring. Readers never see a torn record regardless: every reader
        takes the same lock first.
        """
        payload = json.dumps(record, sort_keys=True).encode("utf-8")
        os.lseek(handle, 0, os.SEEK_SET)
        # `os.write` is allowed to write fewer bytes than it was given, and its
        # return value used to be thrown away — a short write followed by an
        # `ftruncate` to the *intended* length leaves NUL padding inside the
        # JSON, which every reader then treats as an unparseable record, which
        # reads as a free guard. Fail-open, so it never silenced anything; it is
        # still a claim quietly evaporating, and it is one line.
        written = 0
        while written < len(payload):
            step = os.write(handle, payload[written:])
            if step <= 0:  # pragma: no cover — POSIX raises instead
                raise OSError(f"short write to the guard record at {self.path}")
            written += step
        os.ftruncate(handle, written)
        self._last_write = now

    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        """The critical section: open, lock exclusively, and never for long.

        Everything the guard decides happens in here, because a decision made
        outside it is a read-then-write with a gap in the middle, and that gap
        is the simultaneous-start incident.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            acquire_exclusive(
                handle, time.monotonic() + max(0.0, self.lock_wait))
            try:
                yield handle
            finally:
                release_exclusive(handle)
        finally:
            os.close(handle)

    def _verdict(self, verdict: str, why: Optional[str]) -> str:
        """Record a verdict, and say it out loud once per change.

        Once per change, because this runs every turn of the poll loop and a
        line per turn is a log nobody reads — but a *silent* guard is how the
        orphaned bridge went unnoticed for days.
        """
        self._state = verdict
        line = f"{verdict}:{why or ''}"
        if line != self._last_logged:
            self._last_logged = line
            if verdict == HELD and why:
                log.info("poller guard held for %s — %s", self.path, why)
            elif verdict == LOST:
                log.warning("poller guard: %s", why)
        return verdict

    def _degrade(self, exc: BaseException) -> str:
        """A guard that could not be evaluated is a guard that does not vote."""
        self._state = DEGRADED
        line = f"{DEGRADED}:{type(exc).__name__}"
        if line != self._last_logged:
            self._last_logged = line
            log.warning(
                "the singleton guard at %s could not be evaluated (%s) — "
                "polling anyway. A lock that cannot be read is not a reason to "
                "stop delivering tasks; the risk it leaves is the dual poller "
                "it was already there to catch.", self.path, _describe(exc))
        return DEGRADED


def _read_record(handle: int, path: Path) -> Optional[Dict[str, Any]]:
    """The record on the locked descriptor, or `None` for "nobody holds this".

    Empty is nobody (that is what `release` leaves behind). Unparseable is also
    nobody: a record no reader can make sense of is not evidence that anything
    is polling, and reading it as a live claim would be the fail-closed answer.
    """
    os.lseek(handle, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(handle, 4096)
        if not chunk:
            break
        chunks.append(chunk)
        if len(chunks) > 16:  # a guard record is ~150 bytes; this is not one
            break
    raw = b"".join(chunks)
    if not raw.strip():
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 — see the docstring
        log.warning("the guard record in %s is not readable JSON — treating it "
                    "as an empty guard rather than as a live poller", path)
        return None
    if not isinstance(record, dict) or not isinstance(record.get("owner"), str):
        log.warning("the guard record in %s names no owner — treating it as an "
                    "empty guard", path)
        return None
    return record


def _stamp(record: Dict[str, Any]) -> float:
    """The holder's last "I am alive", or 0 — which reads as infinitely stale.

    A record with no usable timestamp cannot claim freshness: freshness is the
    *only* liveness test here, and a claim that declines to make it does not get
    to fall back on being a live pid.

    The clock is the wall clock, because it is the only one two processes share.
    A stepped clock can cost one takeover; a monotonic clock would cost every
    comparison, since another process's monotonic reading means nothing here.

    Non-finite values are read as no timestamp at all. `json.loads` accepts
    `Infinity` and `NaN` and hands back floats for both, and an infinite stamp
    made `now - stamp` infinitely negative — a claim no clock could ever age
    out. `NaN` is worse: every comparison against it is False, so the record
    would have fallen through every branch.
    """
    value = record.get("heartbeat_ts")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def _standby_line(record: Dict[str, Any], age: float) -> str:
    line = ("another poller (pid %s on %s) holds this bearer and said it was "
            "alive %.0fs ago — standing by rather than double-delivering every "
            "task" % (record.get("pid"), record.get("host"), age))
    if record.get("host") not in (None, _hostname()):
        line += ("; that record was written on a different host, which a state "
                 "dir shared between machines cannot arbitrate")
    return line


def _displaced_line(record: Dict[str, Any]) -> str:
    return ("this client's claim on the bearer was taken by another poller "
            "(pid %s on %s) — polling stops now. Two pollers on one bearer "
            "double-deliver every task, and the wire does not detect it."
            % (record.get("pid"), record.get("host")))


def _hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:  # noqa: BLE001 — a diagnostic is never a blocker
        return "?"


def _describe(exc: BaseException) -> str:
    """An exception as one log-safe line."""
    return f"{type(exc).__name__}: {exc}".replace("\n", " ")[:300]
