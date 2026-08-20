"""What this library is allowed to write down: names, paths, and redaction.

Three grammars live here, together because they answer one question — *may this
string become part of something persistent?* — and because each of them has,
separately, been the source of a production bug:

- The **instance name** (J2) namespaces every per-client file. One host may run
  clients against several gateways (prod + dev; the broker is
  multi-implementation by design), and broker task ids are unique only *within*
  a gateway — so shared state across instances means one client claiming
  another's work. The grammar is a single source of truth on purpose: every
  drift between it and one of its consumers produced the same bug class (queued
  work plus ACKs plus silently stranded results) — first a length mismatch,
  then a charset mismatch, when an `isalnum()` check accepted a Unicode name the
  ASCII regex rejected.
- The **wire task id** (F8) is untrusted input that lands in journal paths and
  goes back out on the wire. Unconstrained, it is path traversal in both
  directions.
- **URL redaction** (D3) runs before any gateway URL is persisted or logged. A
  state dir can be vault-synced; a gateway configured with `user:pass@` userinfo
  or a `?token=` query must not land there in plaintext.

`write_private_atomic` lives here for the same reason: it is *how* this library
is allowed to write, and both files it writes — the journal and the status —
need the same discipline. A reader must see the old file or the new one and
never a half-written one, and neither file is anybody else's business.
"""
from __future__ import annotations

import os
import re
import tempfile
import urllib.parse
from pathlib import Path
from typing import Union

#: THE instance-name grammar — ASCII `[A-Za-z0-9_-]`, 1-32 characters, and
#: nothing else. Every consumer derives from this pattern rather than restating
#: it: the drifts that stranded results were all restatements. The charset
#: deliberately excludes `.` and the path separators, which is what lets an
#: instance name be a directory name without further escaping, and excludes `~`,
#: which a consumer's local-id encoding can therefore use as a separator that
#: cannot occur inside either half.
INSTANCE_NAME_PATTERN = r"[A-Za-z0-9_-]{1,32}"
_INSTANCE_RE = re.compile(INSTANCE_NAME_PATTERN)

#: The broker's task-id grammar as the protocol documents it. Validated before
#: *any* use, not just before a filesystem one.
WIRE_ID_PATTERN = r"[A-Za-z0-9._-]{1,64}"
_WIRE_ID_RE = re.compile(WIRE_ID_PATTERN)


def valid_instance_name(name: str) -> bool:
    """True when `name` may namespace this client's state (J2)."""
    return isinstance(name, str) and bool(_INSTANCE_RE.fullmatch(name))


def valid_wire_id(wire_id: str) -> bool:
    """True when `wire_id` is a broker id safe to journal and echo back (F8).

    `.` and `..` match the charset and are excluded by name: they are legal
    slugs and illegal filenames.
    """
    if not isinstance(wire_id, str) or wire_id in (".", ".."):
        return False
    return bool(_WIRE_ID_RE.fullmatch(wire_id))


#: What a URL becomes when it cannot be redacted. Failing redaction has to fail
#: towards writing nothing sensitive — a fallback to the original string would
#: publish exactly the credential the redaction exists to remove.
UNREDACTABLE = "<unparseable url>"


def redact_url(value: str) -> str:
    """Scheme, host and path only — userinfo, query and fragment dropped (D3).

    Never raises, and never falls back to the value it failed to redact: a URL
    that will not parse becomes `UNREDACTABLE`. The shapes that break the parse
    are the ones that carry credentials — `https://u:p@host:notaport/x?token=…`
    raises on the port, `http://u:p@[::1/x` raises inside `urlsplit` — so the
    obvious "return it unchanged" fallback publishes both halves of exactly what
    was being redacted.
    """
    text = str(value)
    try:
        parts = urllib.parse.urlsplit(text)
    except Exception:  # noqa: BLE001 — an unsplittable URL tells us nothing safe
        return UNREDACTABLE
    if not parts.scheme and not parts.netloc:
        return text  # not a URL at all; there was nothing to redact
    try:
        host = parts.hostname or ""
        port = parts.port
        if port:
            host = f"{host}:{port}"
    except ValueError:
        # A port that is not a number makes `hostname`/`port` unusable. The
        # userinfo still has to go, and `netloc` after the last `@` is what is
        # left of the authority.
        host = parts.netloc.rpartition("@")[2]
    try:
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:  # noqa: BLE001 — redaction must never break status I/O
        return UNREDACTABLE


def write_private_atomic(path, text: str) -> None:
    """Write `text` to `path` so a reader sees either the old file or the new.

    `0600`, because a journal is nobody else's business and a state dir is
    exactly the kind of directory that gets synced somewhere. The fsync is what
    makes "durable" mean durable rather than "in the page cache when the power
    went" — the whole ack-ordering rule (F2) is a lie without it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        try:
            os.fchmod(handle, 0o600)
        except (AttributeError, OSError):  # pragma: no cover — non-POSIX modes
            pass
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class StateLayout:
    """Where one instance's durable state lives, and nowhere else.

    Every per-client file hangs off `<state_dir>/<instance>/`, so two clients on
    one host — prod and dev, say — share no path by construction rather than by
    each filename remembering to carry a suffix. The instance name is validated
    through `valid_instance_name`, which is also what makes the directory
    component safe.

    The paths are named here and written by the modules that own them: the
    journal by the wire loop, the status file by the poll outcomes, the
    singleton by the poller guard.
    """

    def __init__(self, state_dir: Union[str, "os.PathLike[str]"], instance: str = "default"):
        if not valid_instance_name(instance):
            raise ValueError(
                f"instance name must match {INSTANCE_NAME_PATTERN} (ASCII only); "
                f"got {instance!r}"
            )
        self.instance = instance
        # `~` is expanded because a caller who wrote it meant their home; this
        # is not a location being guessed, which is the thing C7 forbids.
        self.state_dir = Path(state_dir).expanduser()
        #: This instance's private corner of the state dir.
        self.root = self.state_dir / instance

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<StateLayout {self.root}>"

    @property
    def journal_path(self) -> Path:
        """Accepted-and-completed ids — the durable half of task delivery."""
        return self.root / "journal.jsonl"

    @property
    def status_path(self) -> Path:
        """The library's own connection-only status, so observability survives
        a consumer that never reads the status hook."""
        return self.root / "connection-status.json"

    @property
    def singleton_path(self) -> Path:
        """The singleton-per-bearer guard's file (J1)."""
        return self.root / "poller.lock"

    def ensure(self) -> Path:
        """Create the instance root, privately, and return it.

        `0o700` because a journal and a lock are nobody else's business, and
        because a state dir is exactly the kind of directory that gets synced.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:  # pragma: no cover — filesystems without POSIX modes
            pass
        return self.root
