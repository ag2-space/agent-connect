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
import os
import tempfile
from pathlib import Path

from ag2_relay_client.journal import Journal, Reconciler
from ag2_relay_client.state import write_private_atomic

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def fresh(tmp, name="journal.jsonl", **kwargs):
    return Journal(Path(tmp) / name, **kwargs)


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

print("\n" + ("PASS — journal green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
