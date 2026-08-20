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
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .state import write_private_atomic

log = logging.getLogger(__name__)

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

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Journal {self.path} inflight={self.inflight()}>"

    # --- reading the file back ------------------------------------------

    def load(self) -> "Journal":
        """Read the journal a previous run left, tolerating a damaged line.

        A line that will not parse is dropped with a log rather than raising:
        the alternative is a client that cannot start because of one bad
        record, which is a worse failure than losing one id's bookkeeping.
        """
        with self._lock:
            self._entries.clear()
            try:
                text = self.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return self
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
                if not isinstance(wire_id, str) or state not in (ACCEPTED, PENDING, DONE):
                    damaged += 1
                    continue
                self._entries[wire_id] = record
            if damaged:
                log.warning("%s: skipped %d unreadable journal record(s)",
                            self.path, damaged)
        return self

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
            self._save()

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
            self._save()

    def retire(self, wire_id: str) -> None:
        """The broker accepted the result: the id becomes dedup memory.

        The answer and the room sidecar go with the same write — a successful
        POST is the only thing that retires them (F5, F7).
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
            self._save()

    def drop(self, wire_id: str) -> None:
        """Forget an id entirely (E3: it can never complete through here)."""
        with self._lock:
            if self._entries.pop(wire_id, None) is not None:
                self._save()

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

    def _save(self) -> None:
        """Whole file, temp + atomic replace, fsynced.

        Not an optimization target: this file is small (ids and one pending
        answer at a time), and every alternative — append-and-compact, an
        in-place rewrite — has a window where a kill leaves a record half
        written. The kill is the case this file exists for.
        """
        lines = "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in self._entries.values()
        )
        write_private_atomic(self.path, lines)


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
