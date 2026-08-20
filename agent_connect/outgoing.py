"""A file the Local Agent produced, on its way out of this machine.

The sibling of `agent_connect.attachments`, and deliberately not part of it. That
module answers *"is this safe to open?"* about a file someone else chose and the
relay already downloaded. This one answers a different question about a path the
**agent** named: *"may this leave the machine?"* — and a yes to the first is not
a yes to the second, so the guards are not shared and neither is the module.

## The route is the feature

Files do not go out because the Worker uploads them. They go out because they are
placed in the **outgoing result directory** and named by a marker in the result
body, and the Relay Client does the rest. That route is not ours to choose — it is
the relay client's, read from its own source rather than invented here:

* `ag2_sparrow/result_markers.py` recognises `[file: <path>]`, `[send: <path>]`
  and `[attach: <path>]` anywhere in a result body, in document order, and strips
  them from the text it delivers, so the room reads prose and receives files.
* `ag2_sparrow/remote_gateway_bridge._post_ready_results` uploads each one to the
  task's own room and *then* posts the body — one reply, its files beside it —
  and appends `[attachment not sent: … (reason)]` to the body for any it refused.
* `ag2_sparrow/send_allowlist.is_path_sendable` is what it refuses with: a
  **regular file** whose `realpath` is the outgoing result directory — the same
  directory the Worker writes results into — or something under it, plus a couple
  of `/tmp/` prefixes belonging to another app. Nothing else on the machine is
  sendable, by design, and richer policy is injected by an embedding app through
  `register_extra_roots()`.

**The Worker must not post media itself.** It holds a relay token and a room id,
so it could — and that is exactly the exfiltration route the allowlist exists to
close: a chat message that can name any path becomes a chat message that can take
any file off the machine. Owner-tier restriction narrows who can try it; it does
not close it, because the owner in a room is not necessarily the person sitting
at this keyboard. So the only thing this module does is **stage** a file into the
directory the allowlist already trusts, and let the Relay Client decide at its own
sink. If sending from somewhere else is ever wanted, the extension mechanism is
`register_extra_roots()`, not a second route.

## The permitted area, and what it does not prevent

Staging is a copy, and a copy is a way of promoting a path into the allowlist —
so the copy is the step that has to be judged, or the allowlist would be
vacuous and the Worker would be laundering paths into it.

**The permitted area is the working directory this Turn ran in** (plus the
outgoing directory itself, for a file already written there, which needs no
copy). It is where the Local Agent works, what the Permission Policy already
guards writes against, and the only place a file *this Turn produced* can be. A
path outside it was not produced here, and is refused with the reason said in the
room — which is the whole of the "and the room is told" criterion, because a file
that silently does not arrive is indistinguishable from an agent that ignored the
request.

What this does **not** prevent, stated so nobody reads more into it: an agent that
can be talked into copying `~/.ssh/id_rsa` into its own working directory can
then send the copy. The permitted area is a boundary on paths, not a proof about
contents, and closing that would take a Sandbox that refuses the first copy —
`CONTEXT.md`'s distinction between confinement and a cooperative policy, again.

Everything else here is the same shape as the incoming side: resolve before
judging, judge the **open descriptor** rather than the path, refuse anything that
is not a regular file, and bound the size (`AGENT_CONNECT_OUTGOING_MAX_BYTES`,
defaulting to the relay's own 25 MB upload cap so the refusal arrives here, with
a sentence, instead of at the Relay Client with a log line).

Staged copies live under `<results>/outgoing/<task id>/` and are swept after a
day: they sit in a directory the Relay Client trusts, so leaving them there for ever
would slowly turn a delivery mechanism into a store of sendable files.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .sessions import workspace_dir

MAX_BYTES_ENV = "AGENT_CONNECT_OUTGOING_MAX_BYTES"

#: The relay's own upload ceiling (`remote_gateway_bridge.MAX_MEDIA_BYTES`). The
#: same number on this side is not a second policy — it is the same refusal,
#: arriving where there is a room to say it in. `0` removes it.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

#: How many files one reply may carry. A constant, not a setting: it is not
#: policy, it is the point past which a reply has stopped being a reply.
MAX_FILES = 10

#: How long a staged copy stays in the outgoing directory. Long past any retry
#: the relay client will make, short of "for ever".
KEEP_SECONDS = 24 * 3600

#: Where staged copies go, under the outgoing result directory.
STAGING = "outgoing"

#: The delivery path's marker, exactly as `ag2_sparrow/result_markers.py` reads
#: it: three spellings, anywhere in the body, case-sensitive, `]`-terminated.
#: Ours is the same expression so that what this module recognises and what the
#: Relay Client recognises cannot drift apart.
MARKER = re.compile(r"\[(?:file|send|attach):\s*([^\]]+)\]")

#: Which of the three spellings we *write*. Any of them would do; one of them
#: keeps the archived body readable.
MARKER_FORM = "[file: {path}]"

#: What the Local Agent is told about sending a file, in the preamble. The words
#: live beside the code that judges them, so a change to the rule cannot leave
#: the instruction describing the old one.
INSTRUCTION = (
    "To put a file in the room, write [file: <path>] on a line of its own and "
    "agent-connect attaches it to this reply. The path must be inside your "
    "working directory; a file anywhere else is not sent."
)

#: What a reply says when the agent sent files and no words with them. The room
#: gets a sentence rather than an empty message with attachments under it.
ONLY_FILES = "📎 Attached: {names}."

#: What the room is told about a file that did not go — inside the reply, not as
#: its own message: the person asked for a file *with* an answer, and "you are
#: not getting it" belongs beside the answer they did get.
NOT_SENT = ("📎 agent-connect: {what} the agent named could not be sent:\n{lines}\n"
            "A file is only sent from the working directory this turn ran in.")
NOT_SENT_ONE = "one file"
NOT_SENT_MANY = "{count} files"
NOT_SENT_LINE = "• {name} — {why}"

OUTSIDE = ("it is outside the working directory this turn ran in, and "
           "agent-connect sends nothing from outside it")
TOO_MANY = "no more than {limit} files are sent with one reply"
NO_AREA = ("agent-connect could not tell which directory this turn ran in, so it "
           "sent nothing")
UNNAMEABLE = "its name cannot be written into a message"

#: Control characters, and the brackets that would forge a marker of their own,
#: scrubbed out of any path this module repeats in a room.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Delivery:
    """What one answer's file markers came to.

    `text` is the answer with the markers taken out — what the room will read,
    computed the same way the relay computes it, so the two agree. `markers` are
    the ones to carry on the **result body**, rewritten to point at the staged
    copies; a body carrying any of them must never be marked `[REPLIED]`, or the
    delivery path archives it and the files never leave. `refused` are
    room-facing lines, one per file that did not go.
    """

    text: str = ""
    markers: Tuple[str, ...] = ()
    sent: Tuple[str, ...] = ()
    refused: Tuple[str, ...] = ()

    @property
    def asked(self) -> bool:
        """The agent named a file, whatever became of it."""
        return bool(self.markers or self.refused)


def carries_files(text: Optional[str]) -> bool:
    """Does this body name a file to send? Cheap enough to ask before staging."""
    return bool(MARKER.search(text or ""))


def not_sent_notice(refused: Sequence[str]) -> str:
    """The one paragraph a room gets about files that did not go."""
    if not refused:
        return ""
    what = (NOT_SENT_ONE if len(refused) == 1
            else NOT_SENT_MANY.format(count=len(refused)))
    return NOT_SENT.format(what=what, lines="\n".join(refused))


def only_files_line(sent: Sequence[str]) -> str:
    return ONLY_FILES.format(names=", ".join(sent)) if sent else ""


def result_dir(env: Optional[dict] = None) -> Path:
    """The outgoing result directory — the one the send allowlist trusts.

    It hangs off the workspace and has no setting of its own. There used to be
    one, `AGENT_CONNECT_RESULT_DIR`, because a separate relay-client process had
    to be pointed at the same directory and the launcher named it for both.
    That process is gone (transport-seam ticket 10, workspace ADR 0001), and
    with it the only reason two things ever had to agree about this path: an
    override that nothing on the far side reads is a way to move the staging
    area somewhere the Worker itself will not look for it.

    The directory itself outlives the variable. Transport-seam ticket 09 is
    where the staging airlock retires for the library's allowlisted-path
    egress; until then a file leaves by being placed here.
    """
    return workspace_dir(os.environ if env is None else env) / "results"


def max_bytes(env: Optional[dict] = None) -> int:
    """How large one outgoing file may be. `0` is no limit.

    A value typed wrong is the default, not a Worker that will not start — the
    same rule every other setting here follows.
    """
    raw = ((env if env is not None else os.environ).get(MAX_BYTES_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value >= 0 else DEFAULT_MAX_BYTES


class Outbox:
    """The outgoing result directory, as the one operation the Ladder needs.

    Built with the directory the Worker writes results into, so the staging area
    and the delivery path are the same place by construction rather than by two
    readings of the environment.
    """

    def __init__(self, results_dir=None, limit: Optional[int] = None, clock=time.time):
        self.dir = Path(results_dir).expanduser() if results_dir else result_dir()
        # The staging area is a *subdirectory* of the results directory, and the
        # distinction is load-bearing: the results directory also holds every
        # other Task's archived reply, so treating it as the permitted area
        # would let one room ask for `[file: …/results/task-<other>.txt]` and be
        # handed another room's answer — through the allowlist, which trusts
        # that directory and so cannot catch it.
        self.staging = self.dir / STAGING
        self.limit = max_bytes() if limit is None else limit
        self._clock = clock

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Outbox {self.dir} limit={self.limit}>"

    def stage(self, text: str, ctx=None) -> Delivery:
        """Judge every file this answer names, and stage the ones that may go.

        Never raises: a file that cannot be staged is a line in the room, not a
        Turn that failed. What comes back is the answer without its markers, the
        markers to carry on the result body, and the refusals to say out loud.
        """
        named = [m.group(1).strip() for m in MARKER.finditer(text or "")]
        body = MARKER.sub("", text or "").strip()
        if not named:
            return Delivery(text=body)

        cwd, area = self._area(ctx)
        markers: List[str] = []
        sent: List[str] = []
        refused: List[str] = []
        used: set = set()
        for raw in named:
            shown = _display(raw)
            if len(markers) >= MAX_FILES:
                refused.append(NOT_SENT_LINE.format(
                    name=shown, why=TOO_MANY.format(limit=MAX_FILES)))
                continue
            handle, real, problem = self._open(raw, cwd, area)
            if problem:
                refused.append(NOT_SENT_LINE.format(name=shown, why=problem))
                continue
            try:
                placed = self._place(handle, real, ctx, used)
            except OSError as exc:
                refused.append(NOT_SENT_LINE.format(
                    name=shown, why=f"it could not be staged for sending ({exc.strerror or exc})"))
                continue
            finally:
                os.close(handle)
            if "]" in str(placed):
                refused.append(NOT_SENT_LINE.format(name=shown, why=UNNAMEABLE))
                continue
            markers.append(MARKER_FORM.format(path=placed))
            sent.append(placed.name)
        return Delivery(text=body, markers=tuple(markers), sent=tuple(sent),
                        refused=tuple(refused))

    # -- internals ----------------------------------------------------------

    def _area(self, ctx):
        """Where a file may be sent from: the Turn's working directory, and the
        outgoing directory itself for a file already written there.

        Both resolved once, and the working directory returned on its own as
        well, because a relative path is read against *it* and against nothing
        else — resolving one against the outgoing directory would be answering a
        different question than the agent asked.
        """
        cwd = _resolved(getattr(ctx, "cwd", "") or "")
        here = _resolved(self.staging)
        return cwd, tuple(p for p in (cwd, here) if p is not None)

    def _open(self, raw: str, cwd: Optional[Path], area: Tuple[Path, ...]):
        """Open the named file if it may be sent. Returns `(handle, path, why not)`.

        The caller closes the handle. Everything is decided on the descriptor
        rather than on the path, so what was judged is what is copied.
        """
        if not raw:
            return None, None, "there is no path in the marker"
        if "\x00" in raw:
            return None, None, "its path is not a path"
        if cwd is None:
            # Fail closed. Without the Turn's working directory there is no
            # permitted area to judge against, and "send it anyway" is the one
            # answer this module must never give.
            return None, None, NO_AREA
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = cwd / path
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None, None, "there is no such file"
        if not _inside(real, area):
            return None, None, OUTSIDE
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            handle = os.open(real, flags)
        except OSError as exc:
            return None, None, f"it could not be opened ({exc.strerror or exc})"
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode):
                raise _Refused("it is not a regular file")
            if self.limit and info.st_size > self.limit:
                raise _Refused(_too_big(info.st_size, self.limit))
        except _Refused as refusal:
            os.close(handle)
            return None, None, str(refusal)
        except OSError as exc:
            os.close(handle)
            return None, None, f"it could not be read ({exc.strerror or exc})"
        return handle, real, ""

    def _place(self, handle: int, real: Path, ctx, used: set) -> Path:
        """Put the open file where the send allowlist can see it.

        A file the agent already wrote into the outgoing directory is left where
        it is — it is already sendable, and copying it would only make a second
        one. Everything else is copied *from the descriptor that was judged*.
        """
        here = _resolved(self.staging)
        if here is not None and _inside(real, (here,)):
            return real
        root = self.staging
        self._sweep(root)
        folder = root / _safe(getattr(ctx, "task_id", "") or "turn")
        folder.mkdir(parents=True, exist_ok=True)
        dest = _unique(folder, _safe(real.name) or "file", used)
        with open(dest, "wb") as out:
            while True:
                block = os.read(handle, 1 << 20)
                if not block:
                    break
                out.write(block)
        return dest

    def _sweep(self, root: Path) -> None:
        """Forget staged copies older than a day.

        Best-effort by design: a sweep that raised would cost a person the file
        they asked for, in exchange for tidiness nobody asked for.
        """
        try:
            folders = list(root.iterdir())
        except OSError:
            return
        cutoff = self._clock() - KEEP_SECONDS
        for folder in folders:
            try:
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                continue


class _Refused(Exception):
    """A judgement, raised only to keep one descriptor's cleanup in one place."""


