"""Room Ops: speaking in a room *as* the agent identity.

A Room Op is an action the broker performs in a room on this bearer's behalf —
post, edit, react, put a file in it. They are the cosmetic half of the
conversation, and everything here is arranged around one sentence:

**A Room Op failure must never reach the consumer's loop (I1).** A room that
cannot be spoken to is a room whose answer arrives the plain way, as the task's
result through `POST /v1/results`. Losing the answer to a decoration is never
acceptable, so no method here raises: they return `None`, `False`, or a failed
`UploadResult`, and the caller degrades. The scar is literal — an uncaught
room-op exception in the worker loop killed a bearer's only poller over a
placeholder message.

**The latch is time-gated, not for the process lifetime (the spec's decision on
I1's open question).** The original latch turned room ops off until restart,
which is right about the immediate problem (a per-task retry adds its timeout to
every answer for nothing) and wrong about the long run: a permanent latch does
not self-heal after a broker deploy, which is exactly why the ack cooldown (F4)
is time-gated. So one failure buys `COOLDOWN_S` of silence, and then the client
tries again.

**401/403 still escape sideways (C8).** They do not raise — I1 forbids that —
but a revoked bearer is not one more optional failure, so an `AuthRejected` is
handed to the `on_auth_rejected` hook before the call returns. Auth recovery
sees it; the worker loop does not.

Three wire details that each cost a debugging session, kept here so they cost
nobody another one:

- **The payload key is `room_id`.** A `room` key is ignored and the op fails
  with a 400.
- **The posted event id comes back under several spellings** — `event_id`,
  `eventId`, `id`, sometimes nested under `result` or `data`. Without it there
  is nothing to edit and the placeholder→answer ladder collapses into two
  separate messages, so all of them are accepted.
- **`op:edit` caps at 4000 characters** (413 beyond it). A longer reply goes
  through `/v1/results`, which chunks it — so this refuses locally, *without*
  spending the cooldown: a body we declined to send is not a broker that failed.

And one that is a rule rather than a detail: **the worker does not react to the
inbound message.** The broker auto-🫡s on the first `/ack`; a worker reaction
doubles the eyes. `react` exists for a consumer's own deliberate use, and
refuses any event id the loop has registered as an intake event.

There is no base URL in this file (I3). It comes from the credential, through
`RelayHTTP`.
"""
from __future__ import annotations

import base64
import logging
import re
import time
import urllib.parse
from collections import OrderedDict
from typing import Callable, Dict, NamedTuple, Optional, Sequence

from .egress import ApprovedFile, EgressAllowlist, EgressRefused
from .markers import ROOM_ID_RE
from .transport import AuthRejected, RelayHTTP

log = logging.getLogger(__name__)

#: The generic room-op endpoint.
ROOM_PATH = "/v1/room"

#: Media goes to the room-scoped route, not to `/v1/results` — which is why
#: every task's origin room has to be known at upload time (F7).
MEDIA_PATH = "/v1/rooms/{room}/media"

#: How long one failure buys. Long enough that a broken broker is not retried
#: per task; short enough that a deploy heals it without a restart.
COOLDOWN_S = 300.0

#: `op:edit` is a single-event verb and the broker answers 413 past this.
EDIT_MAX_CHARS = 4000

#: The broker stamps at most this many mxids into `m.mentions.user_ids`.
MAX_MENTIONS = 10

#: A hand-typed mxid in body text does not notify anyone; only this field does.
_MXID_RE = re.compile(r"^@[^\s:@]+:[^\s:@/]+$")

# A room id is broker-supplied, but it is also about to be a URL path segment
# and a JSON value, so it is checked rather than trusted — against
# `markers.ROOM_ID_RE`, imported above. That is where a room id is also *read*,
# out of a `[channel:]` redirect, and the two are the same grammar. A second
# copy of it here is precisely the drift H2 is a scar about.

#: How many intake event ids to remember. Bounded because it is a guard, not a
#: ledger: the ids that matter are the recent ones.
_INTAKE_MEMORY = 512


class UploadResult(NamedTuple):
    """What became of one file.

    `reason` is room-facing when `ok` is false — it is the sentence that gets
    appended to the answer, because a file that silently does not arrive is
    indistinguishable from an agent that ignored the request.
    """

    ok: bool
    mxc: str = ""
    filename: str = ""
    path: str = ""
    reason: str = ""


