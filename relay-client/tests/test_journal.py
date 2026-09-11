"""The durable ledger: what survives a kill, and what is allowed to shrink.

The journal is the only thing standing between at-least-once delivery and
re-executed work. Its three properties are tested here without any HTTP at all,
because they are properties of the file: an id's state is durable the moment the
mutator returns (F2's ordering is a lie otherwise), a result is retained until
someone says the broker took it (F5), and the ledger can shrink (E3 — the
2026-07-09 sighting was 175 stranded ids inflating `inflight` until the broker's
presence sweep marked the agent unassignable).

Run: python3 tests/test_journal.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import json
import logging
import os
import subprocess
import stat
import sys
import tempfile
import time
from pathlib import Path

from ag2_relay_client import journal as journal_module
from ag2_relay_client import locking
from ag2_relay_client.journal import Journal, Reconciler
from ag2_relay_client.locking import acquire_exclusive, release_exclusive
from ag2_relay_client.state import write_private_atomic

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def fresh(tmp, name="journal.jsonl", **kwargs):
    return Journal(Path(tmp) / name, **kwargs)


class Listening(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []

    def emit(self, record):
        self.lines.append((record.levelno, record.getMessage()))

    def warned(self, needle):
        return any(level >= logging.WARNING and needle in text
                   for level, text in self.lines)


# --- durability: the fact is on disk before the mutator returns -------------
with tempfile.TemporaryDirectory() as tmp:
    journal = fresh(tmp)
    journal.accept("task-1", room="!room:ag2.space")

    reopened = fresh(tmp).load()
    check(reopened.is_accepted("task-1"), "an accepted id survives without a save() call")
    check(reopened.room_for("task-1") == "!room:ag2.space",
          "the task->room sidecar is captured at accept and persisted (F7)")
    check(reopened.inflight() == 1, "an accepted id counts as in flight (E2)")

    journal.record_result("task-1", {"id": "task-1", "body": "the answer"})
    reopened = fresh(tmp).load()
    check(reopened.is_pending("task-1"), "a recorded result survives a reopen")
    check(reopened.pending_results() == [("task-1", {"id": "task-1", "body": "the answer"})],
          "and comes back with the payload that will be POSTed")
    check(reopened.inflight() == 1,
          "an answer not yet POSTed is still in flight (E2: accepted-not-completed)")

    journal.retire("task-1")
    reopened = fresh(tmp).load()
    check(reopened.is_done("task-1"), "a retired id is done")
    check(reopened.pending_results() == [], "and owes nothing")
    check(reopened.inflight() == 0, "and is out of the in-flight count")
    check(reopened.room_for("task-1") == "",
          "a successful POST retires the sidecar with the id (F5, F7)")

# --- F5: only success retires. Nothing else may forget a result -------------
with tempfile.TemporaryDirectory() as tmp:
    journal = fresh(tmp)
    journal.accept("task-2", room="!r:x")
    journal.record_result("task-2", {"id": "task-2", "body": "answer"})
    # A redelivery of a task whose answer is already waiting must not reset it
    # to "accepted" — that would lose the answer and re-execute the work.
    journal.accept("task-2", room="!r:x")
    check(journal.is_pending("task-2"),
          "a redelivery does not undo a result already waiting to be POSTed")
    check(journal.pending_results()[0][1]["body"] == "answer",
          "and does not touch the answer")

# --- F3: done is dedup memory, and it is bounded ---------------------------
with tempfile.TemporaryDirectory() as tmp:
    journal = fresh(tmp, done_window=3)
    for n in range(6):
        wire_id = f"task-{n}"
        journal.accept(wire_id)
        journal.record_result(wire_id, {"id": wire_id, "body": "x"})
        journal.retire(wire_id)
    check(journal.done_ids() == ["task-3", "task-4", "task-5"],
          "the done window keeps the most recent ids and drops the oldest")
    check(len(fresh(tmp, done_window=3).load().done_ids()) == 3,
          "the trim is persisted, not just in memory")

    # Trimming must never reach anything still owed to the broker.
    journal.accept("task-live")
    for n in range(6, 12):
        wire_id = f"task-{n}"
        journal.accept(wire_id)
        journal.record_result(wire_id, {"id": wire_id, "body": "x"})
        journal.retire(wire_id)
    check(journal.is_accepted("task-live"),
          "the done window never trims an id that still owes an answer")

# --- the file itself: atomic, private, and readable after damage -----------
with tempfile.TemporaryDirectory() as tmp:
    journal = fresh(tmp)
    journal.accept("task-a", room="!r:x")
    journal.accept("task-b", room="!r:y")
    path = journal.path
    if os.name == "nt":
        # Windows privacy is an ACL property, not a POSIX mode-bit property;
        # chmod(0o600) maps only to the read-only file attribute there. Keep the
        # applicable standard-library contract: the owner can update the file.
        check(bool(os.stat(path).st_mode & stat.S_IWRITE),
              "the Windows journal remains owner-writable (privacy is ACL-based)")
    else:
        check(oct(os.stat(path).st_mode & 0o777) == oct(0o600),
              "the journal is private — it is the id ledger of one bearer")
    check(not [p for p in Path(tmp).iterdir() if p.name.endswith(".tmp")],
          "the temp file the atomic replace used does not survive it")

    lines = path.read_text().splitlines()
    check(len(lines) == 2 and all(json.loads(line)["id"] for line in lines),
          "one JSON record per line")

    # A damaged record loses one id's bookkeeping; it does not stop the client
    # from starting, which would be the worse failure.
    path.write_text(lines[0] + "\n{not json\n" + lines[1] + "\n")
    damaged = fresh(tmp).load()
    check(damaged.knows("task-a") and damaged.knows("task-b"),
          "an unreadable record is skipped, not fatal")

    # A journal that does not exist yet is an empty journal, not a crash.
    check(fresh(tmp, "never-written.jsonl").load().inflight() == 0,
          "a missing journal file loads as empty")

    write_private_atomic(Path(tmp) / "sub" / "deep.jsonl", "x\n")
    check((Path(tmp) / "sub" / "deep.jsonl").read_text() == "x\n",
          "the atomic write creates the directory it was pointed at")

# --- E3: the ledger must be able to shrink ---------------------------------
with tempfile.TemporaryDirectory() as tmp:
    journal = fresh(tmp)
    journal.accept("task-live")     # accepted by this run — the consumer has it
    journal.accept("task-stranded")  # inherited from a run that is gone
    reconciler = Reconciler(journal)
    live = {"task-live"}

    check(reconciler.reconcile(live) == [],
          "one sighting is not enough to drop an id — a result may be landing")
    check(journal.inflight() == 2, "so nothing is dropped on the first pass")
    check(reconciler.reconcile(live) == ["task-stranded"],
          "two consecutive sightings drop it (E3)")
    check(journal.inflight() == 1, "and the in-flight count goes down")
    check(journal.is_accepted("task-live"),
          "an id this run accepted is never dropped, however long it takes")

    # The guard is *consecutive*: an id that stops looking stranded resets.
    journal.accept("task-back")
    reconciler.reconcile({"task-live"})               # task-back is a suspect
    reconciler.reconcile({"task-live", "task-back"})  # ...and then is not
    check(reconciler.reconcile({"task-live", "task-back"}) == [],
          "a suspect that becomes live again is not dropped on the next pass")
    check(journal.is_accepted("task-back"), "and stays in the ledger")

    # A pending result is never stranded: it has an answer, and the drain owes
    # the broker that answer whatever else happens.
    journal.record_result("task-answered", {"id": "task-answered", "body": "x"})
    for _ in range(3):
        reconciler.reconcile(set())
    check(journal.is_pending("task-answered"),
          "an id with an answer waiting is never reconciled away")

# --- C1: two Journals on one file must not revert each other ---------------
# The whole-file rewrite is last-writer-wins by construction. Reproduced: a
# second Journal on the same path retires `task-B`, and the first one's next
# `record_result` writes the file back without it. J1 makes two pollers rare and
# not impossible — a standby exists by design, a takeover has both views live at
# once, and a consumer may answer from another process.
with tempfile.TemporaryDirectory() as tmp:
    first = fresh(tmp)
    first.accept("task-A")
    first.accept("task-B")

    second = fresh(tmp).load()
    second.record_result("task-B", {"id": "task-B", "body": "the other answer"})
    second.retire("task-B")
    check(second.is_done("task-B"), "the second process retires an id")

    first.record_result("task-A", {"id": "task-A", "body": "an answer"})
    on_disk = fresh(tmp).load()
    check(on_disk.is_done("task-B"),
          "and the first one's next write does not put it back — a write that "
          "knew nothing about an id must not destroy it (C1)")
    check(on_disk.is_pending("task-A"),
          "while the id the first one *did* change is written as it changed it")
    check(first.is_done("task-B"),
          "and the first one adopts the fact rather than carrying a stale view "
          "into its next write")

    # The other direction: an id created entirely by another process survives.
    third = fresh(tmp).load()
    third.accept("task-C")
    first.record_result("task-A", {"id": "task-A", "body": "revised"})
    check(fresh(tmp).load().is_accepted("task-C"),
          "an id another process created is adopted, not overwritten away")

# The same merge, with genuinely separate processes leaving the gate together.
JOURNAL_RACER = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ag2_relay_client.journal import Journal
journal = Journal(sys.argv[2]).load()
Path(sys.argv[4]).write_text("ready", encoding="utf-8")
gate = Path(sys.argv[5])
deadline = time.monotonic() + 15.0
while not gate.exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("parent did not release the Journal race gate")
    time.sleep(0.005)
if sys.argv[6] == "no-lock":
    import ag2_relay_client.locking as locking
    locking._BACKEND = None
    locking._BACKEND_LOAD_ERROR = RuntimeError("deliberately unavailable")
journal.accept(sys.argv[3])
"""