def _resolved(path) -> Optional[Path]:
    if not path:
        return None
    try:
        return Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def _inside(path: Path, area: Sequence[Path]) -> bool:
    """Is this resolved path one of these directories, or under one?

    Compared as strings with a separator appended, exactly as the relay client's
    allowlist does it, so `/x/results-old` is not read as living under
    `/x/results`.
    """
    real = str(path)
    for root in area:
        top = str(root)
        if real == top or real.startswith(top + os.sep):
            return True
    return False


def _safe(name: str) -> str:
    """A file or directory name that cannot be anything but a name.

    The relay client sanitises inbound filenames to the same class of characters;
    an outbound one is chosen by the agent, which is at least as good a reason.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", (name or "").strip())[:120].lstrip(".")


def _unique(folder: Path, name: str, used: set) -> Path:
    """A destination no other file in this reply has taken.

    Two files called `report.md` from two directories are two files, and the
    room has to be able to tell them apart.
    """
    stem, dot, ext = name.partition(".")
    candidate, n = name, 1
    while candidate in used or (folder / candidate).exists():
        candidate = f"{stem}-{n}{dot}{ext}"
        n += 1
    used.add(candidate)
    return folder / candidate


def _display(raw: str) -> str:
    """The path as the agent named it, safe to repeat in a room.

    Scrubbed of control characters and of the brackets a marker is made of, so a
    refusal cannot forge one, and truncated: a person needs to recognise which
    file was refused, not to read an essay.
    """
    text = _CONTROL.sub(" ", raw or "").replace("[", "(").replace("]", ")")
    text = re.sub(r"\s+", " ", text).strip()
    return (text[:160] or "an unnamed file")


def _too_big(size: int, limit: int) -> str:
    return (f"it is {_mb(size)} and agent-connect sends at most {_mb(limit)} in one "
            f"file ({MAX_BYTES_ENV})")


def _mb(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB"
