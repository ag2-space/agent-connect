"""The durable half of task delivery: which ids this client owes an answer for.

The in-memory queue a consumer reads Tasks from is a *handoff*, not durability.
This file is the durability. It records three facts and nothing else:

- an id was **accepted** from the broker, and which room it came from (F7 — a
  media answer produced after a restart has no room to target without the
  sidecar, and "origin room unknown" means the file is simply not delivered);
- an accepted id now has a **result waiting to be POSTed** (F5 — a result is
  retained until `POST /v1/results` succeeds; a transient failure that dropped
  the result would lose the user's answer *and* leave the lease to expire into
  a redelivery);
- an id is **done** — its result was accepted by the broker (F3 — a redelivery
  of a done id is re-completed upstream, never re-executed; the reconnect
  replays of 2026-06-30 and 2026-07-01 were 500 historical tasks each).

`inflight` for the heartbeat (E2) is the count of the first two states: ids
accepted from the broker whose results have not yet been POSTed. It is a
broker-visible signal with scheduling consequences, so it must also be able to
go *down* — see `strand_candidates`/`drop` and E3.

**What is not written here.** No wire-received free text: not the task body, not
a display name, not an error string the broker sent. The id is a validated slug
(F8) and the room is a bounded, control-character-free string; that is the whole
of what the wire contributes to this file. That is deliberate, and it is what
makes G5 (redact pasted secrets before anything wire-received is persisted) a
requirement this file satisfies by having nothing to redact. The one free-text
value it does hold is the *outbound* result body, which the consumer wrote and
which lives here only for the seconds between `complete()` and a successful
POST — the spec leaves body persistence to the consumer, and this is the
smallest window that F5 admits.

The file is rewritten whole, temp + `os.replace`, on every change: a journal
that is half-written after a kill is worse than one that is a version behind,
and this file's whole purpose is to survive exactly that kill.

**Two processes, one file.** The whole-file rewrite is last-writer-wins by
construction, and that is not hypothetical: a second `Journal` on the same path
retiring one id, followed by the first one's next write, put the retired id
back. J1 makes two *pollers* rare and not impossible — a standby exists by
design, a takeover has both views live at once, and a consumer may answer from
somewhere else — so every write takes a cross-process lock on a sidecar and
re-reads the file underneath it first, keeping every id it did not itself
change. See `_merge_from_disk`.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .state import valid_wire_id, write_private_atomic

log = logging.getLogger(__name__)

try:  # POSIX advisory locking; see `_across_processes`.
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX
    fcntl = None  # type: ignore[assignment]

#: How long to wait for the journal's cross-process lock before writing without
#: it. Bounded because this runs inside the poll iteration and nothing in there
#: may block unboundedly (F1); short because the critical section is a small
#: read and a small write.
LOCK_WAIT_S = 1.0

#: Accepted from the broker; the consumer has it, or is about to.
ACCEPTED = "accepted"
#: The consumer answered; the answer is waiting for `POST /v1/results`.
PENDING = "pending"
#: The broker took the result. The id stays, as the dedup memory for F3.
DONE = "done"

#: How many completed ids to remember. The memory is what makes a reconnect
#: replay cheap — an id the broker re-serves after its result landed is
#: re-completed from here without re-executing anything — so the window has to
#: outlive a replay flood (the observed ones were ~500 tasks) by a wide margin.
#: It is bounded at all because the alternative is a file that grows forever on
#: a long-lived client; ids older than the window are re-executed if the broker
#: ever re-serves them, which is at-least-once behaving as documented.
DONE_WINDOW = 4096


class Journal:
    """One instance's accepted / pending / done ledger, on disk.

    Every mutator writes the whole file before returning, so a caller that got
    a return value knows the fact is durable. That is the ordering F2 depends
    on: the ack goes out *after* this, never before, because an ack-then-crash
    before durability leaves the broker showing "received" for work no
    surviving process knows about.
    """

    def __init__(self, path, done_window: int = DONE_WINDOW):
        self.path = Path(path)
        self.done_window = int(done_window)
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        #: The ids this instance changed since it last wrote the file. Emptied
        #: by every successful save, so it holds one mutation's worth — it is
        #: the merge rule's whole input. See `_merge_from_disk`.
        self._touched: set = set()
        #: The sidecar the cross-process lock is taken on. A sidecar and not the
        #: journal itself because `write_private_atomic` replaces the journal's
        #: inode, and a lock on an inode that gets swapped is not a lock at all
        #: — the same reason `singleton.py` writes its record in place.
        self._lock_path = self.path.parent / (self.path.name + ".lock")

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Journal {self.path} inflight={self.inflight()}>"

    # --- reading the file back ------------------------------------------

    def load(self) -> "Journal":
        """Read the journal a previous run left, tolerating a damaged line.

        A line that will not parse is dropped with a log rather than raising:
        the alternative is a client that cannot start because of one bad
        record, which is a worse failure than losing one id's bookkeeping.

        Every id is re-checked against F8's grammar on the way back in. The
        file is this library's own, but "our own file" is not a trust boundary
        a state dir can carry: it syncs, it is restored, and it is editable by
        anything running as the user. An id read back off disk goes straight
        onto the wire in a result POST and into a path component, which is the
        same traversal in both directions the intake check exists to stop, so
        it is checked in both directions too.
        """
        with self._lock:
            self._entries.clear()
            self._touched.clear()
            entries = self._read_file()
            if entries is not None:
                self._entries = entries
        return self

    def _read_file(self) -> "Optional[OrderedDict[str, Dict[str, Any]]]":
        """The file as entries, or `None` when there is nothing readable."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        damaged = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                damaged += 1
                continue
            if not isinstance(record, dict):
                damaged += 1
                continue
            wire_id = record.get("id")
            state = record.get("state")
            if (not isinstance(wire_id, str) or not valid_wire_id(wire_id)
                    or state not in (ACCEPTED, PENDING, DONE)):
                damaged += 1
                continue
            entries[wire_id] = record
        if damaged:
            log.warning("%s: skipped %d unreadable or unusable journal "
                        "record(s)", self.path, damaged)
        return entries

    # --- the three facts -------------------------------------------------

    def accept(self, wire_id: str, room: str = "") -> None:
        """Record an id as accepted, with the room it came from (E2, F7).

        Idempotent by design, and deliberately not a no-op on a re-accept: a
        task re-served after its lease expired arrives again with the same id,
        and the sidecar is refreshed from the copy the broker just served.
        """
        with self._lock:
            record = self._entries.get(wire_id)
            if record is not None and record.get("state") == PENDING:
                # A result is already waiting for this id. The redelivery does
                # not undo it — the drain will re-POST, and the broker dedups.
                return
            self._entries[wire_id] = {
                "id": wire_id, "state": ACCEPTED, "room": room or "",
                "ts": time.time(),
            }
            self._entries.move_to_end(wire_id)
            self._save(wire_id)

    def record_result(self, wire_id: str, payload: Dict) -> None:
        """Attach the answer to an accepted id, durably, before it is POSTed.

        This is the write the restart test turns on: a client killed between
        the consumer's `complete()` and a successful POST comes back, finds the
        answer here, and re-completes the lease — it does not hand the task to
        the consumer a second time.
        """
        with self._lock:
            record: Dict[str, Any] = (self._entries.get(wire_id)
                                       or {"id": wire_id, "room": ""})
            record["id"] = wire_id
            record["state"] = PENDING
            record["result"] = payload
            record.setdefault("room", "")
            record["ts"] = time.time()
            self._entries[wire_id] = record
            self._entries.move_to_end(wire_id)
            self._save(wire_id)

    def retire(self, wire_id: str) -> None:
        """The broker accepted the result: the id becomes dedup memory.

        The answer and the room sidecar go with the same write — a successful
        POST is the only thing that retires them (F5, F7).

        The write here is best-effort (see `_save_quietly`): this call runs
        inside the poll iteration, before `GET /v1/tasks`, and D4 says nothing
        in there may raise.
        """
        with self._lock:
            record: Dict[str, Any] = self._entries.get(wire_id) or {"id": wire_id}
            record["state"] = DONE
            record["ts"] = time.time()
            record.pop("result", None)
            record.pop("room", None)
            self._entries[wire_id] = record
            self._entries.move_to_end(wire_id)
            self._trim()
            self._save_quietly(wire_id)

    def drop(self, wire_id: str) -> None:
        """Forget an id entirely (E3: it can never complete through here).

        Best-effort for the same reason as `retire`: the reconciler that calls
        it runs from the heartbeat, which runs before the poll.
        """
        with self._lock:
            if self._entries.pop(wire_id, None) is not None:
                self._save_quietly(wire_id)

    # --- reading -----------------------------------------------------------

    def state_of(self, wire_id: str) -> str:
        with self._lock:
            record = self._entries.get(wire_id)
            return str(record.get("state")) if record else ""

    def is_done(self, wire_id: str) -> bool:
        return self.state_of(wire_id) == DONE

    def is_pending(self, wire_id: str) -> bool:
        return self.state_of(wire_id) == PENDING

    def is_accepted(self, wire_id: str) -> bool:
        return self.state_of(wire_id) == ACCEPTED

    def knows(self, wire_id: str) -> bool:
        with self._lock:
            return wire_id in self._entries

    def room_for(self, wire_id: str) -> str:
        """The room this task came from (F7). Empty when unknown."""
        with self._lock:
            record = self._entries.get(wire_id) or {}
            room = record.get("room")
            return room if isinstance(room, str) else ""

    def inflight_ids(self) -> List[str]:
        """Accepted-not-yet-POSTed, in acceptance order — the heartbeat's
        `inflight` (E2) and nothing else."""
        with self._lock:
            return [k for k, v in self._entries.items()
                    if v.get("state") in (ACCEPTED, PENDING)]

    def inflight(self) -> int:
        return len(self.inflight_ids())

    def accepted_ids(self) -> List[str]:
        with self._lock:
            return [k for k, v in self._entries.items() if v.get("state") == ACCEPTED]

    def pending_results(self) -> List[Tuple[str, Dict]]:
        """Every answer still owed to the broker, oldest first (F5)."""
        with self._lock:
            out = []
            for wire_id, record in self._entries.items():
                if record.get("state") != PENDING:
                    continue
                payload = record.get("result")
                if isinstance(payload, dict):
                    out.append((wire_id, dict(payload)))
            return out

    def done_ids(self) -> List[str]:
        with self._lock:
            return [k for k, v in self._entries.items() if v.get("state") == DONE]

    # --- persistence -------------------------------------------------------

    def _trim(self) -> None:
        """Keep the done window bounded; never touch anything still owed."""
        done = [k for k, v in self._entries.items() if v.get("state") == DONE]
        excess = len(done) - self.done_window
        for wire_id in done[:max(0, excess)]:
            self._entries.pop(wire_id, None)
            # A trim is this instance's own decision, so the merge must not
            # read the id straight back off disk on the way out.
            self._touched.add(wire_id)

    def _save(self, *touched: str) -> None:
        """Whole file, temp + atomic replace, fsynced, under a cross-process lock.

        Not an optimization target: this file is small (ids and one pending
        answer at a time), and every alternative — append-and-compact, an
        in-place rewrite — has a window where a kill leaves a record half
        written. The kill is the case this file exists for.

        Raises what the write raises. Callers whose fact can survive a lost
        write use `_save_quietly`; the two that cannot — `accept` and
        `record_result` — are the durability F2's ack ordering and F5's
        retention rest on, and a caller told "durable" has to be told the truth.
        """
        self._touched.update(touched)
        with _across_processes(self._lock_path) as locked:
            if locked:
                self._merge_from_disk()
            lines = "".join(
                json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                for record in self._entries.values()
            )
            write_private_atomic(self.path, lines)
        self._touched.clear()

    def _save_quietly(self, *touched: str) -> bool:
        """A save whose failure the caller survives (D4). True if it landed.

        `retire` and `drop`, and nothing else. Both record that an id needs
        *less* than the file already promises — the result was delivered, the id
        can never complete — so a write that does not land leaves the journal a
        version behind in the safe direction: the id is re-POSTed on a later
        pass and the broker dedups it (`200 {"duplicate": true}`).

        The scar: `write_private_atomic` can raise — ENOSPC, a read-only mount,
        a state dir removed under a running client — and `retire` runs inside
        `_drain_results`, which runs inside the poll iteration *before* `GET
        /v1/tasks`. Reproduced: the result POST succeeded and only the retire
        failed, so every later pass re-POSTed and re-raised at the same line;
        the loop backed off to its 60 s cap and never polled again, every
        in-flight lease expired, and the status file still said `reconnecting`.
        D4 is explicit that nothing before the GET may raise, and this was the
        one place it could.
        """
        try:
            self._save(*touched)
            return True
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.warning("could not write the journal at %s (%s) — the change is "
                        "held in memory and the file is a version behind, in "
                        "the direction that only costs a duplicate the broker "
                        "dedups. The poll loop is not interrupted (D4).",
                        self.path, exc)
            return False

    def _merge_from_disk(self) -> None:
        """Fold in what another process changed, before overwriting the file.

        `_save` used to serialise this instance's whole view over whatever was
        on disk, which is last-writer-wins across processes: reproduced by
        having a second `Journal` on the same file retire `task-B` and watching
        the first one's next `record_result` rewrite the file without it. B1 is
        the same bug with a bigger blast radius — a standby that takes the
        bearer over writes the journal it loaded at boot and erases everything
        the previous holder answered while it stood by.

        The merge rule is the smallest one that cannot lose a fact: an id *this*
        instance changed since its last write is ours to write, and every other
        id is read back from the file — adopted if it is new there, replaced if
        it moved on, forgotten if it is gone. It is not a general reconciliation
        and does not pretend to be; it removes the failure where a write that
        knew nothing about an id destroyed it.
        """
        on_disk = self._read_file()
        if on_disk is None:
            return  # no file yet, or nothing readable in it: ours stands
        for wire_id in list(self._entries):
            if wire_id in self._touched:
                continue
            theirs = on_disk.get(wire_id)
            if theirs is None:
                self._entries.pop(wire_id, None)
            else:
                self._entries[wire_id] = theirs
        for wire_id, record in on_disk.items():
            # `_touched` covers removals too — a `drop` or a `_trim` is this
            # instance saying an id should be gone, and reading it straight back
            # off the file would undo the write that is being made.
            if wire_id not in self._entries and wire_id not in self._touched:
                self._entries[wire_id] = record