class RoomOps:
    """The broker's room endpoint, as the four ops a consumer needs.

    Shared across tasks — the cooldown is why. A broker that is not doing room
    ops for one task is not doing them for the next one either, and finding that
    out per task costs every answer the timeout.

    **Two attributes are sealed, and the object has no `__dict__`.** The
    allowlist is one of them: an `EgressAllowlist` that refuses to be widened is
    worth nothing if the reference *to* it is a plain attribute, because then
    the attacker does not widen the allowlist, they replace it — one assignment,
    same result, one layer up. The authenticated HTTP session is the other:
    `RelayHTTP` is a bearer token with a `.post`, and reaching it from here
    would be the bytes API this library refuses to have, spelled
    `ops.http.post("/v1/rooms/X/media", {"content_b64": ...})`. So it is private
    and it is sealed, and the only route to a room's media is `upload`, which
    goes through the allowlist.
    """

    #: Slots, so the seal below has no `__dict__` to be walked around.
    __slots__ = (
        "_http", "_allowlist", "cooldown_s", "timeout", "_clock",
        "_on_auth_rejected", "_blocked_until", "_intake",
    )

    #: Written once, in `__init__`, and never again. The rest of the state here
    #: is ordinary — the cooldown deadline moves by design.
    _SEALED = frozenset({"_http", "_allowlist"})

    def __init__(
        self,
        http: RelayHTTP,
        allowlist: Optional[EgressAllowlist] = None,
        cooldown_s: float = COOLDOWN_S,
        timeout: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
        on_auth_rejected: Optional[Callable[[AuthRejected], None]] = None,
    ):
        self._http = http
        self._allowlist = allowlist
        self.cooldown_s = float(cooldown_s)
        self.timeout = float(timeout)
        self._clock = clock
        self._on_auth_rejected = on_auth_rejected
        self._blocked_until = 0.0
        self._intake: OrderedDict[str, bool] = OrderedDict()

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<RoomOps available={self.available}>"

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._SEALED and hasattr(self, name):
            raise AttributeError(
                f"{name} is fixed at construction; build another RoomOps rather "
                f"than repointing this one"
            )
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self._SEALED:
            raise AttributeError(f"{name} is fixed at construction")
        object.__delattr__(self, name)

    @property
    def allowlist(self) -> Optional[EgressAllowlist]:
        """Egress runs through this or not at all — and it is read-only.

        `None` means this consumer configured no roots, and every upload is
        refused: the fail-closed reading of "no allowlist", and the only safe
        one. Read-only for the same reason the roots inside it are fixed at
        construction — an allowlist that can be *replaced* at runtime is an
        allowlist an attacker only has to reach once.

        Read-only means the private name too. A property with no setter stops
        `ops.allowlist = wider` and nothing else; `ops._allowlist = wider` is
        the same attack with four more characters, and `ops.__dict__[...]` is
        the same attack again. The class seals the private name and carries
        `__slots__` so there is no third spelling.
        """
        return self._allowlist

    @property
    def available(self) -> bool:
        """False while the cooldown from the last failure is still running."""
        return self._clock() >= self._blocked_until

    @property
    def cooldown_remaining(self) -> float:
        """Seconds of silence left — for the status snapshot, so a supervisor
        can tell "room ops are off" from "room ops are off for ever"."""
        return max(0.0, self._blocked_until - self._clock())

    def note_intake_event(self, event_id: str) -> None:
        """Remember an event this client was *served*, so nothing reacts to it.

        Called by the wire loop when a task is accepted, carrying the task's
        `source_event_id`. The broker already put 🫡 on that message when the
        task was acked; a second one from here is the room seeing double.
        """
        if not event_id:
            return
        self._intake[event_id] = True
        while len(self._intake) > _INTAKE_MEMORY:
            self._intake.popitem(last=False)

    # -- the ops ------------------------------------------------------------

    def message(
        self,
        room_id: str,
        body: str,
        mentions: Optional[Sequence[str]] = None,
    ) -> Optional[str]:
        """Post as the agent identity; return the event id, or `None`.

        The event id is the whole point of the return value: without it there is
        nothing to edit later, and the ladder collapses into a stream of
        separate messages.
        """
        if not _valid_room(room_id) or not (body or "").strip():
            log.warning("room op: refusing a message with no room or no body")
            return None
        payload: Dict[str, object] = {
            "op": "message", "room_id": room_id, "body": body,
        }
        stamped = _mentions(mentions)
        if stamped:
            payload["mentions"] = stamped
        answer = self._call(payload)
        if answer is None:
            return None
        event_id = _event_id(answer)
        if not event_id:
            # The message may well have landed. What did not is the ability to
            # edit it, so the ladder must not be started from here.
            log.warning("room op: message posted but the broker named no event id")
            self._trip("message returned no event id")
            return None
        return event_id

    def edit(self, room_id: str, event_id: str, body: str) -> bool:
        """Replace the body of a message this agent identity posted."""
        if not _valid_room(room_id) or not event_id or not (body or "").strip():
            return False
        if len(body) > EDIT_MAX_CHARS:
            # A local refusal, and deliberately not a cooldown: the broker did
            # not fail, we declined to ask. The reply goes through
            # `/v1/results`, whose render path chunks it.
            log.info(
                "room op: not editing with %d chars (cap %d) — this reply goes "
                "through /v1/results", len(body), EDIT_MAX_CHARS,
            )
            return False
        return self._call(
            {"op": "edit", "room_id": room_id, "event_id": event_id, "body": body}
        ) is not None

    def react(self, room_id: str, event_id: str, key: str) -> bool:
        """React to an event — never to one this client was served.

        The intake reaction belongs to the broker (I2). A reaction from here on
        a message the broker already acked puts two of the same emoji on it,
        which reads as two agents answering.
        """
        if not _valid_room(room_id) or not event_id or not key:
            return False
        if event_id in self._intake:
            log.info(
                "room op: not reacting to %s — the broker places the intake "
                "reaction, and a second one doubles it", event_id,
            )
            return False
        return self._call(
            {"op": "react", "room_id": room_id, "event_id": event_id, "key": key}
        ) is not None

    def upload(
        self,
        room_id: str,
        path: object,
        caption: Optional[str] = None,
        base_dir: Optional[object] = None,
    ) -> UploadResult:
        """Put an **allowlisted path** in a room. There is no bytes overload.

        The signature is the security property: this method resolves the path
        through the allowlist itself and reads the descriptor the allowlist
        judged. Nothing in this library — public or private — accepts bytes to
        upload, because a surface that did would let any caller read a file the
        allowlist would have refused and hand the contents over anyway.

        Like everything else here it raises nothing (I1). Not an `OSError` from
        a mount that went away mid-read, not a `MemoryError` from base64'ing 24
        MiB on a small host: an attachment is worth an attachment, never the
        consumer's loop, and the failure is said out loud in the answer instead.
        """
        if self._allowlist is None:
            return UploadResult(False, reason=_NO_ALLOWLIST)
        if not _valid_room(room_id):
            return UploadResult(False, reason=_NO_ROOM)
        if not self.available:
            return UploadResult(False, reason=_COOLING)

        try:
            approved = self._allowlist.open(path, base_dir=base_dir)
        except EgressRefused as refusal:
            log.info("egress refused %r: %s", str(path)[:200], refusal.reason)
            return UploadResult(False, reason=refusal.reason)
        except Exception:  # noqa: BLE001 — I1: nothing here reaches the loop
            log.exception("egress raised judging %r", str(path)[:200])
            return UploadResult(False, reason=_UNREADABLE)

        try:
            return self._put_media(room_id, approved, caption)
        except EgressRefused as refusal:
            # The descriptor was judged and then would not read — a stale NFS
            # handle, a device error. Room-facing sentence, no cooldown: the
            # broker did not fail, this machine did.
            log.info("egress refused %r mid-read: %s", approved.path, refusal.reason)
            return UploadResult(False, path=approved.path, reason=refusal.reason)
        except Exception:  # noqa: BLE001 — I1 again, and this one is the scar
            log.exception("room op: %s failed on its way to the room", approved.path)
            return UploadResult(False, path=approved.path, reason=_UNREADABLE)
        finally:
            approved.close()

    # -- internals ----------------------------------------------------------

    def _put_media(
        self,
        room_id: str,
        approved: ApprovedFile,
        caption: Optional[str],
    ) -> UploadResult:
        """The upload proper, on a descriptor the allowlist has already judged.

        Split out of `upload` so the `try` that keeps I1 can wrap all of it —
        the encode, the read underneath the encode, and the call — rather than
        the one line anybody remembers to guard.
        """
        encoded = _encode(approved)
        if encoded is None:
            return UploadResult(False, path=approved.path, reason=_GREW)
        filename = _filename(approved)
        payload: Dict[str, object] = {"content_b64": encoded, "filename": filename}
        if caption:
            payload["caption"] = caption
        answer = self._call(
            payload, path=MEDIA_PATH.format(room=_quote(room_id)), op="upload",
        )
        if answer is None:
            return UploadResult(False, filename=filename, path=approved.path,
                                reason=_UNREACHABLE)
        mxc = answer.get("mxc") if isinstance(answer, dict) else ""
        return UploadResult(True, mxc=str(mxc or ""), filename=filename,
                            path=approved.path)

    def _call(self, payload: Dict[str, object], path: str = ROOM_PATH,
              op: str = "") -> Optional[dict]:
        """One room op. Returns the answer, or `None` — and never raises."""
        name = op or str(payload.get("op") or "?")
        if not self.available:
            log.debug("room op %s skipped: cooling down for %.0fs more",
                      name, self.cooldown_remaining)
            return None
        try:
            answer = self._http.post(path, payload, timeout=self.timeout)
            return answer if isinstance(answer, dict) else {}
        except AuthRejected as exc:
            # Not raised (I1) and not swallowed either (C8): auth recovery is
            # told directly, because a revoked bearer is not a cosmetic failure
            # and the loop above must not have to guess it from a log line.
            self._trip(f"{name} rejected the bearer ({exc.status})")
            if self._on_auth_rejected is not None:
                try:
                    self._on_auth_rejected(exc)
                except Exception:  # noqa: BLE001 — a hook must not become fatal
                    log.exception("room op: the auth-rejection hook raised")
            return None
        except Exception as exc:  # noqa: BLE001 — nothing here reaches the loop
            self._trip(f"{name} failed: {exc}")
            return None

    def _trip(self, why: str) -> None:
        self._blocked_until = self._clock() + self.cooldown_s
        log.warning("room ops off for %.0fs — %s", self.cooldown_s, why)


