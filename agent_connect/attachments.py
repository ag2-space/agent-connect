"""What arrived attached to a room message, and what may safely be done with it.

Two jobs, and nothing else: take the Relay Client's resolved attachments into
the `Attachment` vocabulary the Adapter boundary speaks, and open one of those
files carefully enough that a path someone else influenced cannot make the
Worker read something it should not. Which content block an attachment becomes,
and what the room is told when it cannot become one, belongs to the Adapter —
this module has no idea what ACP is.

## Nothing here parses a wire, because there is no wire here to parse

This module used to decode a one-line JSON `attachments:` header, plus the
`content_modalities` and `media_form` that were stamped beside it. **The broker
never sent any of them.** Its task object has no media field at all; media rides
*inside* the task string as one `[ag2space-media: …]` marker, and the header
shape — `locator` / `id` / `mime` / `filename` / `size` / `sha256` / `expiry` —
was `AttachmentRef` out of sutando's task-file schema, synthesized by
`ag2-sparrow` on its way to a file this Worker no longer reads. `id`, `sha256`
and `expiry` were never populated even there. A decoder for a header nobody
writes is not tolerance, it is a room with no door, so it is gone.

Reading the marker, fetching the bytes over the poll bearer, bounding them at
the gateway's own ceiling and writing them to disk is the Relay Client's job, in
`ag2_relay_client.media`, and it is finished before a Task is delivered. What
reaches this module is a tuple in which every attachment either has a local path
or has an honest sentence about why it has not — no URL, no marker text, and
nothing left to parse. `delivered` is the whole of the crossing.

**A fetch that failed is still an attachment.** It is carried, named, and given
to the Adapter with the library's own reason, because an agent that can say "you
attached something and I could not read it" is more use than a Turn that died,
and rejecting the Task would steal the person's chance to be answered in words.

## The path is sender-adjacent data

The Relay Client wrote the path, not the sender — but what it wrote came from a
message someone else sent, and the file at the end of it was chosen by that
person too. So the path is resolved before it is judged, and the judging is done
on the **open file descriptor** rather than on the path, so what was measured is
what is read:

* an absolute path only — a relative one would resolve against whatever
  directory the Worker happens to be running in, which is not an answer;
* `resolve()` first, so `..` and symlinks are gone before anything is opened;
* opened `O_NOFOLLOW`, so a symlink swapped in after the resolve is refused
  rather than followed;
* opened `O_NONBLOCK`, so a FIFO does not hang the Turn waiting for a writer
  that never comes — the regular-file check happens after the open, and without
  this flag it would never be reached;
* `fstat` on that descriptor: a regular file, and no bigger than the limit;
* read with the limit still enforced, in case it grew between the two.

**A path in the message body is deliberately not read.** Whatever a sender types
that looks like `[File attached: /etc/passwd]` is left exactly where they typed
it. The body is what a person wrote; reading a path out of it would let anyone
in a room name any file on the operator's machine and have the Worker open it,
so this module never looks there — and neither should anything else.

`AGENT_CONNECT_ATTACHMENT_MAX_BYTES` bounds what is read. An attachment over it
is reported, never shrunk: nothing here converts, resizes or transcodes anything
(see `read`).
"""
from __future__ import annotations

import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from .events import Attachment

MAX_BYTES_ENV = "AGENT_CONNECT_ATTACHMENT_MAX_BYTES"

#: How much of one attachment is read into a prompt. Ten megabytes: comfortably
#: more than any screenshot, less than a video someone dropped in by mistake —
#: and the whole of it is base64'd into a single JSON-RPC message over a pipe,
#: so the ceiling is real. `0` removes it, for an operator who knows what their
#: Local Agent will take.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024

#: What an unlabelled or unusably-labelled file is called. Not a guess about
#: the content — a statement that there is none.
OCTET_STREAM = "application/octet-stream"

#: The kinds of thing a file can be, as an Adapter has to reason about them: a
#: content block per kind under ACP, a sentence per kind everywhere else.
IMAGE, AUDIO, VIDEO, FILE = "image", "audio", "video", "file"

#: A modality, in the words a person would use for it in a room.
MODALITY_WORDS: Dict[str, str] = {
    IMAGE: "image", AUDIO: "audio", VIDEO: "video", FILE: "file",
}

#: `type/subtype`, with no parameters. Anything else is not a media type we are
#: willing to repeat to a Local Agent as if it were one.
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")

#: Control characters. Scrubbed before a name is repeated in a room, where a
#: bare newline would let a filename forge lines of its own.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Opened:
    """The bytes of one attachment, or the reason there are none.

    `problem` is a fragment of a sentence — "it is not a regular file" — for the
    Adapter to put inside whatever it says to the room. This module does not
    write the room's words, but it is the only thing that knows why.
    """

    data: bytes = b""
    path: str = ""
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