@contextlib.contextmanager
def _across_processes(path: Path) -> Iterator[bool]:
    """Hold an exclusive lock over one read-merge-write. Yields whether it took.

    Whether, and not an exception, because the merge is a correctness
    improvement and not a precondition: a state dir on a filesystem with no
    locking, or a platform with no `fcntl`, must still be able to write its
    journal. Failing the write there would trade a rare cross-process clobber
    for a guaranteed local one (D4).

    `LOCK_NB` and a short spin rather than a blocking `flock`, for the reason
    every wait in this library is bounded: the poll loop is what keeps leases
    alive, and a call in it that can wait forever is how a client stops
    delivering without ever looking broken (F1).

    **One `finally` closes the descriptor, on every exit.** The give-up path
    used to `yield False` and return with the file still open, so every write
    made while another process held the lock leaked one descriptor — measured,
    twenty contended `accept()` calls cost twenty of them. It is exactly the
    shape J1 tolerates by design (a standby exists, a takeover has both views
    live at once), so the leak is *paced by the condition this lock exists for*,
    and it ends in `EMFILE` on the poll thread: not a lost journal write, a
    client that can no longer open a socket. `singleton.py`'s `_locked` had it
    right — its `os.close` is in a `finally` — and this is the same shape.
    """
    if fcntl is None:  # pragma: no cover — non-POSIX
        yield False
        return
    handle = None
    locked = False
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
            locked = _flock_within(handle, path)
        except OSError as exc:
            log.debug("no cross-process journal lock at %s (%s)", path, exc)
            locked = False
        yield locked
    finally:
        if handle is not None:
            if locked:
                try:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                except OSError:  # pragma: no cover — closing releases it anyway
                    pass
            os.close(handle)