def run_process_journal_race(tmp, disable_lock=False, one_at_a_time=False):
    """Two writer processes against one journal, released by gate files.

    `one_at_a_time` releases writer A, waits for it to exit, then releases
    writer B. Both writers loaded the journal *before* either gate opened, so
    B's view is stale either way — what the sequencing removes is only the
    unrelated race of two `os.replace` calls landing on one destination in the
    same instant, which Windows answers with a sharing violation in whichever
    process loses. The lost update being demonstrated needs a stale view, not
    simultaneity.
    """
    path = Path(tmp) / "journal.jsonl"
    wire_ids = ("task-process-A", "task-process-B")
    if one_at_a_time:
        gates = [Path(tmp) / (wire_id + ".release") for wire_id in wire_ids]
    else:
        gates = [Path(tmp) / "release"] * len(wire_ids)
    ready_markers = [Path(tmp) / (wire_id + ".ready") for wire_id in wire_ids]
    writers = [
        subprocess.Popen([
            sys.executable, "-c", JOURNAL_RACER, str(_bootstrap.ROOT),
            str(path), wire_id, str(ready), str(gate),
            "no-lock" if disable_lock else "lock"
        ])
        for wire_id, ready, gate in zip(wire_ids, ready_markers, gates)
    ]
    deadline = time.monotonic() + 10.0
    while not all(marker.exists() for marker in ready_markers):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)
    both_ready = all(marker.exists() for marker in ready_markers)

    def finished(writer):
        try:
            return writer.wait(timeout=30)
        except subprocess.TimeoutExpired:
            writer.terminate()
            return writer.wait(timeout=5)

    codes = []
    if one_at_a_time:
        for writer, gate in zip(writers, gates):
            gate.write_text("go", encoding="utf-8")
            codes.append(finished(writer))
    else:
        gates[0].write_text("go", encoding="utf-8")
        codes = [finished(writer) for writer in writers]
    return both_ready, codes, fresh(tmp).load().inflight_ids()


