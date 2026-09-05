"""A file on its way off this machine, and the only door it may leave by.

## Read this first — the regression this module carries

Until this library existed, the egress allowlist was enforced by a *different
process* than the one that held the token: the Worker staged a file into an
airlock directory and sparrow, separately, decided whether anything in that
directory could be uploaded. Decider and actor were not the same program, which
is a real property — a bug in the Worker could not, on its own, put an arbitrary
file in a room.

That property is what the transport seam spends. The check now runs in the same
process as the bearer, and the guarantee is self-policing: Permission
Policy-grade, not Sandbox-grade. It always *was* Permission Policy-grade, since
the Worker has always held the token — but a bug that opens egress can now live
here. Everything below is written for that reader. The adversarial suite in
`tests/test_egress.py` exists because of this paragraph, and a change to this
module that makes one of those tests pass "differently" is a change to the
security posture of every consumer of this library.

## The shape of the check

**Paths only. There is no bytes API in this library, public or private** — a
surface that accepted bytes would make this allowlist decorative, because the
caller could read anything and hand it over. The only way to a room is
`EgressAllowlist.open(path)`, and what it returns is a descriptor it has already
judged.

- **Roots are fixed at construction.** No `add_root`, no `register_extra_roots`,
  no attribute to reassign — the object refuses writes after `__init__`. A
  process whose allowlist can be widened at runtime has an allowlist an attacker
  only has to reach once.
- **Roots are `realpath`'d once, at construction.** A root may legitimately *be*
  a symlink (a notes tree pointing into a sync root, say); resolving it once,
  under our control, is what lets that work without letting a root be repointed
  later to somewhere else.
- **The boundary check is `real == root or real.startswith(root + os.sep)`.**
  The separator is the whole point: without it `/allowed-evil` is a prefix match
  for the root `/allowed`, and a look-alike directory name is an exfiltration
  route.
- **The descriptor is the authority, not the string.** After the string check
  passes, the file is opened by walking the path one component at a time from
  the root with `O_NOFOLLOW` on every step (`openat`, via `dir_fd`). A symlink
  swapped in between the check and the open — the classic TOCTOU — raises
  `ELOOP` on the walk and is refused. What the caller then reads is the
  descriptor that was judged, not a path that could since have become something
  else.
- **`fstat` on that descriptor decides the rest**: a regular file, within the
  size cap, with exactly one name.

That last clause is the one worth naming. **A hard link is the escape
`realpath` structurally cannot see**: a second name inside an allowed root for
an inode that lives outside it collapses to itself, so every string check in the
world says yes. Refusing `st_nlink > 1` closes it. The cost is that a
legitimately hard-linked file is refused with a sentence in the room, which is
the direction this module fails in on purpose.

`st_nlink` closes the hard-link route; it is not an enumeration of every way one
inode gets a second real path. A bind mount, an overlay mount or a second mount
of the same filesystem gives a file a path inside an allowed root while
`st_nlink` stays `1`, and nothing here can see that — the link count is a
property of the inode, and the second name is a property of the mount table.
Read the clause as "a second *name on this filesystem* is refused", not as "a
file inside a root is reachable only through that root". A deployment that
bind-mounts something sensitive under an egress root has widened the allowlist,
and this module will not notice.

Special files are refused by the same `fstat`: `/dev/zero` is not a regular
file, and a FIFO cannot even stall the open, because it is opened `O_NONBLOCK`.
"""
from __future__ import annotations

import errno
import logging
import os
import stat
from typing import Iterable, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

#: How large one outgoing file may be. `0` removes the limit.
#:
#: The room-media route's own raw ceiling is 25 MiB and its *request* cap is
#: 35 MB — and base64 inflates by 4/3, so 25 MiB of bytes is 34.95 MB of
#: request, inside that cap by less than the JSON envelope around it. The
#: refusal reads better here (a sentence in the room, beside the answer) than
#: there (a 413 and a log line), so the cap is set where the encoded form is
#: comfortably inside.
DEFAULT_MAX_BYTES = 24 * 1024 * 1024

