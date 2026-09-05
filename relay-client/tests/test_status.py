"""The connection-only status file, on its own (D2-D4).

The library writes this itself, rather than leaving observability to whoever
consumes it, because the failure it exists for is exactly the one where nobody
is watching: a client wedged for 21 hours with the UI saying "reconnecting" and
nothing on disk to read. A consumer that composes a richer file of its own reads
the same facts from the hook.

Run: python3 tests/test_status.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import json
import os
import stat
import tempfile
from pathlib import Path

from ag2_relay_client.state import write_private_atomic
from ag2_relay_client.status import (
    AUTH_WAIT,
    CONNECTED,
    RECONNECTING,
    StatusReporter,
)

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "connection-status.json"
    reporter = StatusReporter(path, gateway="https://u:p@gw.example/relay?token=t",
                              instance="prod")

    snapshot = reporter.update(CONNECTED, inflight=2)
    check(snapshot["connected"] is True and snapshot["state"] == CONNECTED,
          "a connected update says connected")
    check(snapshot["last_ok_ts"] > 0, "and stamps the last healthy round-trip")
    check(snapshot["inflight"] == 2, "carrying whatever the caller measured")
    check(json.loads(path.read_text()) == snapshot, "the file matches the snapshot")
    if os.name == "nt":
        # Windows privacy is an ACL property, not a POSIX mode-bit property;
        # chmod(0o600) maps only to the read-only attribute there, and this
        # library claims no ACL privacy (test_journal.py's precedent). What is
        # applicable is that the owner can keep updating the status file.
        check(bool(os.stat(path).st_mode & stat.S_IWRITE),
              "and stays owner-writable on Windows — the POSIX 0600 privacy "
              "claim is not made here (privacy is ACL-based)")
    else:
        check(oct(os.stat(path).st_mode & 0o777) == oct(0o600), "and is private")
    check(snapshot["gateway"] == "https://gw.example/relay",
          "the gateway is redacted before it is ever persisted (D3)")
    connected_at = snapshot["last_ok_ts"]

    failed = reporter.update(RECONNECTING, error="Timeout", backoff_s=8.0)
    check(failed["last_ok_ts"] == connected_at,
          "a failure preserves last_ok_ts — 'connected N seconds ago' is the "
          "number an operator needs, and the one a naive rewrite drops")
    check(failed["error"] == "Timeout" and failed["backoff_s"] == 8.0,
          "and records what went wrong and for how long")

    healthy = reporter.update(CONNECTED)
    check(healthy["error"] is None and healthy["backoff_s"] == 0.0,
          "one healthy round-trip clears the error and the backoff together")

    waiting = reporter.update(AUTH_WAIT, error="auth rejected HTTP 401")
    check(waiting["connected"] is False, "auth-wait is not connected")

    # A restart must not report "never connected" for a client that was
    # connected a second ago.
    restarted = StatusReporter(path, gateway="https://gw.example/relay")
    check(restarted.snapshot()["last_ok_ts"] == healthy["last_ok_ts"],
          "a new reporter on the same file inherits last_ok_ts")

    path.write_text("{not json")
    check(StatusReporter(path).snapshot()["last_ok_ts"] == 0.0,
          "an unreadable previous status is 'unknown', not a crash")

    # D4: the hook is a side channel, and side channels never block delivery.
    seen = []
    reporter.on_change(lambda snap: seen.append(snap["state"]))
    reporter.update(CONNECTED)
    check(seen == [CONNECTED], "the hook sees each change")

    def explode(_snapshot):
        raise RuntimeError("consumer bug")

    reporter.on_change(explode)
    check(reporter.update(RECONNECTING, error="x")["state"] == RECONNECTING,
          "a hook that raises does not take the update with it (D4)")

    # And neither does an unwritable file: the status write is best-effort by
    # contract, because it runs inside the poll iteration.
    #
    # The path here used to be `/proc/definitely/not/writable/status.json`,
    # which is a guess about the host: on a Mac the write does fail (read-only
    # APFS root), in a container running as root it may not, and the assertion —
    # that `update` returned CONNECTED — is satisfied either way. So the test
    # silently stopped testing the swallow. This makes the failure structural
    # (the file's own parent is a *file*, which no filesystem lets you write
    # under) and asserts that it really did fail, not only that nothing raised.
    blocker = Path(tmp) / "not-a-directory"
    blocker.write_text("in the way")
    unwritable = StatusReporter(blocker / "status.json")
    failed = None
    try:
        write_private_atomic(unwritable.path, "{}")
    except OSError as exc:
        failed = exc
    check(failed is not None,
          "the path chosen for this test really is unwritable — otherwise the "
          "assertion below passes without exercising anything")
    check(unwritable.update(CONNECTED)["state"] == CONNECTED,
          "a status write that fails is swallowed, never raised into the loop")
    check(not unwritable.path.exists(),
          "and nothing was written, which is what was swallowed")

print("\n" + ("PASS — status green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
