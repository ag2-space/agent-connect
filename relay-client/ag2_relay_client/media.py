"""Media on the way in: a marker becomes a file before the consumer sees it.

The broker sends **no attachments field**. It never has — the task object is
`id`, `timestamp`, `task`, `source`, `channel_id`, `user_id`, `access_tier`,
`priority` and a few optional context strings, and media rides *inside* the
`task` string as one text marker:

    [ag2space-media: <https-url> mime=<mime> name=<file> size=<bytes> kind=<msgtype>]

A client that waits for an `attachments:` header waits for ever. So this module
is the other half of the wire: it reads the marker, fetches the bytes with the
poll bearer, writes them under the media directory, and hands the consumer a
`Task` carrying resolved local paths. **The consumer never sees a marker or a
URL** — that is the seam, and every choice below serves it.

## The marker is unescaped, so its attributes are hints

Nothing in the marker is quoted or escaped, and the reference consumer reads
attributes as `key=<non-space run>`. Two consequences that are not edge cases —
they are what an ordinary iPhone filename does:

- a filename with a **space** truncates `name=` at the space;
- a filename with a **`]`** truncates the whole marker, taking the tail of the
  URL with it.

So `name` and `kind` ride along as hints and nothing more, and **the mime comes
from the fetch's `Content-Type`**, not from the marker, whenever the fetch says
anything at all. The on-disk name is this module's to choose; the hint only
seasons it.

## A failed fetch never blocks the Task

One budgeted retry, and then the Task is delivered anyway with the attachment
marked failed and the reason carried. Not held until success: the gateway's
media route answers **502 for every cause**, including "not a room member", so a
client that waited for a good answer would wait for ever on a task that was
never going to be servable. Not auto-rejected either: an agent that can say "I
can see you attached something but I could not read it" is more use than a
dead-lettered task, and rejecting steals its chance to answer in words.

## The fetch runs off the poll thread

Cadence is correctness (F1) — the broker extends a lease only while the worker
keeps polling — so nothing that takes as long as a 25 MiB download may happen on
the poll thread. `accept()` is called by the poll loop, strips the markers, and
either delivers immediately (nothing to fetch, which is almost every task) or
hands the Task to this module's own thread. Ack ordering is unchanged (F2):
journal, then ack, then this — only `POST /v1/results` completes a lease, so
fetching after the ack is safe.

A Task *with* media is therefore delivered when its bytes are, which can be
after a Task that was polled later. That reordering is the price of not making
every text answer wait behind someone's video, and it is the right way round.

## Credential routing is not this module's decision

`RelayHTTP.fetch` decides whether the bearer goes out, by parsed origin and base
path (G4). It lives there because that is where the bearer lives; the routing
rule is worth reading in `transport.same_origin` / `transport.under_base`, and
the short version is that a look-alike host gets nothing. This module supplies
the one thing that layer cannot: the refusal to call it at all for a URL that is
not an `http(s)` address it can parse — a malformed URL is a failed attachment,
never an exception out of task intake.

## Where the bytes live, and when they leave

Default is **delete-on-complete plus a startup sweep**: the file goes when the
consumer answers the task, and anything an earlier run left behind is deleted
when this one starts — the queue those tasks were sitting in was memory, so a
file that outlived its process can never be claimed by anyone. A consumer whose
own archives reference the paths (sutando's shim does) opts out at construction
with `media_retention_s`, which switches to age-based retention: nothing is
deleted on completion, and the startup sweep takes only what is older than that.

**The media directory is not auto-allowlisted for egress.** A consumer that
wants to re-upload what arrived adds it as an explicit root, so all egress
policy stays where a reviewer can see it in one place: the constructor's root
list.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import stat
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional, Tuple

from .envelope import Task, strip_to_fixpoint
from .transport import (
    AuthRejected,
    RelayHTTP,
    RelayHTTPError,
    ResponseTooLarge,
)

log = logging.getLogger(__name__)

#: The marker tag, as both broker transports write it (`media_marker.py` is the
#: one formatter the nio ingest and the AppService share, so they are
#: byte-identical by construction). Not configurable here: sparrow reads a
#: `REMOTE_MEDIA_MARKER` environment variable, and a library that reads the
#: environment behind its consumer's back is a surprise, not a feature.
MARKER_TAG = "ag2space-media"

#: The whole grammar. `[^\]]*` for the body, exactly as the reference consumer
#: matches it — which is also why a `]` inside a filename truncates the marker
#: and there is nothing this parser can do about it.
MARKER_RE = re.compile(r"\s*\[" + re.escape(MARKER_TAG) + r":([^\]]*)\]")

#: An *unterminated* marker — `[ag2space-media:` with no closing `]` before the
#: end of the body. `MARKER_RE` cannot match it, so before this existed the
#: whole tail (URL included) reached the consumer verbatim, in a body any room
#: member can write. The same hole the room-ops metadata stripper had; found in
#: both by review on 2026-08-20. Anchored to end-of-string: a `[` that is later
#: closed is an ordinary marker and belongs to `MARKER_RE`.
UNTERMINATED_RE = re.compile(r"\s*\[" + re.escape(MARKER_TAG) + r":[^\]]*\Z")

#: The gateway's own ceiling on the media route (25 MiB), so this client refuses
#: what the broker would refuse anyway — and refuses it before reading it.
CAP_BYTES = 25 * 1024 * 1024

#: One fetch attempt's socket timeout. Generous, because 25 MiB over a domestic
#: uplink is not fast; bounded, because F1's rule is that *nothing* waits for
#: ever, and this one runs where a Task is waiting on it.
FETCH_TIMEOUT_S = 30.0

#: The budgeted retry's pause. One retry, then the Task goes out with the
#: attachment marked failed — the fetcher is not a delivery service.
RETRY_DELAY_S = 1.0

#: What the on-disk name is built from. The stem is bounded rather than the
#: whole string, because the extension has to survive: the broker's *outbound*
#: media route guesses the mime from the filename, so a file that arrives as
#: `.png` and would be re-uploaded as `.png` renders as an image on the way back.
MAX_STEM = 100
MAX_EXT = 16

#: The bounded shapes of the hints. They are wire-supplied text on their way
#: into a filename and a consumer's prompt; none of these limits changes a
#: legitimate value.
MAX_HINT = 256
MAX_MIME = 128

#: Extension by mime, explicitly. `mimetypes.guess_extension` is tempting and
#: answers `.jpe` for `image/jpeg` on some interpreters, which is a working file
#: with a name nothing renders.
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg",
    "image/heic": ".heic", "application/pdf": ".pdf", "text/plain": ".txt",
    "audio/mpeg": ".mp3", "audio/ogg": ".ogg", "video/mp4": ".mp4",
    "application/zip": ".zip",
}

#: What the bytes are called when neither the fetch nor the marker says.
DEFAULT_MIME = "application/octet-stream"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# The reasons a fetch failed, in the second person a consumer can repeat to the
# room. They are written here rather than derived from an exception because an
# exception's text carries the URL, and the URL is the one thing that must not
# cross this seam.
UNFETCHABLE = "its address is not one this client can fetch"
UNREACHABLE = "it could not be fetched"
TOO_LARGE = "it is larger than this client will fetch"
UNSAVEABLE = "it was fetched but could not be saved"
REJECTED_BEARER = "the gateway would not accept this client's credential for it"


class Marker(NamedTuple):
    """One `[ag2space-media:]` marker, as written. Only `url` is load-bearing."""

    url: str
    mime: str = ""
    name: str = ""
    kind: str = ""
    #: The `size=` hint, or 0. Never used as a ceiling — the cap is enforced by
    #: reading `cap + 1` bytes, because this number is the sender's client's
    #: opinion and arrives unsigned.
    size: int = 0


class Attachment(NamedTuple):
    """One resolved attachment, as the consumer sees it.

    There is no URL on this object and no marker text: what the consumer gets is
    a path it can open, or an honest sentence about why it cannot.
    """

    #: The local file, or `""` when the fetch failed.
    path: str = ""
    #: From the fetch's `Content-Type` — the marker's hint only when the fetch
    #: said nothing.
    mime: str = DEFAULT_MIME
    #: The sender's filename as a *hint*: sanitised, never used to name the file
    #: on disk, and quite possibly truncated at a space by the marker grammar.
    name: str = ""
    #: The Matrix msgtype the marker carried (`m.image`, `m.file`, …).
    kind: str = ""
    #: Bytes on disk, or 0.
    size: int = 0
    ok: bool = False
    #: Room-facing, and empty when `ok`.
    reason: str = ""


def strip_markers(body: str) -> Tuple[str, Tuple[Marker, ...]]:
    """`(body without any media marker, the markers it carried)`.

    Called on the poll thread for every task, because the invariant is about the
    *queue*: no marker reaches a consumer, whether or not its fetch succeeds,
    whether or not there was anything to fetch. A body that was only a marker
    degrades to empty — the attachment tuple is where that task's content is.

    Today's wire emits at most one marker per task (a multi-file upload is
    several Matrix events, so several tasks). This returns all of them anyway:
    the Task type carries 0..N, and a parser that could only count to one would
    be the thing to change if that ever stops being true.
    """
    if not body or MARKER_TAG not in body:
        return body, ()
    found: List[Marker] = []
    for match in MARKER_RE.finditer(body):
        marker = _read_marker(match.group(1))
        if marker is not None:
            found.append(marker)
    # The strip is unconditional: a marker with nothing usable inside it names
    # no attachment and is still a marker, so it still must not reach anyone.
    # The unterminated tail goes second, after the well-formed markers are out,
    # so a body carrying one of each loses both — and the pair runs to a
    # fixpoint, because one pass can re-form the marker it just took apart
    # (`[[ag2space-media: a]ag2space-media: mxc://...]`, 2026-08-21 review).
    #
    # The markers above are read from the body as it arrived and from nowhere
    # else: a marker that only exists *because* a strip re-formed it is deleted
    # rather than honoured. Reading later passes would turn a body someone
    # composed into a URL this client fetches, which is a wider surface than
    # the bug being closed.
    stripped, _ = strip_to_fixpoint(body, MARKER_RE, UNTERMINATED_RE)
    return stripped.strip(), tuple(found)


def _read_marker(inner: str) -> Optional[Marker]:
    """The inside of one marker: a URL, then `key=value` hints.

    The URL is the first whitespace-delimited run, and the hints are read from
    what follows it — never from the URL itself, which can carry a `name=` of
    its own in its query string.
    """
    inner = (inner or "").strip()
    if not inner:
        return None
    parts = inner.split(None, 1)
    url = parts[0]
    tail = parts[1] if len(parts) > 1 else ""
    return Marker(
        url=url,
        mime=_mime_hint(_attr(tail, "mime")),
        name=_hint(_attr(tail, "name")),
        kind=_hint(_attr(tail, "kind")),
        size=_size_hint(_attr(tail, "size")),
    )


def _attr(tail: str, key: str) -> str:
    """One `key=<non-space run>` hint, exactly as the reference consumer reads it.

    Deliberately as permissive-and-lossy as the writer is: this is where a
    filename with a space in it loses its second half, and matching that
    behaviour is how the two ends agree about what the marker said.
    """
    found = re.search(r"\b" + re.escape(key) + r"=([^\s\]]+)", tail)
    return found.group(1) if found else ""


def _hint(value: str) -> str:
    """Wire text as a bounded, control-character-free hint."""
    return _CONTROL_RE.sub("", value or "")[:MAX_HINT]


def _mime_hint(value: str) -> str:
    """A mime type as a bare `type/subtype`, lowercased, parameters dropped."""
    text = _CONTROL_RE.sub("", value or "").split(";")[0].strip().lower()
    return text[:MAX_MIME] if "/" in text else ""


def _size_hint(value: str) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return 0
    return size if 0 <= size < 1 << 62 else 0


def fetchable(url: str) -> bool:
    """True when this is an `http(s)` URL this client can even try.

    The guard rather than the fetch, and it is the whole of "malformed URLs
    never crash intake": `.port` raises `ValueError` at *access* time for an
    authority like `host:bad`, `file:///etc/passwd` is not a thing this client
    fetches at all, and both arrive from a room message.
    """
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        split = urllib.parse.urlsplit(url)
        _ = split.port  # raises here, not at request time
    except ValueError:
        return False
    return split.scheme in ("http", "https") and bool(split.hostname)


class MediaStore:
    """The directory fetched bytes land in, and the rule for when they leave.

    **This directory is the library's.** The sweep deletes files it did not
    write, because it cannot tell them apart and because the alternative — a
    ledger of which file belonged to which dead process — is durable state
    invented to avoid deleting a file the consumer was told to expect at a path
    it no longer has. Point it somewhere of its own.
    """

    def __init__(self, directory, retention_s: Optional[float] = None):
        self.path = Path(directory).expanduser()
        #: `None` is delete-on-complete. A number is age-based retention, in
        #: seconds, and turns the completion delete off entirely.
        self.retention_s = None if retention_s is None else max(0.0, float(retention_s))
        self._lock = threading.Lock()
        self._held: dict = {}

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        mode = ("delete-on-complete" if self.retention_s is None
                else f"{self.retention_s:.0f}s retention")
        return f"<MediaStore {self.path} {mode}>"

    @property
    def deletes_on_complete(self) -> bool:
        return self.retention_s is None

    def ensure(self) -> Path:
        """Create the directory, privately. Attachments are one bearer's mail."""
        self.path.mkdir(parents=True, exist_ok=True)
        try:
            self.path.chmod(0o700)
        except OSError:  # pragma: no cover — filesystems without POSIX modes
            pass
        return self.path

    def save(self, wire_id: str, data: bytes, name: str, mime: str) -> str:
        """Write `data` under a name of this store's choosing; return the path.

        Exclusive creation through `mkstemp`, so two attachments that share a
        filename — which the sender's client hands out freely — get two files
        rather than one of them silently overwriting the other. The name hint
        seasons the prefix and never *is* the name.
        """
        self.ensure()
        stem, suffix = _on_disk_name(name, mime)
        handle, written = tempfile.mkstemp(prefix=stem + "-", suffix=suffix,
                                           dir=str(self.path))
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
        except BaseException:
            try:
                os.unlink(written)
            except OSError:  # pragma: no cover — nothing to clean up
                pass
            raise
        self.keep(wire_id, written)
        return written

    def keep(self, wire_id: str, path: str) -> None:
        """Remember that this task's answer retires this file."""
        with self._lock:
            self._held.setdefault(wire_id, []).append(path)

    def release(self, wire_id: str) -> int:
        """The task is answered: delete its files, or don't, per the mode.

        Called from `complete`/`reject`, which is the moment the consumer is
        done with the Task. Under age-based retention this only forgets them —
        the consumer's own archives are pointing at those paths.
        """
        with self._lock:
            held = self._held.pop(wire_id, [])
        if not self.deletes_on_complete:
            return 0
        removed = 0
        for path in held:
            if _unlink(path):
                removed += 1
        if removed:
            log.debug("released %d media file(s) for %s", removed, wire_id)
        return removed

    def sweep(self) -> int:
        """Delete what an earlier run left behind. Returns how many.

        Under delete-on-complete every file here at startup is an orphan: the
        queue its Task was waiting in was memory, so nobody alive can complete
        it and nobody will ever come back for the bytes. Under age-based
        retention only what has aged out goes.

        Never recursive, never follows a symlink, and never raises: a sweep that
        stopped a client from starting would be a housekeeping chore with a
        production outage attached.
        """
        cutoff = None if self.retention_s is None else time.time() - self.retention_s
        removed = 0
        try:
            entries = list(self.path.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                info = entry.lstat()
            except OSError:  # pragma: no cover — it went away by itself
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if cutoff is not None and info.st_mtime > cutoff:
                continue
            if _unlink(str(entry)):
                removed += 1
        if removed:
            log.info("swept %d file(s) from %s that no live task can claim",
                     removed, self.path)
        return removed


def _unlink(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def _on_disk_name(name: str, mime: str) -> Tuple[str, str]:
    """`(stem, suffix)` for a file this client is about to write.

    The extension comes from the *mime* first — which came from the fetch — and
    falls back to whatever the name hint ended in. A hint that was truncated at
    a space keeps its extension only if the space was before it; that is the
    grammar's doing, and the file is still perfectly readable either way.
    """
    suffix = _EXT_BY_MIME.get((mime or "").lower(), "")
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", name or "").strip("._-")
    if not suffix and "." in stem:
        tail = re.sub(r"[^A-Za-z0-9]", "", stem.rsplit(".", 1)[1])[:MAX_EXT]
        suffix = "." + tail if tail else ""
    if suffix and stem.lower().endswith(suffix.lower()):
        stem = stem[: -len(suffix)]
    stem = stem.replace(".", "_")[:MAX_STEM].strip("_-")
    return (stem or "attachment"), suffix


class MediaIngress:
    """The stage between the poll and the delivery queue.

    Owns one thread, one unbounded hand-off queue, and the store. `accept` is
    the only thing the wire loop calls; everything else here is either the
    thread's own business or a test's.
    """

    def __init__(
        self,
        http: RelayHTTP,
        store: MediaStore,
        deliver: Callable[[Task], None],
        cap_bytes: int = CAP_BYTES,
        timeout: float = FETCH_TIMEOUT_S,
        retry_delay: float = RETRY_DELAY_S,
        on_auth_rejected: Optional[Callable[[AuthRejected], None]] = None,
    ):
        self.http = http
        self.store = store
        self.deliver = deliver
        self.cap_bytes = int(cap_bytes)
        self.timeout = float(timeout)
        self.retry_delay = float(retry_delay)
        self._on_auth_rejected = on_auth_rejected
        self._pending: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._guard = threading.Lock()

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<MediaIngress {self.store.path} pending={self._pending.qsize()}>"

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> "MediaIngress":
        """Open the media directory, sweep last run's orphans, run the thread."""
        self.store.ensure()
        self.store.sweep()
        self._ensure_thread()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop fetching, without waiting out a download.

        Anything still queued is dropped: those Tasks were never delivered, so
        nobody is holding one, and the broker re-serves what it is still owed.
        A fetch already in flight is *not* waited out — the thread is a daemon
        with its own socket timeout, and all it can still do is put a Task on a
        queue nobody is reading and leave a file for the next run's sweep. A
        `stop()` that could take thirty seconds to return is a `stop()` a
        supervisor learns to kill instead.
        """
        self._stop.set()
        self._pending.put(None)  # wake a thread parked on an empty queue
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _ensure_thread(self) -> None:
        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="ag2-relay-media", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            pending = self._pending.get()
            if pending is None:
                continue  # the wake-up `stop` puts on the queue
            if self._stop.is_set():
                return  # dropped: never delivered, so nobody is waiting on it
            try:
                self.deliver(self.resolve(pending))
            except Exception:  # noqa: BLE001 — a fetcher thread that dies takes
                # every later attachment with it, so nothing escapes this loop.
                # The Task still goes out: the attachment is the part that
                # failed, and its placeholder already says so.
                log.exception("media ingress failed for task %s", pending[0].id)
                try:
                    self.deliver(pending[0])
                except Exception:  # noqa: BLE001 — a consumer's queue hook
                    log.exception("delivering task %s after a media failure",
                                  pending[0].id)

    # --- what the wire loop calls ------------------------------------------

    def accept(self, task: Task) -> bool:
        """Take one polled Task. Returns whether it went to the fetcher.

        Runs on the poll thread, so it does exactly two things that cost
        nothing — a regex, and a `put` on an unbounded queue — and never blocks
        (F1). The marker strip happens here, for every task, so the "no marker
        crosses the seam" invariant does not depend on the fetch.
        """
        body, markers = strip_markers(task.body)
        task.body = body
        if not markers:
            self.deliver(task)
            return False
        task.attachments = tuple(
            Attachment(mime=m.mime or DEFAULT_MIME, name=m.name, kind=m.kind,
                       size=m.size, ok=False, reason=UNREACHABLE)
            for m in markers
        )
        self._ensure_thread()
        self._pending.put((task, markers))
        return True

    def release(self, wire_id: str) -> int:
        """The consumer answered this task: its files are done (delete-on-complete)."""
        return self.store.release(wire_id)

    # --- the fetch ---------------------------------------------------------

    def resolve(self, pending) -> Task:
        """Resolve one Task's markers into attachments. Never raises.

        Synchronous, and public because that is the whole of this stage: the
        thread is scheduling, this is behaviour, and a test that wants the
        behaviour should not have to run a thread to see it.
        """
        task, markers = pending
        task.attachments = tuple(self._one(task.id, marker) for marker in markers)
        for attachment in task.attachments:
            if attachment.ok:
                log.info("task %s: attachment saved to %s (%s, %d bytes)",
                         task.id, attachment.path, attachment.mime, attachment.size)
            else:
                log.warning("task %s: attachment not available — %s",
                            task.id, attachment.reason)
        return task

    def _one(self, wire_id: str, marker: Marker) -> Attachment:
        base = Attachment(mime=marker.mime or DEFAULT_MIME, name=marker.name,
                          kind=marker.kind, size=marker.size)
        if not fetchable(marker.url):
            # Never a crash and never a fetch: a room message can put anything
            # in this field, and `file:///etc/passwd` is the reason the scheme
            # is checked rather than assumed.
            log.warning("task %s: refusing to fetch a media address that is not "
                        "an http(s) URL", wire_id)
            return base._replace(ok=False, reason=UNFETCHABLE, size=0)

        fetched, reason = self._fetch(wire_id, marker.url)
        if fetched is None:
            return base._replace(ok=False, reason=reason, size=0)

        mime = _mime_hint(fetched.content_type) or marker.mime or DEFAULT_MIME
        try:
            path = self.store.save(wire_id, fetched.body, marker.name, mime)
        except OSError as exc:
            log.warning("task %s: media could not be saved (%s)", wire_id, exc)
            return base._replace(ok=False, reason=UNSAVEABLE, size=0)
        return Attachment(path=path, mime=mime, name=marker.name, kind=marker.kind,
                          size=len(fetched.body), ok=True, reason="")

    def _fetch(self, wire_id: str, url: str):
        """The attempt and its one budgeted retry. `(Fetched, "")` or `(None, why)`.

        What is *not* retried is as considered as what is: an oversize file will
        not have shrunk, and a rejected bearer will not have been fixed in a
        second — that one belongs to auth recovery, which is told about it
        rather than being made to notice from a log line (C8).
        """
        attempts = 2
        reason = UNREACHABLE
        for attempt in range(attempts):
            try:
                return self.http.fetch(url, self.cap_bytes, timeout=self.timeout), ""
            except ResponseTooLarge:
                log.warning("task %s: attachment is over the %d-byte ceiling",
                            wire_id, self.cap_bytes)
                return None, TOO_LARGE
            except AuthRejected as exc:
                log.warning("task %s: the media fetch was refused as HTTP %s",
                            wire_id, exc.status)
                if self._on_auth_rejected is not None:
                    try:
                        self._on_auth_rejected(exc)
                    except Exception:  # noqa: BLE001 — a hook is not a gate
                        log.exception("the media auth-rejection hook raised")
                return None, REJECTED_BEARER
            except RelayHTTPError as exc:
                # This route answers 502 for *everything*, membership refusals
                # included, so the status is a fact to report and never a
                # permission verdict to act on.
                reason = f"{UNREACHABLE} (HTTP {exc.status})"
                last = attempt == attempts - 1
                log.info("task %s: media fetch answered HTTP %s%s",
                         wire_id, exc.status, "" if last else " — trying once more")
            except Exception as exc:  # noqa: BLE001 — a network is a network
                reason = UNREACHABLE
                last = attempt == attempts - 1
                log.info("task %s: media fetch failed (%s: %s)%s", wire_id,
                         type(exc).__name__, exc, "" if last else " — trying once more")
            if attempt < attempts - 1 and self.retry_delay > 0:
                self._stop.wait(self.retry_delay)
        return None, reason