#: Said out loud when a consumer built the client with no roots. Naming the
#: cause matters: "not sent" plus no reason reads as a bug in the agent.
_NO_ALLOWLIST = (
    "this client is not configured to send files, so nothing was attached"
)
_NO_ROOM = "this client does not know which room to send it to"
_UNREACHABLE = "this client could not reach the room to send it"
#: Cooling down reads to the room as unreachable, because that is what it is.
_COOLING = _UNREACHABLE
_GREW = "it grew while it was being read, so it was not sent"
#: For a file this machine could not hand over. Deliberately vague about why:
#: an errno is for the log, and the room gets a sentence.
_UNREADABLE = "this client could not read it, so it was not sent"


def _encode(approved: ApprovedFile) -> Optional[str]:
    """The judged descriptor's bytes, base64'd — or `None` if it outgrew its size.

    Reads `size + 1` for the same reason the media *ingress* does: the size came
    from an `fstat` taken a moment ago, and a file that has been growing since
    should be refused rather than truncated into the room.

    The measurement to read against is **the judged size, not the cap**. Reading
    `cap + 1` says it refuses growth and does not: a file that was 40 bytes when
    the allowlist approved it and 9 MB by the time it was read is well under a
    24 MiB cap, so all 9 MB go into the room, unmeasured by anything. The cap
    answers "may a file this big be sent"; only the `fstat` answers "is this
    still the file that was approved", and this is the second question.
    """
    limit = approved.size
    raw = approved.read(limit + 1)
    if len(raw) > limit:
        return None
    return base64.b64encode(raw).decode("ascii")