def _flock_within(handle: int, path: Path) -> bool:
    """Take the exclusive lock inside `LOCK_WAIT_S`, or say it could not be."""
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                log.warning("the journal lock at %s stayed held for %ss — "
                            "writing without it; a concurrent writer's "
                            "change to another id may be lost", path,
                            LOCK_WAIT_S)
                return False
            time.sleep(0.005)


class Reconciler:
    """E3: the in-flight ledger must be able to shrink.

    An id stays in the journal until its result is POSTed, which is right until
    the answer can never arrive — and then it is wrong in a way that is visible
    to the broker. Stranded ids inflate `inflight` monotonically until the
    presence sweep marks the agent unassignable (2026-07-09: 175 stranded ids,
    none with any pending work). `inflight` is a scheduling input, not local
    bookkeeping.

    In this library an id can never complete when it was accepted by a *previous*
    run: the Task object that carried it lived in the queue, in memory, and the
    process that held it is gone. Ids this run accepted are left alone however
    long the consumer takes — the library cannot tell a slow Turn from an
    abandoned one, and guessing wrong drops a live answer. A consumer that knows
    it will never answer says so with `reject`.

    Two consecutive sightings are required before a drop, exactly as sparrow
    does it: a result landing between the check and the discard is then picked
    up on the next pass instead of being raced.
    """

    def __init__(self, journal: Journal):
        self.journal = journal
        self._suspects: set = set()

    def reconcile(self, live_ids) -> List[str]:
        """Drop what can never complete; return the ids dropped this pass."""
        live = set(live_ids)
        stranded = {wire_id for wire_id in self.journal.accepted_ids()
                    if wire_id not in live}
        confirmed = stranded & self._suspects
        for wire_id in sorted(confirmed):
            self.journal.drop(wire_id)
            log.warning(
                "dropped stranded in-flight id %s — accepted by an earlier run, "
                "no answer can arrive for it here (E3)", wire_id)
        self._suspects = stranded - confirmed
        return sorted(confirmed)
