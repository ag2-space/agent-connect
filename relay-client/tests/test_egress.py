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
    escape = root / "escape.txt"
    os.symlink(str(secret), str(escape))
    why = refused(allowlist, escape, "a symlink out of the root is refused")
    check(why == egress.OUTSIDE, "and refused for being outside, not for being odd")

    # --- symlink *component* mid-path: the link is a directory on the way
    os.symlink(str(outside), str(root / "elsewhere"))
    refused(allowlist, root / "elsewhere" / "id_rsa",
            "a symlink component mid-path is refused")

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
    inside_link = root / "alias.png"
    os.symlink(str(good), str(inside_link))
    allowed(allowlist, inside_link, "a symlink resolving inside the root is sendable")

    # --- the hard link: the escape realpath structurally cannot see
    hard = root / "hard.txt"
    os.link(str(secret), str(hard))
    check(os.path.realpath(str(hard)) == str(hard),
          "a hard link's realpath is itself — no string check can catch it")
    why = refused(allowlist, hard, "a hard link to a file outside the root is refused")
    check(why == egress.MULTI_LINKED, "and refused by name: it has a second name")
    hard.unlink()

    # --- special files: not-a-regular-file, and a FIFO that must not stall
    fifo = root / "pipe"
    os.mkfifo(str(fifo))
    why = refused(allowlist, fifo, "a FIFO inside a root is refused (and does not hang)")
    check(why == egress.NOT_REGULAR, "refused for what it is, not for where it is")
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
    swapped = root / "report.txt"
    os.symlink(str(outside / "id_rsa"), str(swapped))

    allowlist = EgressAllowlist([root])
    real_realpath = egress.os.path.realpath

    def lying_realpath(path, *args, **kwargs):
        # As if the file had been a plain file when it was measured, and had
        # become a symlink a microsecond later.
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

    # --- the same walk refuses a symlink swapped in *mid-path*
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
    os.symlink(str(decoy), str(root2 / "reports"))
    egress.os.path.realpath = lambda p, *a, **k: (
        str(root2 / "reports" / "q3.txt")
        if str(p) == str(root2 / "reports" / "q3.txt")
        else real_realpath(p, *a, **k)
    )
    try:
        why = refused(allowlist2, root2 / "reports" / "q3.txt",
                      "a directory component swapped for a symlink is refused")
        check(why == egress.SWAPPED, "ELOOP mid-walk, not a truncated read")
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
    refused(allowlist, outside / "loot.txt",
            "after all that, the outside file is still refused")

    # --- a root given as a symlink is resolved ONCE, at construction, and does
    # not follow a later repointing. (Roots that are symlinks are a real
    # deployment: a notes tree living in a sync root.)
    real_root = top / "real"
    real_root.mkdir()
    (real_root / "note.md").write_text("note")
    link_root = top / "link"
    os.symlink(str(real_root), str(link_root))
    by_link = EgressAllowlist([link_root])
    check(by_link.roots == (str(real_root),),
          "a symlinked root is stored resolved, so it names one directory for good")
    allowed(by_link, real_root / "note.md", "a file under the resolved root is sendable")
    os.remove(str(link_root))
    os.symlink(str(outside), str(link_root))
    refused(by_link, outside / "loot.txt",
            "repointing the root's symlink afterwards widens nothing")
    allowed(by_link, real_root / "note.md",
            "and the original root still works, unaffected")

    # --- duplicate and unusable roots do not confuse the resolution
    doubled = EgressAllowlist([root, root, "", None, str(root) + "/"])
    check(doubled.roots == (str(root),),
          "duplicate, empty and trailing-slash roots collapse to one")

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