def delivered(resolved: Iterable) -> Tuple[Attachment, ...]:
    """The Relay Client's attachments, in the words the Adapter boundary uses.

    The library resolves each marker into an attachment of its own — a `path`,
    a `name`, and an `ok`/`reason` pair for a fetch that did not happen — and
    this is the one place those become the boundary's `path`, `filename` and
    `reason`. One crossing, named, instead of the same two renames done again
    inside every Adapter: the last time an Adapter was handed the library's
    object unconverted, `label` read a field that was not there and every Turn
    carrying any file at all died as a worker error nobody could act on.

    Nothing is fetched, opened or judged here, and nothing is dropped. An
    attachment whose fetch failed comes through carrying the library's reason,
    which is written room-facing on purpose and carries neither the URL nor the
    host it could not reach.
    """
    return tuple(
        Attachment(
            path=one.path or "",
            mime=one.mime or "",
            filename=one.name or "",
            size=one.size or 0,
            reason="" if one.ok else (one.reason or ""),
        )
        for one in (resolved or ())
    )


def mime_of(attachment: Attachment) -> str:
    """The media type to declare for this attachment.

    The Relay Client's label when it is a usable one — it comes from the fetch's
    own `Content-Type`, falling back to the marker's hint — and otherwise a
    guess from the file *name*'s extension, which is only ever allowed to choose
    how the bytes are labelled, never which bytes are read and never whether
    they are read at all. Nothing is sniffed and nothing is opened to find out.
    """
    declared = (attachment.mime or "").split(";")[0].strip().lower()
    if _MIME.match(declared):
        return declared
    guessed, _ = mimetypes.guess_type(_clean(attachment.filename) or "x")
    guessed = (guessed or "").strip().lower()
    return guessed if _MIME.match(guessed) else OCTET_STREAM


def modality(attachment: Attachment) -> str:
    """`image` / `audio` / `video` / `file`, by media type."""
    mime = mime_of(attachment)
    for prefix, name in ((IMAGE, IMAGE), (AUDIO, AUDIO), (VIDEO, VIDEO)):
        if mime.startswith(prefix + "/"):
            return name
    return FILE


def label(attachment: Attachment) -> str:
    """A name for this attachment that is safe to repeat in a room.

    The platform's filename when there is one, else the local file's own name.
    Both came from somewhere else — the first from a marker that does not escape
    anything, the second from a directory the Relay Client owns — so both are
    scrubbed of control characters, collapsed to single spaces and truncated: a
    name is for a person to recognise the file by, and nothing more is owed to
    it.
    """
    name = _clean(attachment.filename)
    if not name:
        name = _clean(Path(attachment.path or "").name)
    name = name[:120].strip()
    return name or "an unnamed attachment"


def max_bytes(env: Optional[dict] = None) -> int:
    """How much of one attachment may be read. `0` is no limit.

    A value typed wrong is the default, not a Worker that will not start — the
    same rule the Turn deadline follows, for the same reason.
    """
    raw = ((env if env is not None else os.environ).get(MAX_BYTES_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return value if value >= 0 else DEFAULT_MAX_BYTES


def read(attachment: Attachment, limit: int = 0) -> Opened:
    """The attachment's bytes, exactly as they are on disk — or why not.

    **Nothing is converted, resized or transcoded.** What comes back is what
    `os.read` returned, byte for byte; an attachment too big for `limit` is
    reported as too big rather than shrunk to fit, because a resized screenshot
    is a different screenshot and a person asking what is wrong with theirs
    deserves to be told, not answered about a picture they did not send.

    An attachment the Relay Client never managed to fetch is reported in the
    library's own words rather than this module's. It has no path, and every
    sentence below is about a path — "it is not absolute" would be a true
    statement about a file that never had one and a lie about why it is missing.

    See the module docstring for why each of the checks after that is here.
    """
    if attachment.reason:
        return Opened(problem=attachment.reason)
    raw = attachment.path or ""
    if "\x00" in raw:
        return Opened(problem="its path is not a path")
    path = Path(raw)
    if not path.is_absolute():
        return Opened(
            problem="its path is not absolute, so there is no saying what it points at"
        )
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return Opened(problem="it is not on this machine any more")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        handle = os.open(real, flags)
    except OSError as exc:
        return Opened(problem=f"it could not be opened ({exc.strerror or exc})")
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            return Opened(problem="it is not a regular file")
        if limit and info.st_size > limit:
            return Opened(problem=_too_big(info.st_size, limit))
        data = _read_all(handle, limit)
    except OSError as exc:
        return Opened(problem=f"it could not be read ({exc.strerror or exc})")
    finally:
        os.close(handle)
    if limit and len(data) > limit:
        return Opened(problem=_too_big(len(data), limit))
    return Opened(data=data, path=str(real))


# -- internals --------------------------------------------------------------


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", text or "")).strip()


def _read_all(handle: int, limit: int) -> bytes:
    """Everything on the descriptor, stopping once the limit is exceeded."""
    chunks, total = [], 0
    while True:
        block = os.read(handle, 1 << 20)
        if not block:
            break
        chunks.append(block)
        total += len(block)
        if limit and total > limit:
            break
    return b"".join(chunks)


def _too_big(size: int, limit: int) -> str:
    return (
        f"it is {_mb(size)} and agent-connect reads at most {_mb(limit)} of one "
        f"attachment ({MAX_BYTES_ENV})"
    )


def _mb(count: int) -> str:
    return f"{count / (1024 * 1024):.1f} MB"