# The refusal reasons, in the second person the room will read them in. They are
# constants because they are said out loud: a file that silently does not arrive
# is indistinguishable from an agent that ignored the request.
NO_PATH = "no path was named"
NOT_A_PATH = "its name is not a path"
NO_ROOTS = "this client was built with no egress allowlist, so it sends no files"
RELATIVE = "it is a relative path, and there is no directory to read it against"
OUTSIDE = "it is outside the directories this client may send from"
MISSING = "there is no such file"
NOT_REGULAR = "it is not a regular file"
SWAPPED = "the path changed while it was being opened"
MULTI_LINKED = (
    "it has more than one name on this filesystem, which an allowlist cannot "
    "vouch for"
)


class EgressRefused(Exception):
    """This file may not leave, and this is the sentence to say about it.

    Carries a room-facing `reason` separately from the exception text, because
    the reason is repeated to a person and the exception text is repeated to a
    log, and they are not the same audience.
    """

    def __init__(self, reason: str, path: str = ""):
        self.reason = reason
        self.path = path
        super().__init__(f"{path}: {reason}" if path else reason)


class ApprovedFile:
    """An open descriptor the allowlist has already said yes to.

    Handed out instead of a path so that what is read is what was judged. It is
    a context manager; the caller closes it, and closing twice is not an error.
    """

    __slots__ = ("fd", "path", "name", "size", "_closed")

    def __init__(self, fd: int, path: str, size: int):
        self.fd = fd
        self.path = path
        self.name = os.path.basename(path)
        self.size = size
        self._closed = False

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<ApprovedFile {self.path} {self.size}B>"

    def __enter__(self) -> "ApprovedFile":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def read(self, limit: int) -> bytes:
        """At most `limit` bytes from the judged descriptor.

        `limit` is a ceiling the caller sets and this reads *past* on purpose:
        a caller asking for `size + 1` is asking "did this grow since it was
        measured?", and gets an answer.

        A read that fails comes back as an `EgressRefused`, not as an `OSError`.
        The outbox is allowed to be an NFS or cloud-sync mount — the module
        docstring blesses exactly that deployment — and such a mount answers
        `ESTALE` or `EIO` mid-read whenever it feels like it. A caller that has
        to catch `EgressRefused` *and* `OSError` will catch one of them, and I1
        says which failure that costs: the bearer's only poller, over an
        attachment.
        """
        chunks = []
        remaining = int(limit)
        while remaining > 0:
            try:
                block = os.read(self.fd, min(remaining, 1 << 20))
            except OSError as exc:
                raise EgressRefused(_unreadable(exc), self.path) from None
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _shut(self.fd)