with tempfile.TemporaryDirectory() as tmp:
    ready, codes, result = run_process_journal_race(tmp)
    check(ready and codes == [0, 0] and set(result) == {
        "task-process-A", "task-process-B"},
        f"two processes changing different Task ids preserve both facts ({result})")

# The negative control: the same stale views, with the merge lock disabled.
# Writer A lands completely before writer B is released — B's overwrite comes
# from the stale view it loaded before A ran, so the lost update is proved
# deterministically. Releasing both at once proved the same thing *most* runs,
# and on the others proved only that two unlocked `os.replace` calls on one
# Windows destination can collide (a sharing violation exits one child nonzero
# — reproduced at ~12% per pair under load). That collision is the lock's job
# to prevent and the locked positive control above shows it prevented; a
# negative control that depends on it is measuring the wrong property.
with tempfile.TemporaryDirectory() as tmp:
    ready, codes, result = run_process_journal_race(
        tmp, disable_lock=True, one_at_a_time=True)
    check(ready and codes == [0, 0] and result == ["task-process-B"],
          "without the merge lock, the second stale-view writer erases the "
          f"first one's fact ({result})")

# --- F8 on the way back in, not only on the way out ------------------------
# The journal is this library's own file, but "our own file" is not a trust
# boundary a state dir can carry: it syncs, it is restored, and anything running
# as the user can edit it. An id read back off disk goes straight onto the wire
# in a result POST and into a path component.
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "journal.jsonl"
    write_private_atomic(path, "".join(json.dumps(record) + "\n" for record in (
        {"id": "task-ok", "state": "accepted", "room": ""},
        {"id": "../../etc/passwd", "state": "pending",
         "result": {"id": "../../etc/passwd", "body": "x"}},
        {"id": "..", "state": "done"},
        {"id": "has spaces", "state": "accepted"},
    )))
    loaded = Journal(path).load()
    check([wire_id for wire_id, _ in loaded.pending_results()] == [],
          "an id that fails F8's grammar is not read back off disk and POSTed")
    check(loaded.inflight_ids() == ["task-ok"] and not loaded.done_ids(),
          "only ids this client would have been willing to write survive a load")

