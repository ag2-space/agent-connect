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
        a caller asking for `cap + 1` is asking "did this grow since it was
        measured?", and gets an answer.
        """
        chunks = []
        remaining = int(limit)
        while remaining > 0:
            block = os.read(self.fd, min(remaining, 1 << 20))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        return b"".join(chunks)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                os.close(self.fd)
            except OSError:  # pragma: no cover — already gone is close enough
                pass


class EgressAllowlist:
    """The directories this client may send a file from, and nothing else.

    Built once, with its roots, and sealed: every attribute is read-only
    afterwards. An empty allowlist refuses everything, which is what a consumer
    that configured no roots meant.
    """

    def __init__(
        self,
        roots: Iterable[object] = (),
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        resolved = []
        for root in roots or ():
            real = _real(root)
            if real and real not in resolved:
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
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise EgressRefused(NOT_REGULAR, shown)
            if info.st_nlink > 1:
                # The escape realpath cannot see. See the module docstring.
                raise EgressRefused(MULTI_LINKED, shown)
            if self._max_bytes and info.st_size > self._max_bytes:
                raise EgressRefused(_too_big(info.st_size, self._max_bytes), shown)
        except Exception:
            os.close(fd)
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
        relative = os.path.relpath(real, root)
        parts = [] if relative == os.curdir else relative.split(os.sep)
        if not parts or any(part in ("", os.pardir) for part in parts):
            # `..` cannot survive a realpath, so reaching this means something
            # stranger than a traversal attempt. Refuse it as one anyway.
            raise EgressRefused(OUTSIDE, shown)

        directory = getattr(os, "O_DIRECTORY", 0)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        nonblock = getattr(os, "O_NONBLOCK", 0)

        if os.open not in getattr(os, "supports_dir_fd", set()):  # pragma: no cover
            # No `openat` on this platform. The walk degrades to a single
            # `O_NOFOLLOW` open of the resolved path, which still refuses a
            # swapped *final* component; the mid-path race is not closable
            # without `openat`, and pretending otherwise would be worse.
            return _open(real, os.O_RDONLY | nofollow | nonblock, shown)

        fd = _open(root, os.O_RDONLY | directory | nofollow, shown, traversing=True)
        try:
            for index, part in enumerate(parts):
                last = index == len(parts) - 1
                flags = os.O_RDONLY | nofollow | nonblock | (0 if last else directory)
                nxt = _open(part, flags, shown, dir_fd=fd, traversing=not last)
                os.close(fd)
                fd = nxt
        except Exception:
            os.close(fd)
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
    validation, so a typo shows up at startup rather than at the first upload."""
    return EgressAllowlist(roots).roots