class EgressAllowlist:
    """The directories this client may send a file from, and nothing else.

    Built once, with its roots, and sealed: every attribute is read-only
    afterwards. An empty allowlist refuses everything, which is what a consumer
    that configured no roots meant.
    """

    #: The `__setattr__` below refuses a write; `__slots__` refuses the way
    #: around it. Without slots an instance carries a `__dict__`, and
    #: `al.__dict__["_roots"] = (...)` widens the allowlist without ever
    #: calling `__setattr__` — a one-liner, in a language where the expensive
    #: bypass (`object.__setattr__`) cannot be closed at all. Closing the cheap
    #: route is not a guarantee, it is the difference between an attacker
    #: needing to mean it and an attacker tripping over it. `ApprovedFile` next
    #: door has had slots all along; this class is the one that decides.
    __slots__ = ("_roots", "_max_bytes", "_sealed")

    def __init__(
        self,
        roots: Iterable[object] = (),
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        resolved = []
        for root in roots or ():
            real = _real(root)
            if not real or not _usable_root(real, root):
                continue
            if real not in resolved:
                resolved.append(real)
        self._roots = tuple(resolved)
        self._max_bytes = max(0, int(max_bytes))
        if not self._roots:
            log.info("egress allowlist is empty — this client sends no files")
        # Sealed last: from here the object is read-only, and there is no
        # method that would widen it either. See the module docstring.
        self._sealed = True

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<EgressAllowlist {len(self._roots)} root(s)>"

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError(
                "the egress allowlist is fixed at construction; build another "
                "client rather than widening this one"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise AttributeError("the egress allowlist is fixed at construction")

    @property
    def roots(self) -> Tuple[str, ...]:
        """The resolved roots, as a tuple — a copy of nothing, mutable by none."""
        return self._roots

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    def allows(self, path: object, base_dir: Optional[object] = None) -> bool:
        """A yes/no with no descriptor, for callers that only want to ask.

        Answering with a boolean is the weaker question on purpose: it can be
        stale by the time anyone acts on it. Uploads go through `open`.
        """
        try:
            approved = self.open(path, base_dir=base_dir)
        except EgressRefused:
            return False
        approved.close()
        return True

    def open(self, path: object, base_dir: Optional[object] = None) -> ApprovedFile:
        """Judge `path` and return the descriptor, or raise `EgressRefused`.

        `base_dir` is what a relative path is read against — the consumer's own
        notion of "where this turn ran", which this library has no opinion
        about. Supplying one widens nothing: the destination is still judged
        against the roots, so a relative path that climbs *into* an allowed root
        from an unallowed base is allowed, and one that climbs out is not. The
        allowlist judges where a file is, never how it was named.
        """
        shown = "" if path is None else str(path)
        if not shown.strip():
            raise EgressRefused(NO_PATH, "")
        if "\x00" in shown:
            raise EgressRefused(NOT_A_PATH, shown)
        if not self._roots:
            raise EgressRefused(NO_ROOTS, shown)

        candidate = os.path.expanduser(shown)
        if not os.path.isabs(candidate):
            base = _real(base_dir) if base_dir is not None else None
            if not base:
                raise EgressRefused(RELATIVE, shown)
            candidate = os.path.join(base, candidate)

        try:
            real = os.path.realpath(candidate)
        except (OSError, ValueError, RuntimeError):
            # A path that will not resolve is a path we know nothing about.
            raise EgressRefused(NOT_A_PATH, shown) from None

        root = self._root_for(real)
        if root is None:
            raise EgressRefused(OUTSIDE, shown)

        fd = self._open_within(root, real, shown)
        try:
            try:
                info = os.fstat(fd)
            except OSError as exc:
                # A descriptor on a mount that went away between the open and
                # the stat. `EgressRefused` rather than `OSError` for the same
                # reason `read` converts: one exception type out of this module,
                # or I1 is one `except` clause away from being breached.
                raise EgressRefused(_unreadable(exc), shown) from None
            if not stat.S_ISREG(info.st_mode):
                raise EgressRefused(NOT_REGULAR, shown)
            if info.st_nlink > 1:
                # The escape realpath cannot see. See the module docstring.
                raise EgressRefused(MULTI_LINKED, shown)
            if self._max_bytes and info.st_size > self._max_bytes:
                raise EgressRefused(_too_big(info.st_size, self._max_bytes), shown)
        except BaseException:
            _shut(fd)
            raise
        return ApprovedFile(fd, real, info.st_size)

    # -- internals ----------------------------------------------------------

    def _root_for(self, real: str) -> Optional[str]:
        """Which root this resolved path lives under, if any.

        The separator on the prefix comparison is the guard: `/allowed-evil`
        starts with `/allowed`, and a directory named to look like a prefix of
        an allowed one is the cheapest attack on this whole module.
        """
        for root in self._roots:
            if real == root or real.startswith(root + os.sep):
                return root
        return None

    def _open_within(self, root: str, real: str, shown: str) -> int:
        """Open `real` by walking down from `root`, refusing every symlink.

        This is the check that actually holds: the string comparison above ran
        against a snapshot of the filesystem, and the filesystem is not obliged
        to hold still. Each component is opened relative to the previous one
        with `O_NOFOLLOW`, so anything that became a link between then and now
        fails here instead of quietly resolving somewhere else.

        The path being walked came out of `realpath`, so every component of it
        *was* a directory and none of them *was* a symlink when it was measured.
        That is what lets the walk read both `ELOOP` and a mid-path `ENOTDIR`
        (which is how macOS answers `O_NOFOLLOW|O_DIRECTORY` on a symlink) as
        the same thing: the path changed underneath us.
        """
        try:
            relative = os.path.relpath(real, root)
        except ValueError:
            # Windows: a legacy DOS device name inside a root — `outbox\NUL` —
            # canonicalises to another mount (`\\.\NUL`) inside `relpath`, which
            # raises rather than answering. The file such a path names does not
            # live under any root, and the one-exception contract (I1) holds:
            # this leaves as a refusal, never as a bare ValueError out of the
            # poll thread.
            raise EgressRefused(OUTSIDE, shown) from None
        parts = [] if relative == os.curdir else relative.split(os.sep)
        if not parts or any(part in ("", os.pardir) for part in parts):
            # `..` cannot survive a realpath, so reaching this means something
            # stranger than a traversal attempt. Refuse it as one anyway.
            raise EgressRefused(OUTSIDE, shown)

        directory = getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        nonblock = getattr(os, "O_NONBLOCK", 0)

        if os.open not in getattr(os, "supports_dir_fd", set()):
            # No `openat` on this platform. The walk degrades to a single open
            # of the resolved path — with `O_NOFOLLOW` where it exists, which
            # still refuses a swapped *final* component. Windows has neither
            # `openat` nor `O_NOFOLLOW`, so the open follows any link swapped
            # in after the check (reproduced: a directory component replaced by
            # a junction between `realpath` and the open handed an outside file
            # out). What Windows does have is the handle itself as authority:
            # the kernel names the file a handle really opened, and a name that
            # is not the judged path is refused the same way `ELOOP` is.
            fd = _open(real, os.O_RDONLY | nofollow | nonblock, shown)
            if os.name == "nt" and not _handle_names(fd, real):
                _shut(fd)
                raise EgressRefused(SWAPPED, shown)
            return fd

        fd = _open(root, os.O_RDONLY | directory | nofollow, shown, traversing=True)
        try:
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                flags = os.O_RDONLY | nofollow | nonblock | (0 if last else directory)
                nxt = _open(part, flags, shown, dir_fd=fd, traversing=not last)
                _shut(fd)
                fd = nxt
        except BaseException:
            _shut(fd)
            raise
        return fd


def _open(target: object, flags: int, shown: str, dir_fd: Optional[int] = None,
          traversing: bool = False) -> int:
    """`os.open`, with every failure turned into a room-facing refusal."""
    try:
        if dir_fd is None:
            return os.open(target, flags)  # type: ignore[arg-type]
        return os.open(target, flags, dir_fd=dir_fd)  # type: ignore[arg-type]
    except OSError as exc:
        if exc.errno == errno.ELOOP or (traversing and exc.errno == errno.ENOTDIR):
            # A symlink where the check saw a directory or a file: either an
            # escape attempt or a swap in the window between check and open.
            # Both refuse the same way.
            raise EgressRefused(SWAPPED, shown) from None
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            raise EgressRefused(MISSING, shown) from None
        raise EgressRefused(
            f"it could not be opened ({exc.strerror or exc})", shown
        ) from None


def _handle_names(fd: int, real: str) -> bool:  # pragma: no cover — Windows only
    """Does this open handle really name the path that was judged? (Windows.)

    The degraded single-open walk above cannot refuse a symlink or junction on
    Windows — there is no `O_NOFOLLOW` to open with — so the question is asked
    the other way around: the kernel is asked what file the handle it returned
    actually names (`GetFinalPathNameByHandle`, the same resolution `realpath`
    uses), and an answer that is not the judged path means the path changed
    between the check and the open. Anything that prevents the answer fails
    closed: a handle whose identity cannot be established is a handle this
    module will not vouch for, which is the direction it fails in on purpose.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    size = 32768
    buffer = ctypes.create_unicode_buffer(size)
    try:
        length = kernel32.GetFinalPathNameByHandleW(
            wintypes.HANDLE(msvcrt.get_osfhandle(fd)), buffer, size, 0)
    except OSError:
        return False
    if not length or length >= size:
        return False
    final = buffer.value
    if final.startswith("\\\\?\\UNC\\"):
        final = "\\\\" + final[8:]
    elif final.startswith("\\\\?\\"):
        final = final[4:]
    return os.path.normcase(final) == os.path.normcase(real)


def _shut(fd: int) -> None:
    """Close a descriptor and say nothing about it.

    Closing is cleanup, and cleanup that raises replaces the failure being
    handled with a less interesting one. On the paths that call this, the
    interesting failure is already on its way to the room.
    """
    try:
        os.close(fd)
    except OSError:  # pragma: no cover — already gone is close enough
        pass


def _unreadable(exc: OSError) -> str:
    """A room-facing sentence for a file the filesystem would not hand over."""
    return f"it could not be read ({exc.strerror or exc})"


def _usable_root(real: str, given: object) -> bool:
    """Would this root ever match anything? Say so now, not at the first upload.

    Two root shapes resolve cleanly, look valid in `sendable_roots`, and then
    refuse every file for ever. Both fail closed, which is the right direction
    and the wrong moment — a typo belongs at startup, in the consumer's config
    validation, not in a sentence in a room three days later.

    - **The filesystem root.** `/` is not an allowlist; it is the absence of
      one, spelled in a way that reads like a value. It is refused rather than
      honoured: a consumer that means "send anything" has no business holding
      this object, and a consumer that typed `/` by accident gets told.
    - **A root spelled in the wrong case.** On a case-insensitive filesystem —
      macOS's default, and every SMB mount — `realpath` resolves `…/Outbox`
      when the directory on disk is `outbox`, and stores the spelling it was
      given. No child's real path then starts with it, so the root matches
      nothing at all. Checking each component against the directory that holds
      it is what catches it, on every platform, and catches a plain misspelling
      with the same reading.

    A component we cannot list is not a component we may condemn: an
    unreadable parent directory answers "cannot tell", and the root is kept.
    """
    if os.path.splitdrive(real)[1] == os.sep:
        # `/` on POSIX; on Windows also what `/` resolves to — a drive root
        # like `C:\` (or a share root), which is the same absence of an
        # allowlist spelled with a drive letter. Left in place it would refuse
        # every file for ever without saying why, since no child's real path
        # starts with `C:\` + a second separator.
        log.warning(
            "egress root %r is the whole filesystem, which is not an allowlist "
            "— dropping it", str(given),
        )
        return False
    parent, name = os.path.split(real)
    while name:
        try:
            present = name in os.listdir(parent)
        except OSError:
            return True  # Cannot tell. Not grounds for narrowing the allowlist.
        if not present:
            log.warning(
                "egress root %r does not name a directory on this filesystem "
                "(%r is not in %r) — dropping it, because it would refuse every "
                "file and never say why", str(given), name, parent,
            )
            return False
        parent, name = os.path.split(parent)
    return True


def _real(path: object) -> str:
    """A path resolved for comparison, or `""` when it cannot be."""
    if path is None:
        return ""
    text = str(path).strip()
    if not text:
        return ""
    try:
        return os.path.realpath(os.path.expanduser(text))
    except (OSError, ValueError, RuntimeError):  # pragma: no cover — rare shapes
        return ""


def _too_big(size: int, limit: int) -> str:
    return (
        f"it is {_mb(size)} and this client sends at most {_mb(limit)} "
        f"in one file"
    )


def _mb(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB"


def sendable_roots(roots: Sequence[object]) -> Tuple[str, ...]:
    """The roots as an allowlist would resolve them — for a consumer's config
    validation, so a typo shows up at startup rather than at the first upload.

    A root that is missing from what comes back is a root that would have sent
    nothing: it did not resolve, it was the filesystem root, or it is spelled in
    a case this filesystem does not use. Each one is logged as it is dropped.
    Comparing lengths is the check a consumer wants at startup.
    """
    return EgressAllowlist(roots).roots