# --- lock fallback stays visible, bounded, fail-open, and leak-free --------
# The give-up path is the *expected* one under J1: a standby exists by design, a
# takeover has both views live at once, and a consumer may answer from somewhere
# else — so a contended write is ordinary, not exotic. It used to `yield False`
# with the lock file still open, which leaks one descriptor per contended write
# and ends in `EMFILE` on the poll thread: a client that can no longer open a
# socket, arrived at through the very condition the lock exists for.
def descriptor_count():
    if Path("/dev/fd").exists():
        return len(os.listdir("/dev/fd"))
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        count = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL
        if not kernel32.GetProcessHandleCount(
                kernel32.GetCurrentProcess(), ctypes.byref(count)):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(count.value)
    return None


with tempfile.TemporaryDirectory() as tmp:
    listener = Listening()
    logging.getLogger("ag2_relay_client").addHandler(listener)
    journal = fresh(tmp)
    rival = os.open(str(Path(tmp) / "journal.jsonl.lock"),
                    os.O_RDWR | os.O_CREAT, 0o600)
    acquire_exclusive(rival, time.monotonic() + 1.0)
    was, journal_module.LOCK_WAIT_S = journal_module.LOCK_WAIT_S, 0.01
    try:
        before = descriptor_count()
        started = time.monotonic()
        for index in range(20):
            journal.accept("task-%d" % index, room="!room:ag2.space")
        elapsed = time.monotonic() - started
        after = descriptor_count()
    finally:
        journal_module.LOCK_WAIT_S = was
        release_exclusive(rival)
        os.close(rival)
    if os.name == "nt":
        counts_available = before is not None and after is not None
        check(counts_available,
              "Windows handle counts are available around contended writes")
        counts_match = counts_available and after == before
    else:
        counts_match = before is None or after == before
    check(counts_match,
          "twenty writes made while another process holds the lock leak no "
          "descriptors — the give-up path closes the file it opened")
    check(elapsed < 1.0,
          f"journal contention stays bounded ({elapsed:.3f}s for twenty writes)")
    check(listener.warned("stayed held"),
          "a journal lock timeout makes the write-without-merge fallback visible")
    check(fresh(tmp).load().inflight() == 20,
          "and every one of them still landed: the lock is a merge, never a "
          "precondition (D4)")
    logging.getLogger("ag2_relay_client").removeHandler(listener)

with tempfile.TemporaryDirectory() as tmp:
    listener = Listening()
    logging.getLogger("ag2_relay_client").addHandler(listener)
    saved_backend = locking._BACKEND
    saved_error = locking._BACKEND_LOAD_ERROR
    locking._BACKEND = None
    locking._BACKEND_LOAD_ERROR = RuntimeError("deliberately unavailable")
    try:
        before = descriptor_count()
        journal = fresh(tmp)
        journal.accept("task-backend-fallback")
        after = descriptor_count()
    finally:
        locking._BACKEND = saved_backend
        locking._BACKEND_LOAD_ERROR = saved_error
    check(fresh(tmp).load().is_accepted("task-backend-fallback"),
          "backend failure still writes the Journal without a merge lock")
    check(listener.warned("backend failed"),
          "and the backend-failure fallback is visible in logs")
    if os.name == "nt":
        counts_available = before is not None and after is not None
        check(counts_available,
              "Windows handle counts are available around backend failure")
        counts_match = counts_available and after == before
    else:
        counts_match = before is None or after == before
    check(counts_match,
          "and backend failure closes the Journal lock descriptor")
    logging.getLogger("ag2_relay_client").removeHandler(listener)


print("\n" + ("PASS — journal green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
