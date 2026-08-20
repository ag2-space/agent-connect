"""An answer on its way to the room: markers read, files sent, body assembled.

This is where the three outbound pieces meet. A consumer hands over whatever its
agent produced; what comes back is the body to POST to `/v1/results` and a
record of what happened on the way.

The order is not arbitrary. **Uploads happen before the result POST**, because a
file that could not be sent has to be said out loud *in the answer* — appended
as `[attachment not sent: …]` rather than dropped into a log the person who
asked will never read. And that ordering is exactly what creates the duplicate
this module then has to prevent:

**F6 — a result-POST retry must not re-upload.** `POST /v1/results` fails
transiently; the client retains the result and retries (F5). Without a ledger,
every retry uploads the media again, and the room fills with copies of the same
chart. So each `(task, path)` pair uploaded in this process is remembered, and
a second `prepare` for the same task returns the same body having sent nothing.
The ledger is in-memory on purpose: it guards a retry loop, not a restart, and
a restart's duplicate is the broker's `duplicate: true` to absorb.

**H1 — a skip marker is still a POST.** `[no-send]`, `[REPLIED]` and
`[deduped: <id>]` complete the lease with no user-visible message. The temptation
is to skip the POST as well, since nothing will be rendered; doing that leaves
the lease to expire and the task to be re-served, for ever. So a skip body goes
on the wire **verbatim** — the deliverer reads the marker and posts nothing.

**The ledger is guarded by a lock, and the lock claims less than it looks.** The
spec has agent-connect calling this library from an executor, so two threads can
be inside `prepare` at once. On today's CPython the unlocked version happened to
be safe — every operation on the ledger was one dict operation, and the GIL does
not let go inside one — which is a property of an interpreter, not of this code,
and it is the property a free-threaded build removes. The lock buys the
guarantee outright, costs an uncontended acquire twice per upload, and means
nobody has to re-derive that argument when they add the third ledger operation.

What it deliberately does **not** do is hold across an upload. Two *genuinely
overlapping* `prepare` calls for one `(task, path)` still both miss the ledger
and both upload. That overlap is not F6's scar — F6 is a failed POST retried
afterwards, in sequence — and the wire loop leases a task to one worker at a
time, so reaching it means the consumer prepared one task twice at once.
Serialising every upload behind one lock to close it would make one slow
attachment delay every other task's answer: a worse trade than a duplicate
nobody has seen.

**H3 — the redirect goes back on.** The parser strips `[channel: <room>]` for
the consumer's benefit; the broker's deliverer is what actually performs the
move, so it is re-stitched onto the first line of the POSTed body. Unless
`[dm-only]` was present, in which case it was stripped and stays stripped: the
privacy guard suppresses the redirect, and re-stitching a marker whose action
was suppressed would hand the private body to the shared room anyway.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections import OrderedDict
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import markers
from .roomops import RoomOps

log = logging.getLogger(__name__)

#: How many files one answer may carry. Not policy — the point past which a
#: reply has stopped being a reply.
MAX_FILES = 10

#: How many tasks' upload ledgers to keep when nobody calls `forget`. Bounded
#: so a consumer that never retires an id cannot turn this into a leak.
LEDGER_TASKS = 512

#: What the room is told about a file that did not go. In the body, not as its
#: own message: someone asked for a file *with* an answer, and "you are not
#: getting it" belongs beside the answer they did get.
NOT_SENT = "[attachment not sent: {name} ({why})]"

#: What a reply says when the agent sent files and no words with them.
ONLY_FILES = "📎 Attached: {names}."

TOO_MANY = "no more than {limit} files are sent with one reply"

#: Control characters and the brackets a marker is made of, scrubbed from any
#: path repeated back into a room — a refusal must not be able to forge one.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PreparedResult(NamedTuple):
    """One answer, ready for `POST /v1/results`."""

    #: Exactly what to put in the `body` field. For a skip this is the original
    #: text, markers and all.
    body: str
    #: The skip reason (`no-send` / `REPLIED` / `deduped`), or `""`.
    skip: str = ""
    #: The holder id a `[deduped: <id>]` named.
    skip_id: str = ""
    #: True when `[dm-only]` appeared anywhere — and therefore no redirect ran.
    dm_only: bool = False
    #: The room a `[channel:]` named, already re-stitched into `body`.
    redirect: str = ""
    #: Filenames that reached the room on this call *or an earlier one* for the
    #: same task.
    uploaded: Tuple[str, ...] = ()
    #: Room-facing refusal sentences, already appended to `body`.
    refused: Tuple[str, ...] = ()

    @property
    def silent(self) -> bool:
        """Completes the lease without a user-visible post (H1)."""
        return bool(self.skip)


class Outbound:
    """Marker parsing, egress and the result body, for one client.

    Built with the `RoomOps` that owns the egress allowlist. Without one, an
    answer's attachment markers are stripped and refused in-band — the text
    still lands, which is the same degradation Room Op failure gets (I1).
    """

    def __init__(
        self,
        room_ops: Optional[RoomOps] = None,
        max_files: int = MAX_FILES,
        ledger_tasks: int = LEDGER_TASKS,
    ):
        self.room_ops = room_ops
        self.max_files = int(max_files)
        self._ledger: OrderedDict[str, Dict[str, str]] = OrderedDict()
        self._ledger_tasks = int(ledger_tasks)
        #: Guards the ledger's shape, not the uploads. See the module docstring.
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Outbound {len(self._ledger)} task(s) with media>"

    def prepare(
        self,
        task_id: str,
        room_id: str,
        body: Optional[str],
        base_dir: Optional[object] = None,
    ) -> PreparedResult:
        """Read the markers, send the files, and build the body to POST.

        Idempotent for a given `(task_id, body)`: calling it again — which is
        what a retried result POST does — re-derives the same body without
        re-uploading anything (F6). Never raises: an answer is not lost because
        a file could not be attached.
        """
        parsed = markers.parse(body)

        # A skip is terminal, and its body goes on the wire unchanged. The
        # marker is *for* the deliverer; stripping it here would turn "record
        # but do not post" into an empty message posted to the room.
        if parsed.skip:
            return PreparedResult(
                body=body or "", skip=parsed.skip, skip_id=parsed.skip_id,
            )

        text = parsed.body
        uploaded: List[str] = []
        refused: List[str] = []
        for index, named in enumerate(parsed.attachments):
            shown = _display(named)
            if index >= self.max_files:
                refused.append(NOT_SENT.format(
                    name=shown, why=TOO_MANY.format(limit=self.max_files)))
                continue
            done, why = self._send(task_id, room_id, named, base_dir)
            if done:
                uploaded.append(done)
            else:
                refused.append(NOT_SENT.format(name=shown, why=why))

        if not text and uploaded:
            # An answer that is only files still needs words, or the room gets
            # an empty message with attachments hanging off it.
            text = ONLY_FILES.format(names=", ".join(uploaded))
        if refused:
            text = (text + "\n\n" + "\n".join(refused)).strip() if text \
                else "\n".join(refused)

        if parsed.redirect:
            text = markers.restitch(text, parsed.redirect)

        return PreparedResult(
            body=text,
            dm_only=parsed.dm_only,
            redirect=parsed.redirect,
            uploaded=tuple(uploaded),
            refused=tuple(refused),
        )

    def forget(self, task_id: str) -> None:
        """Drop a task's upload ledger — call it when its result POST succeeds.

        Only success retires an id (F5), so only success may forget what it
        uploaded. Forgetting on failure is precisely the bug F6 describes.
        """
        with self._lock:
            self._ledger.pop(task_id, None)

    def already_sent(self, task_id: str) -> Tuple[str, ...]:
        """What this task has already put in its room. For tests and status."""
        with self._lock:
            return tuple(sorted(set(self._ledger.get(task_id, {}).values())))

    # -- internals ----------------------------------------------------------

    def _send(self, task_id, room_id, named, base_dir):
        """Upload one named path, once. Returns `(filename, "")` or `("", why)`."""
        key = _key(named)
        with self._lock:
            already = (self._ledger.get(task_id) or {}).get(key)
        if already:
            # The retry case. The file is in the room; saying so again is the
            # duplicate this ledger exists to prevent.
            log.debug("task %s: %s already sent, not uploading again", task_id, key)
            return already, ""

        if self.room_ops is None:
            return "", _NO_ROOM_OPS

        try:
            result = self.room_ops.upload(room_id, named, base_dir=base_dir)
        except Exception:  # noqa: BLE001 — the last frame before the consumer
            # `RoomOps.upload` promises not to raise (I1) and this is what makes
            # the promise's failure cost an attachment rather than the poller.
            # `prepare` says "Never raises" in its docstring; a docstring is not
            # an enforcement mechanism, and the loop above is a bearer's only one.
            log.exception("room op: upload raised for %r", str(named)[:200])
            return "", _UPLOAD_RAISED
        if not result.ok:
            return "", result.reason
        self._remember(task_id, key, result.filename)
        if result.path:
            # A second key for the resolved path, so two spellings of one file
            # in one body do not put it in the room twice.
            self._remember(task_id, _key(result.path), result.filename)
        return result.filename, ""

    def _remember(self, task_id: str, key: str, filename: str) -> None:
        with self._lock:
            sent = self._ledger.get(task_id)
            if sent is None:
                sent = {}
                self._ledger[task_id] = sent
                while len(self._ledger) > self._ledger_tasks:
                    self._ledger.popitem(last=False)
            sent[key] = filename


_NO_ROOM_OPS = "this client is not configured to send files"
_UPLOAD_RAISED = "this client could not send it"


def _key(named: str) -> str:
    """How a `(task, path)` pair is remembered across retries.

    Normalised but *not* resolved: what repeats across a retry is the marker
    text the agent wrote, because the retry re-posts the same body. The resolved
    path is recorded alongside it after a successful upload, which catches the
    other case — one file named two ways in one answer.
    """
    return os.path.normpath(os.path.expanduser((named or "").strip()))


def _display(named: str) -> str:
    """A path as the agent named it, safe to repeat in a room.

    Scrubbed of control characters and of the brackets a marker is made of, so
    a refusal cannot forge one, and truncated: a person needs to recognise which
    file was refused, not to read an essay.
    """
    text = _CONTROL.sub(" ", named or "").replace("[", "(").replace("]", ")")
    return re.sub(r"\s+", " ", text).strip()[:160] or "an unnamed file"
