"""The names and paths this library is allowed to write down (J2, F8, D3).

Three grammars meet here, and each one exists because a drift in it produced a
bug: the instance name that namespaces every per-client file (J2 — first a
length mismatch, then `str.isalnum()` accepting a Unicode name the ASCII regex
rejected, both of which stranded results); the wire task id, which is untrusted
input that lands in journal paths (F8); and the URL redaction that runs before
anything with a gateway in it is persisted (D3).

Run: python3 tests/test_state.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import os
import stat
import tempfile
from pathlib import Path

from ag2_relay_client import state as state_module
from ag2_relay_client.state import (
    StateLayout,
    fsync_dir,
    redact_url,
    valid_instance_name,
    valid_wire_id,
    write_private_atomic,
)

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# --- J2: the instance-name grammar, ASCII [A-Za-z0-9_-]{1,32} and nothing else
GOOD_NAMES = ["default", "prod", "dev-2", "a_b", "A", "9", "x" * 32]
BAD_NAMES = ["", " ", "x" * 33, "é", "prod.dev", "a/b", "..", ".", "a b", "a~b",
             "pro\nd"]

for name in GOOD_NAMES:
    check(valid_instance_name(name), f"instance name accepted: {name!r}")
for name in BAD_NAMES:
    check(not valid_instance_name(name), f"instance name refused: {name!r}")

# --- J2: ONE validator. Every drift between the grammar and a consumer of it
# produced the same bug class, so the layout must refuse exactly what the
# validator refuses — not its own approximation of it.
with tempfile.TemporaryDirectory() as tmp:
    agreed = True
    for name in GOOD_NAMES + BAD_NAMES:
        try:
            StateLayout(tmp, name)
            layout_ok = True
        except ValueError:
            layout_ok = False
        if layout_ok != valid_instance_name(name):
            agreed = False
            print(f"    (disagreement on {name!r}: layout={layout_ok})")
    check(agreed, "StateLayout accepts exactly what valid_instance_name accepts")

# --- F8: wire ids are untrusted input; they land in journal paths
for tid in ["task-1755500000000", "a", "A.b_c-d", "x" * 64]:
    check(valid_wire_id(tid), f"wire id accepted: {tid!r}")
for tid in ["", ".", "..", "x" * 65, "a/b", "a~b", "a b", "../etc/passwd",
            "a\nb", "tâche"]:
    check(not valid_wire_id(tid), f"wire id refused: {tid!r}")

# --- J2: per-instance namespacing — two instances share no path at all
with tempfile.TemporaryDirectory() as tmp:
    prod = StateLayout(tmp, "prod")
    dev = StateLayout(tmp, "dev")
    prod_paths = {prod.root, prod.journal_path, prod.status_path, prod.singleton_path}
    dev_paths = {dev.root, dev.journal_path, dev.status_path, dev.singleton_path}
    check(len(prod_paths) == 4, "the layout's four paths are distinct")
    check(not (prod_paths & dev_paths), "two instances share no state path")
    check(all(str(p).startswith(str(prod.root)) for p in prod_paths),
          "every path stays under the instance root")
    check(prod.instance == "prod" and Path(prod.root).name == "prod",
          "the instance names its own directory")

    # A second layout for the same instance is the same layout — the journal a
    # restart reopens must be the one the previous run wrote.
    check(StateLayout(tmp, "prod").journal_path == prod.journal_path,
          "the same (dir, instance) always resolves to the same paths")

    prod.ensure()
    check(Path(prod.root).is_dir(), "ensure() creates the instance root")
    if os.name == "nt":
        # Windows privacy is an ACL property, not a POSIX mode-bit property,
        # and this library makes no ACL claim (see test_journal.py — the same
        # honesty, for the same reason). The POSIX 0700 assertion is
        # inapplicable here; what IS applicable is that ensure() left a
        # directory this client can go on using.
        check(bool(os.stat(prod.root).st_mode & stat.S_IWRITE),
              "the Windows instance root stays usable after ensure() — the "
              "POSIX 0700 privacy claim is not made here (privacy is ACL-based)")
    else:
        check(oct(os.stat(prod.root).st_mode & 0o777) == oct(0o700),
              "the instance root is private (it holds a journal and a lock)")
    prod.ensure()
    check(Path(prod.root).is_dir(), "ensure() is idempotent")

# --- D3: redact before persist — userinfo, query and fragment never land
check(redact_url("https://user:pass@chat.ag2.space/relay") ==
      "https://chat.ag2.space/relay", "userinfo is stripped")
check(redact_url("https://chat.ag2.space/relay?token=abc") ==
      "https://chat.ag2.space/relay", "query is stripped")
check(redact_url("https://chat.ag2.space/relay#frag") ==
      "https://chat.ag2.space/relay", "fragment is stripped")
check(redact_url("https://chat.ag2.space:8443/relay") ==
      "https://chat.ag2.space:8443/relay", "a non-default port survives")
check(redact_url("not a url") == "not a url", "a non-URL is passed through")
check(redact_url("") == "", "the empty string is not a crash")
check("pass" not in redact_url("https://user:pass@host/p?token=t#f"),
      "no secret survives any of the three carriers at once")

# Redaction FAILURE must degrade to writing nothing sensitive, never to writing
# the value it failed to redact. A port that is not a number and an unclosed
# IPv6 literal are the two shapes that break the parse — one raises reading
# `.port`, the other raises inside `urlsplit` itself.
bad_port = redact_url("https://user:pass@gw.example:notaport/relay?token=abc")
check("pass" not in bad_port and "token=abc" not in bad_port,
      "a URL with an unparseable port is still redacted, not passed through")
unparseable = redact_url("http://user:pass@[::1/relay?token=abc")
check("pass" not in unparseable and "token=abc" not in unparseable,
      "a URL that will not parse at all leaks neither userinfo nor query")

# --- A6: the rename is fsynced too, not only the bytes inside the file ------
# `os.replace` is atomic, which is what a concurrent reader needs; it is not
# durable, which is what a crash needs. Without the directory fsync a power cut
# after the rename can leave the entry still naming the old file — so the bytes
# were made safe and then the pointer to them was lost. E2 and F7 rest on the
# whole write surviving, not on half of it, and the journal's own docstring
# claimed the fsync was what F2's ack ordering rests on.
#
# A power cut cannot be run here, so this asserts the call is made: the fsync is
# taken on a *directory* descriptor, and only the parent of the file just
# renamed.
SYNCED = []
_saved_fsync = state_module.os.fsync
_saved_open = state_module.os.open


def _watch_open(path, flags, *rest):
    handle = _saved_open(path, flags, *rest)
    SYNCED.append((handle, str(path)))
    return handle


def _watch_fsync(handle):
    for opened, path in SYNCED:
        if opened == handle:
            SYNCED.append(("fsynced", path))
    return _saved_fsync(handle)


with tempfile.TemporaryDirectory() as tmp:
    target = Path(tmp) / "nested" / "journal.jsonl"
    state_module.os.open = _watch_open
    state_module.os.fsync = _watch_fsync
    try:
        write_private_atomic(target, "one line\n")
    finally:
        state_module.os.open = _saved_open
        state_module.os.fsync = _saved_fsync
    check(target.read_text() == "one line\n", "the write lands")
    if os.name == "nt":
        # The documented Windows contract for A6 is narrower, and this asserts
        # the contract that exists rather than the one POSIX has: `fsync_dir`
        # is best-effort by construction, Windows cannot open a directory
        # through `os.open` at all, and a write that already landed must not be
        # failed because the entry pointing at it could not be flushed (D4).
        # The *bytes* are still made durable — the temp file is fsynced before
        # the rename on every platform. Directory-rename durability is a POSIX
        # property this library does not claim on Windows.
        check(any(marker == "fsynced" and path != str(target.parent)
                  for marker, path in SYNCED
                  if not isinstance(marker, int)),
              "the bytes are fsynced before the rename on Windows too")
        check(("fsynced", str(target.parent)) not in SYNCED,
              "the directory fsync of A6 is inapplicable on Windows — it is "
              "documented best-effort, and no fsync on the parent occurs")
        survived = None
        try:
            fsync_dir(target.parent)
            survived = True
        except OSError:
            survived = False
        check(survived,
              "and asking for it directly degrades to a no-op, never an error "
              "that could fail a write that already landed")
    else:
        check(("fsynced", str(target.parent)) in SYNCED,
              "and the directory holding it is fsynced, so the rename survives a "
              "crash and not only the bytes do (A6)")

    # It is best-effort by contract: not every filesystem lets a directory be
    # opened for fsync, and a write that already landed must not be failed
    # because the entry pointing at it could not be flushed (D4).
    survived = None
    try:
        fsync_dir(Path(tmp) / "no-such-directory-at-all")
        survived = True
    except OSError:
        survived = False
    check(survived, "a directory that cannot be fsynced is not an error")

# --- an explicitly given `~` is a path the caller wrote, not a location guessed
home = StateLayout("~/state-dir-test", "prod")
check("~" not in str(home.root), "a leading ~ in the state dir is expanded")

print("\n" + ("PASS — state green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
