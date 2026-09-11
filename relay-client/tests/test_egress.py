"""The adversarial egress suite — the mitigation for a recorded regression.

The transport seam moves the egress allowlist *into* the process that holds the
bearer. Decider and actor used to be two programs; now they are one, and the
guarantee is self-policing. The spec records that honestly and says whoever
implements egress reads the paragraph first. This file is what the paragraph
asks for: every escape anyone has thought of, tried against the allowlist, one
check per attempt.

Read it as the threat model, not as coverage. Each block names what an attacker
(or a talked-into-it agent) would write, and asserts the file does not leave.

Run: python3 tests/test_egress.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import errno
import inspect
import os
import stat
import tempfile
from pathlib import Path

from ag2_relay_client import egress
from ag2_relay_client.egress import EgressAllowlist, EgressRefused

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def inapplicable(name, why):
    """A case this process cannot exercise, said out loud rather than skipped
    in silence — the line names the capability that is missing and where the
    case IS exercised, so a green run never overclaims."""
    print("  n/a  " + name + " — " + why)


def _probe_symlink():
    """Can this process create a symlink at all? On Windows that is a
    privilege (SeCreateSymbolicLinkPrivilege — held by an elevated process or
    under Developer Mode); everywhere else it is a given."""
    with tempfile.TemporaryDirectory() as probe:
        target = Path(probe) / "t"
        target.write_text("t")
        try:
            os.symlink(str(target), str(Path(probe) / "l"))
        except (OSError, NotImplementedError):
            return False
    return True


CAN_SYMLINK = _probe_symlink()
NEEDS_SYMLINK = ("creating a symlink needs a privilege this process does not "
                 "hold; exercised on POSIX, and on Windows whenever symlink "
                 "privilege is available")


def dirlink(target, link):
    """A link to a directory: a symlink where the process may create one, a
    junction on Windows where it may not. A junction resolves, follows and
    repoints exactly the way these tests need a directory symlink to, and
    needs no privilege — so the directory-link escapes are exercised on every
    Windows process, elevated or not."""
    if CAN_SYMLINK:
        os.symlink(str(target), str(link))
    else:
        import _winapi
        _winapi.CreateJunction(str(target), str(link))


def rm_dirlink(link):
    """Remove a directory link without touching what it points at."""
    try:
        os.remove(str(link))
    except OSError:
        # A Windows directory symlink or junction is unlinked as a directory.
        os.rmdir(str(link))


def refused(allowlist, path, name, base_dir=None):
    """Assert `path` does not leave, and return the reason for inspection."""
    try:
        approved = allowlist.open(path, base_dir=base_dir)
    except EgressRefused as exc:
        check(True, name)
        return exc.reason
    approved.close()
    check(False, name + "  <-- THE FILE WOULD HAVE LEFT")
    return ""


def allowed(allowlist, path, name, base_dir=None):
    try:
        with allowlist.open(path, base_dir=base_dir) as approved:
            check(approved.size >= 0, name)
            return approved
    except EgressRefused as exc:
        check(False, name + f" ({exc.reason})")
        return None


with tempfile.TemporaryDirectory() as tmp:
    top = Path(tmp).resolve()
    root = top / "allowed"
    (root / "sub").mkdir(parents=True)
    outside = top / "outside"
    outside.mkdir()

    good = root / "chart.png"
    good.write_bytes(b"PNG-ish")
    nested = root / "sub" / "notes.md"
    nested.write_text("notes")
    secret = outside / "id_rsa"
    secret.write_text("PRIVATE KEY")

    allowlist = EgressAllowlist([root])

    # --- the baseline: the allowlist is not simply refusing everything
    approved = allowed(allowlist, good, "a regular file inside a root is sendable")
    check(approved is not None and approved.name == "chart.png",
          "the approved descriptor names the file it judged")
    allowed(allowlist, nested, "a file in a subdirectory of a root is sendable")
    allowed(allowlist, str(good), "a path given as a string works the same way")

    # --- symlink pointing out of the root
    if CAN_SYMLINK:
        escape = root / "escape.txt"
        os.symlink(str(secret), str(escape))
        why = refused(allowlist, escape, "a symlink out of the root is refused")
        check(why == egress.OUTSIDE, "and refused for being outside, not for being odd")
    else:
        inapplicable("a symlink out of the root is refused", NEEDS_SYMLINK)

    # --- link *component* mid-path: the link is a directory on the way
    dirlink(outside, root / "elsewhere")
    refused(allowlist, root / "elsewhere" / "id_rsa",
            "a directory-link component mid-path is refused")

    # --- `..` traversal, in both the obvious and the buried spelling
    refused(allowlist, str(root) + "/../outside/id_rsa",
            "a `..` climbing straight out of the root is refused")
    refused(allowlist, str(root) + "/sub/../../outside/id_rsa",
            "a `..` buried mid-path is refused")
    refused(allowlist, str(root) + "/sub/../../allowed/../outside/id_rsa",
            "a `..` that re-enters and leaves again is refused")

    # --- the prefix look-alike: `/allowed-evil` starts with `/allowed`
    evil = top / "allowed-evil"
    evil.mkdir()
    (evil / "loot.txt").write_text("loot")
    why = refused(allowlist, evil / "loot.txt",
                  "a root-prefix look-alike directory is refused (/allowed-evil)")
    check(why == egress.OUTSIDE, "the separator is what refuses it, not luck")
    sibling = top / "allowedX"
    sibling.mkdir()
    (sibling / "loot.txt").write_text("loot")
    refused(allowlist, sibling / "loot.txt",
            "a one-character-longer sibling root name is refused (/allowedX)")

    # --- a symlink whose target is *inside* the root: allowed, on purpose.
    # The allowlist judges where a file is, not how it was named — and a notes
    # tree that is itself a symlink is a real deployment, not a hypothetical.
    if CAN_SYMLINK:
        inside_link = root / "alias.png"
        os.symlink(str(good), str(inside_link))
        allowed(allowlist, inside_link, "a symlink resolving inside the root is sendable")
    else:
        inapplicable("a symlink resolving inside the root is sendable", NEEDS_SYMLINK)

    # --- the hard link: the escape realpath structurally cannot see
    hard = root / "hard.txt"
    os.link(str(secret), str(hard))
    check(os.path.realpath(str(hard)) == str(hard),
          "a hard link's realpath is itself — no string check can catch it")
    why = refused(allowlist, hard, "a hard link to a file outside the root is refused")
    check(why == egress.MULTI_LINKED, "and refused by name: it has a second name")
    hard.unlink()

    # --- special files: not-a-regular-file, and a FIFO that must not stall
    if hasattr(os, "mkfifo"):
        fifo = root / "pipe"
        os.mkfifo(str(fifo))
        why = refused(allowlist, fifo, "a FIFO inside a root is refused (and does not hang)")
        check(why == egress.NOT_REGULAR, "refused for what it is, not for where it is")
    else:
        # No FIFO exists on Windows: named pipes live in the `\\.\pipe\`
        # namespace, not in any directory an allowlist could name, so there is
        # no in-tree special file to create. The Windows twin is the legacy
        # DOS device name, which is reachable *inside* a root — `outbox\NUL`
        # opens the NUL device on Win32 — and is refused below.
        inapplicable("a FIFO inside a root is refused (and does not hang)",
                     "os.mkfifo does not exist on Windows; the in-root device "
                     "case is covered by the DOS device name check instead")
        why = refused(allowlist, root / "NUL",
                      "a legacy DOS device name inside a root is refused "
                      "(the Windows spelling of a device where a file belongs)")
        check(why == egress.OUTSIDE,
              "and refused as outside: its canonical home is the device "
              "namespace, which no root contains")
    refused(allowlist, root, "the root directory itself is not a file to send")
    refused(allowlist, root / "sub", "a directory inside the root is refused")

    devices = EgressAllowlist(["/dev", root])
    refused(devices, "/dev/null",
            "a character device is refused even when /dev is an allowed root")
    refused(allowlist, "/dev/null", "and /dev/null is outside an ordinary root anyway")
    refused(allowlist, "/etc/passwd", "the classic target is outside every root")

    # --- nonexistent, empty, and unnameable paths
    refused(allowlist, root / "nope.txt", "a file that does not exist is refused")
    refused(allowlist, "", "an empty path is refused")
    refused(allowlist, None, "a missing path is refused")
    refused(allowlist, str(good) + "\x00.png", "a NUL in the path is refused, not a crash")
    refused(allowlist, "   ", "whitespace is not a path")

    # --- relative paths: refused without a base, judged by destination with one
    refused(allowlist, "chart.png", "a relative path with no base directory is refused")
    allowed(allowlist, "chart.png", "a relative path against an in-root base is sendable",
            base_dir=root)
    refused(allowlist, "id_rsa", "a relative path against an out-of-root base is refused",
            base_dir=outside)
    allowed(allowlist, "../allowed/chart.png",
            "a relative path that climbs INTO a root is sendable — the destination "
            "is what is judged", base_dir=outside)
    refused(allowlist, "../outside/id_rsa",
            "a relative path that climbs OUT of a root is refused", base_dir=root)

    # --- size
    fat = root / "fat.bin"
    fat.write_bytes(b"x" * 4096)
    small = EgressAllowlist([root], max_bytes=1024)
    why = refused(small, fat, "a file over the cap is refused")
    check("MB" in why, "and the refusal says how big it was")
    allowed(EgressAllowlist([root], max_bytes=0), fat,
            "max_bytes=0 removes the cap")

    # --- an empty allowlist refuses everything: the fail-closed reading of
    # "this consumer configured no roots"
    closed = EgressAllowlist([])
    why = refused(closed, good, "an allowlist with no roots refuses a file it could see")
    check(why == egress.NO_ROOTS, "and says so rather than saying 'outside'")

with tempfile.TemporaryDirectory() as tmp:
    # --- TOCTOU: the string check and the open are not the same instant.
    #
    # The check runs against a realpath taken a moment ago. This makes that
    # snapshot *lie* — realpath is patched to report the path unchanged — so the
    # boundary check passes on a path that is really a symlink out of the root.
    # What has to refuse it is the descriptor walk, which opens every component
    # with O_NOFOLLOW and gets ELOOP. If this test ever fails, the allowlist has
    # been reduced to a string comparison.
    top = Path(tmp).resolve()
    root = top / "allowed"
    root.mkdir()
    outside = top / "outside"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE KEY")
    allowlist = EgressAllowlist([root])
    real_realpath = egress.os.path.realpath

    if CAN_SYMLINK:
        swapped = root / "report.txt"
        os.symlink(str(outside / "id_rsa"), str(swapped))

        def lying_realpath(path, *args, **kwargs):
            # As if the file had been a plain file when it was measured, and
            # had become a symlink a microsecond later.
            if str(path) == str(swapped):
                return str(swapped)
            return real_realpath(path, *args, **kwargs)

        egress.os.path.realpath = lying_realpath
        try:
            why = refused(allowlist, swapped,
                          "a path swapped for a symlink after the check is refused")
            check(why == egress.SWAPPED,
                  "and refused by the descriptor walk, which is what actually holds")
        finally:
            egress.os.path.realpath = real_realpath
    else:
        # The mid-path swap below still runs — a junction needs no privilege —
        # so the TOCTOU hold is exercised on this process too, one level up.
        inapplicable("a path swapped for a symlink after the check is refused",
                     NEEDS_SYMLINK)

    # --- the same hold refuses a directory link swapped in *mid-path*. On
    # POSIX that is the `openat` walk reading ELOOP; on Windows — which has
    # neither `openat` nor `O_NOFOLLOW` — it is the opened handle being asked
    # what it really names. A junction works where symlink creation is a
    # privilege, so this case runs on every Windows process.
    root2 = top / "allowed2"
    (root2 / "reports").mkdir(parents=True)
    (root2 / "reports" / "q3.txt").write_text("fine")
    allowlist2 = EgressAllowlist([root2])
    allowed(allowlist2, root2 / "reports" / "q3.txt", "the honest path is sendable")
    # Now `reports` becomes a link to somewhere else, with a same-named file.
    decoy = top / "decoy"
    decoy.mkdir()
    (decoy / "q3.txt").write_text("PRIVATE KEY")
    import shutil
    shutil.rmtree(str(root2 / "reports"))
    dirlink(decoy, root2 / "reports")
    egress.os.path.realpath = lambda p, *a, **k: (
        str(root2 / "reports" / "q3.txt")
        if str(p) == str(root2 / "reports" / "q3.txt")
        else real_realpath(p, *a, **k)
    )
    try:
        why = refused(allowlist2, root2 / "reports" / "q3.txt",
                      "a directory component swapped for a link is refused")
        check(why == egress.SWAPPED,
              "refused as a swap — ELOOP on the POSIX walk, a handle that "
              "names the wrong file on Windows — not a truncated read")
    finally:
        egress.os.path.realpath = real_realpath

with tempfile.TemporaryDirectory() as tmp:
    # --- roots are fixed at construction, in every sense
    top = Path(tmp).resolve()
    root = top / "allowed"
    root.mkdir()
    (root / "ok.txt").write_text("ok")
    outside = top / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("loot")

    allowlist = EgressAllowlist([root])
    check(isinstance(allowlist.roots, tuple), "the roots are a tuple, not a list")
    check(not hasattr(allowlist, "add_root") and
          not hasattr(allowlist, "register_extra_roots"),
          "there is no method that widens an allowlist at runtime")

    widened = None
    try:
        allowlist.roots = (str(top),)  # type: ignore[misc]
    except AttributeError as exc:
        widened = exc
    check(widened is not None, "assigning `roots` is refused after construction")

    widened = None
    try:
        allowlist._roots = (str(top),)
    except AttributeError as exc:
        widened = exc
    check(widened is not None, "and so is assigning the private attribute")

    widened = None
    try:
        del allowlist._roots
    except AttributeError as exc:
        widened = exc
    check(widened is not None, "and so is deleting it")

    # The route PAST `__setattr__` rather than through it. `object.__setattr__`
    # cannot be closed in this language, so this is defence in depth and says so
    # — but `al.__dict__["_roots"] = (...)` is a one-liner an attacker trips
    # over, and `__slots__` is what there is no instance dict to write into.
    check(not hasattr(allowlist, "__dict__"),
          "the allowlist carries no instance dict at all")
    widened = None
    try:
        allowlist.__dict__["_roots"] = (str(top),)  # type: ignore[attr-defined]
    except (AttributeError, TypeError) as exc:
        widened = exc
    check(widened is not None,
          "so writing straight into it — the cheap way around __setattr__, which "
          "is never called on that path — has nowhere to land")

    refused(allowlist, outside / "loot.txt",
            "after all that, the outside file is still refused")

    # --- a root given as a directory link is resolved ONCE, at construction,
    # and does not follow a later repointing. (Roots that are links are a real
    # deployment: a notes tree living in a sync root.)
    real_root = top / "real"
    real_root.mkdir()
    (real_root / "note.md").write_text("note")
    link_root = top / "link"
    dirlink(real_root, link_root)
    by_link = EgressAllowlist([link_root])
    check(by_link.roots == (str(real_root),),
          "a linked root is stored resolved, so it names one directory for good")
    allowed(by_link, real_root / "note.md", "a file under the resolved root is sendable")
    rm_dirlink(link_root)
    dirlink(outside, link_root)
    refused(by_link, outside / "loot.txt",
            "repointing the root's link afterwards widens nothing")
    allowed(by_link, real_root / "note.md",
            "and the original root still works, unaffected")

    # --- the same attack one level up: a directory link that is an ANCESTOR of
    # the root. The whole chain above the root is spent at construction, so
    # there is no live link left in it to swap afterwards.
    holder = top / "holder"
    (holder / "notes").mkdir(parents=True)
    (holder / "notes" / "note.md").write_text("note")
    via = top / "via"
    dirlink(holder, via)
    through = EgressAllowlist([via / "notes"])
    check(through.roots == (str(holder / "notes"),),
          "a root reached through a linked ANCESTOR is stored fully resolved")
    allowed(through, holder / "notes" / "note.md",
            "and a file under it is sendable")
    decoy_holder = top / "decoy-holder"
    (decoy_holder / "notes").mkdir(parents=True)
    (decoy_holder / "notes" / "loot.txt").write_text("loot")
    rm_dirlink(via)
    dirlink(decoy_holder, via)
    refused(through, via / "notes" / "loot.txt",
            "repointing that ancestor afterwards reaches nothing")
    allowed(through, holder / "notes" / "note.md",
            "while the root it was built with keeps working")

    # --- duplicate and unusable roots do not confuse the resolution
    doubled = EgressAllowlist([root, root, "", None, str(root) + "/"])
    check(doubled.roots == (str(root),),
          "duplicate, empty and trailing-slash roots collapse to one")

    # --- two root shapes that resolve cleanly, look valid, and then refuse
    # EVERYTHING for ever. Both fail closed, which is the right direction and
    # the wrong moment: `sendable_roots` exists so a typo shows up at startup
    # rather than as a sentence in a room three days later, and it reported both
    # of these as perfectly good roots.
    check(EgressAllowlist(["/"]).roots == (),
          "`/` is not an allowlist, it is the absence of one — it is dropped, "
          "not honoured, so a consumer that typed it by accident is told")
    check(egress.sendable_roots(["/"]) == (),
          "and config validation sees it gone rather than sees it as valid")
    check(egress.sendable_roots(["/", root]) == (str(root),),
          "while the roots beside it survive")

    (top / "outbox").mkdir()
    (top / "outbox" / "chart.png").write_bytes(b"PNG-ish")
    if os.name == "nt":
        # Windows `realpath` reads the spelling off the disk itself
        # (GetFinalPathNameByHandle), so a wrong-case root arrives already
        # corrected — the failure this check exists for cannot be built here.
        # What is asserted instead is the correction: the stored root is the
        # on-disk spelling, so every child's real path starts with it.
        check(egress.sendable_roots([top / "OUTBOX"]) == (str(top / "outbox"),),
              "a root spelled in a case the filesystem does not use is stored "
              "in the on-disk spelling, so it matches its own children")
    else:
        check(egress.sendable_roots([top / "OUTBOX"]) == (),
              "a root spelled in a case the filesystem does not use is dropped: on a "
              "case-insensitive volume it resolves happily, no child's real path "
              "starts with it, and every upload is refused without ever saying why")
    check(egress.sendable_roots([top / "outbox"]) == (str(top / "outbox"),),
          "spelled the way the directory is, it is a root like any other")

with tempfile.TemporaryDirectory() as tmp:
    # --- the mount failing underneath us. An outbox on NFS or a cloud-sync
    # mount is the deployment the module docstring explicitly blesses, and such
    # a mount answers ESTALE mid-read or EIO on a stat whenever it likes. Those
    # have to leave here as `EgressRefused` like every other refusal: a caller
    # that must catch `EgressRefused` AND `OSError` will one day catch only one
    # of them, and I1 names what that costs — the bearer's only poller, over an
    # attachment.
    top = Path(tmp).resolve()
    root = top / "allowed"
    root.mkdir()
    target = root / "report.txt"
    target.write_text("the report")
    allowlist = EgressAllowlist([root])

    def stale_read(fd, size):
        raise OSError(errno.ESTALE, "Stale file handle")

    def broken_fstat(fd):
        raise OSError(errno.EIO, "Input/output error")

    real_read, egress.os.read = egress.os.read, stale_read
    raised = None
    try:
        approved = allowlist.open(target)
        try:
            approved.read(64)
        except BaseException as exc:  # noqa: BLE001 — the type is the assertion
            raised = exc
        approved.close()
    finally:
        egress.os.read = real_read
    check(isinstance(raised, EgressRefused),
          "a read that fails on the mount comes back as EgressRefused, not as a "
          "bare OSError (got " + type(raised).__name__ + ")")

    real_fstat, egress.os.fstat = egress.os.fstat, broken_fstat
    raised = None
    try:
        allowlist.open(target).close()
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    finally:
        egress.os.fstat = real_fstat
    check(isinstance(raised, EgressRefused),
          "and so does an fstat that fails on a descriptor whose mount went "
          "away (got " + type(raised).__name__ + ")")
    allowed(allowlist, target, "and the allowlist still works afterwards")


# --- the shape of the surface: no bytes go in anywhere.
#
# "No public or private code path uploads bytes that did not come from an
# allowlisted path" is a property of the *signatures*, so it is checked as one.
# A future overload taking `data=` or `content_b64=` would make every check
# above decorative, and this is what notices.
from ag2_relay_client import outbound, roomops  # noqa: E402

_BYTES_ISH = {
    "data", "bytes", "content", "content_b64", "data_b64", "blob", "buffer",
    "buf", "raw", "file_bytes", "body_bytes", "payload_bytes", "contents",
}
offenders = []
for module in (egress, roomops, outbound):
    for _, obj in vars(module).items():
        candidates = []
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            candidates = [obj]
        elif inspect.isclass(obj) and obj.__module__ == module.__name__:
            candidates = [m for _, m in inspect.getmembers(obj, inspect.isfunction)
                          if m.__module__ == module.__name__]
        for func in candidates:
            for parameter in inspect.signature(func).parameters:
                if parameter.lstrip("_").lower() in _BYTES_ISH:
                    offenders.append(f"{module.__name__}.{func.__qualname__}({parameter})")
check(not offenders,
      "no callable in egress/roomops/outbound takes bytes to send: " + repr(offenders))

source = Path(roomops.__file__).read_text()
check(source.count("b64encode") == 1,
      "exactly one place in the library encodes a file for upload")
check("ApprovedFile" in source.split("def _encode")[1].split("\n\n")[0],
      "and it encodes an ApprovedFile — a descriptor the allowlist judged")
for module in (egress, outbound):
    check("b64encode" not in Path(module.__file__).read_text(),
          f"{module.__name__} has no second encoder of its own")

print("\n" + ("PASS — egress green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
