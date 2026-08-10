"""What arrived attached to a room message, and what may safely be done with it.

Two jobs, and nothing else: read the relay's `attachments:` header into the
`Attachment` vocabulary, and open one of those files carefully enough that a
path someone else influenced cannot make the Worker read something it should
not. Which content block an attachment becomes, and what the room is told when
it cannot become one, belongs to the Adapter — this module has no idea what ACP
is.

## The header's shape is not invented here

`attachments:` is **a one-line JSON array of objects**, each with a `locator`
and optional `id` / `mime` / `filename` / `size` / `sha256` / `expiry`. That is
not a reading of the documentation — it is the relay client's own encoder and
decoder, `ag2_sparrow/local_task_protocol.py`: `AttachmentRef.as_dict`,
`format_attachments` (`json.dumps(..., separators=(",", ":"))`, guaranteed
single-line so it cannot forge a header) and `parse_attachments`. The sibling
headers `content_modalities` (comma-joined `text,image,audio,video,file`) and
`media_form` (`attachment`) are stamped by `media_attachment_headers` in the
same module, and both are *derived from the same refs* — the modalities are one
per ref mime, and the form is the literal constant `"attachment"` on the
messaging task path. They are a summary of the list, so this module reads the
list and ignores the summary: where the two could ever disagree, the list is the
one carrying the bytes.

Decoding is tolerant on purpose, matching the relay's own rule: a malformed
value, a non-list payload, an element that is not an object, or an element with
no locator is skipped rather than raised. An attachment nobody can parse must
not cost the person their question.

## The path is sender-adjacent data

The relay wrote the locator, not the sender — but what the relay wrote came from
a message someone else sent, and the file at the end of it was chosen by that
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

**The legacy body marker is deliberately not read.** The relay dual-writes
`[File attached: <path>]` into the task body beside these headers. The body is
what the person typed; a header is not. Reading a path out of the body would let
anyone in a room name any file on the operator's machine and have the Worker
read it, so this module never looks there — and neither should anything else.

`AGENT_CONNECT_ATTACHMENT_MAX_BYTES` bounds what is read. An attachment over it
is reported, never shrunk: nothing here converts, resizes or transcodes anything
(see `read`).
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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

#: The relay's modality vocabulary (`local_task_protocol.CONTENT_MODALITIES`
#: minus `text`, which is the message body rather than a file).
IMAGE, AUDIO, VIDEO, FILE = "image", "audio", "video", "file"

#: A modality, in the words a person would use for it in a room.
MODALITY_WORDS: Dict[str, str] = {
    IMAGE: "image", AUDIO: "audio", VIDEO: "video", FILE: "file",
}

#: `type/subtype`, with no parameters. Anything else is not a media type we are
#: willing to repeat to a Local Agent as if it were one.
_MIME = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")

#: Control characters, including the newline a JSON-encoded filename can carry
#: through a one-line header. Scrubbed before a name is repeated in a room,
#: where a bare newline would let a filename forge lines of its own.
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


def parse(raw: Optional[str]) -> Tuple[Attachment, ...]:
    """The `attachments:` header value, as `Attachment`s. Never raises."""
    if not raw or not raw.strip():
        return ()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, list):
        return ()
    out = []
    for element in payload:
        if not isinstance(element, dict):
            continue
        locator = element.get("locator")
        if not isinstance(locator, str) or not locator.strip():
            # A ref with nothing to point at is meaningless — the relay's
            # encoder drops these too, so one here did not round-trip.
            continue
        out.append(
            Attachment(
                locator=locator,
                mime=_string(element.get("mime")),
                filename=_string(element.get("filename")),
                size=_count(element.get("size")),
                sha256=_string(element.get("sha256")),
                id=_string(element.get("id")),
            )
        )
    return tuple(out)


def mime_of(attachment: Attachment) -> str:
    """The media type to declare for this attachment.

    The relay's label when it is a usable one; otherwise a guess from the file
    *name*'s extension, which is only ever allowed to choose how the bytes are
    labelled — never which bytes are read, and never whether they are read at
    all. Nothing is sniffed and nothing is opened to find out.
    """
    declared = (attachment.mime or "").split(";")[0].strip().lower()
    if _MIME.match(declared):
        return declared
    guessed, _ = mimetypes.guess_type(_clean(attachment.filename) or "x")
    guessed = (guessed or "").strip().lower()
    return guessed if _MIME.match(guessed) else OCTET_STREAM


def modality(attachment: Attachment) -> str:
    """`image` / `audio` / `video` / `file`, by media type.

    The same mapping the relay uses when it stamps `content_modalities`
    (`local_task_protocol.modality_for_mime`), reimplemented rather than
    imported because that module is the relay client's, not ours.
    """
    mime = mime_of(attachment)
    for prefix, name in ((IMAGE, IMAGE), (AUDIO, AUDIO), (VIDEO, VIDEO)):
        if mime.startswith(prefix + "/"):
            return name
    return FILE


def label(attachment: Attachment) -> str:
    """A name for this attachment that is safe to repeat in a room.

    The platform's filename when there is one, else the local file's own name.
    Both came from somewhere else, so both are scrubbed of control characters,
    collapsed to single spaces and truncated: a name is for a person to
    recognise the file by, and nothing more is owed to it.
    """
    name = _clean(attachment.filename)
    if not name:
        name = _clean(Path(attachment.locator or "").name)
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

    See the module docstring for why each of the checks below is here.
    """
    raw = attachment.locator or ""
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


def _string(value: Any) -> str:
    """A field that is only interesting when it is genuinely a string.

    `str(value)` on whatever JSON happened to hold would turn a nested object
    into a Python `repr` and then carry it around as if the platform had said
    it.
    """
    return value if isinstance(value, str) else ""


def _count(value: Any) -> int:
    """A byte count, or `0` for "unknown".

    A bool is not a count (`int(True) == 1` would lie) and a negative one is
    nonsense that would slip past a `size and size > limit` check — both become
    unknown, which is honest and which nothing here trusts anyway: the size that
    decides whether an attachment is read is the one `fstat` reports.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


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