def _filename(approved: ApprovedFile) -> str:
    """A name that is only a name — and that keeps its extension.

    The extension is load-bearing on this route: the broker guesses the mime
    from it (`mimetypes.guess_type`), so it decides whether the room renders an
    image or offers a download. Truncating the whole string would eat it, so the
    stem is what gets shortened.
    """
    stem, dot, ext = _partition_ext(approved.name)
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).lstrip(".")[:100]
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:16]
    return (stem or "file") + (dot + ext if ext else "")


def _partition_ext(name: str):
    head, dot, tail = (name or "").rpartition(".")
    if not dot or not head:
        return (name or ""), "", ""
    return head, ".", tail


def _quote(room_id: str) -> str:
    """A room id as one URL path segment. `!room:server` has to stay one."""
    return urllib.parse.quote(room_id, safe="")


def _valid_room(room_id: object) -> bool:
    return isinstance(room_id, str) and bool(ROOM_ID_RE.match(room_id))


def _mentions(mentions: Optional[Sequence[str]]) -> list:
    """The mxids to notify: full ones only, at most `MAX_MENTIONS`.

    Over the cap the extras are dropped rather than the op refused. A message
    that lands and notifies nine of ten people is a better outcome than one that
    does not land at all — and the drop is logged so it is not a silence.

    Materialised once, first. A caller handing over a generator is handing over
    something that can be walked exactly one time, and the old code walked it
    three: the mxids came out, and then the count of what was dropped came out
    of an exhausted iterator as a negative number in the log.
    """
    named = list(mentions or ())
    if not named:
        return []
    good = [m for m in named if isinstance(m, str) and _MXID_RE.match(m)]
    if len(good) != len(named):
        log.info("room op: dropped %d mention(s) that were not full mxids",
                 len(named) - len(good))
    if len(good) > MAX_MENTIONS:
        log.info("room op: %d mentions asked for, the broker stamps %d",
                 len(good), MAX_MENTIONS)
        good = good[:MAX_MENTIONS]
    return good


def _event_id(answer: object) -> str:
    """The posted message's id, under whichever name it came back.

    Three spellings at the top level and two nesting keys, all observed. The
    ladder is worth this much tolerance: an id read as absent turns one edited
    message into two posted ones.
    """
    if not isinstance(answer, dict):
        return ""
    for key in ("event_id", "eventId", "id"):
        value = answer.get(key)
        if isinstance(value, str) and value:
            return value
    for key in ("result", "data"):
        inner = answer.get(key)
        if isinstance(inner, dict):
            found = _event_id(inner)
            if found:
                return found
    return ""
