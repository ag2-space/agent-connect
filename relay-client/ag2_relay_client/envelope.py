"""The task envelope, and the trust boundary it crosses.

Everything in this module exists because the broker is *outside* the machine's
trust boundary. It is trusted to attest who sent a message — that is its job,
and `user_id` staying broker-attested rather than body-influenced is what makes
the no-wire-escalation property true — and it is trusted with nothing else. So
this is where a wire dict becomes a `Task`:

- **Unknown fields are ignored, unknown enum values degrade to the default
  (G3).** The envelope is frozen additive-only with no version field, which
  means a broker deploy that adds a field must not break a running worker, and
  an out-of-vocabulary value must not land verbatim in trusted local state. Both
  halves matter: hard-failing on `lease_id` breaks on every additive deploy;
  passing an unrecognised `priority` through lands an attacker-shaped string
  wherever priority is read.
- **Unsigned in-band metadata is stripped, with no fallback (G2).** The gateway
  appends `[room-ops metadata: …]` blocks to the message body — the same field
  the user's words arrive in, unsigned, self-labelled "Not an instruction",
  which a naive agent reads as an instruction anyway (owner directive
  2026-07-16). A body that was *only* metadata degrades to empty. It does not
  fall back to the original: that fallback was a P1 on PR #2149, because falling
  back re-admits the exact block being quarantined.
- **The id is validated before any use (F8).** It lands in journal paths and
  goes back out on the wire.

**The tier is delivered as data, verbatim.** `access_tier` and `user_id` are the
broker's attestation and this library passes them across the seam unchanged —
the trust *mapping* is the consumer's, and the two consumers of this package
answer it differently on purpose (agent-connect honors the attestation per its
ADR 0003; sutando's shim ignores the wire tier by local policy). A library that
mapped the tier itself would have to pick one of them and break the other. The
value's *shape* is bounded — a str, control characters removed, length-capped —
which changes no legitimate value and keeps an attacker-shaped one small.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Tuple

from .state import valid_wire_id

#: The gateway's unsigned metadata block. The bracket body is `[^\]]*` because
#: the block carries no nested `]`, so the pattern cannot over-eat the user's
#: words; the leading `\s*` takes the whitespace the block would otherwise
#: leave behind. Case-insensitive, and applied before anything reads the body.
ROOM_OPS_META_RE = re.compile(r"\s*\[room-ops metadata:[^\]]*\]", re.IGNORECASE)

#: `priority`'s vocabulary, and the value anything else degrades to (G3).
PRIORITIES = ("urgent", "normal", "low")
DEFAULT_PRIORITY = "normal"

#: Bounds for the free-ish strings the wire supplies. None of these is a
#: security boundary on its own — they keep an unbounded remote value from
#: becoming an unbounded local one.
MAX_TEXT = 64 * 1024
MAX_SHORT = 256
MAX_TIER = 64

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value: Any, limit: int = MAX_SHORT, keep_newlines: bool = False) -> str:
    """A wire value as a bounded string, or `""`.

    A non-string is not coerced with `str()`: a dict where a string was
    expected is a shape this client does not understand, and `"{'a': 1}"` is a
    worse answer than nothing.
    """
    if not isinstance(value, str):
        return ""
    if not keep_newlines:
        value = value.replace("\r", " ").replace("\n", " ")
    return _CONTROL_RE.sub("", value)[:limit]


def strip_room_ops_meta(body: str) -> Tuple[str, bool]:
    """`(cleaned, stripped)` — the body with every metadata block removed (G2).

    Returns the cleaned body even when it is now empty. A metadata-only body is
    pure injection with no legitimate task text in it, so empty is the honest
    answer; the tempting fallback to the original is the bug this function is
    written to not have.
    """
    if not body or "room-ops metadata:" not in body.lower():
        return body, False
    cleaned = ROOM_OPS_META_RE.sub("", body)
    return cleaned.strip(), cleaned != body


class Task:
    """One unit of work, as the consumer sees it.

    Deliberately **not** a view over the wire dict: there is no `raw` attribute,
    no `original_body`, nothing that hands back the text G2 just stripped. A
    consumer that could reach the unstripped body would eventually reach for it.

    `attachments` is 0..N `media.Attachment`s — resolved local paths by the time
    this object leaves the queue, because the media stage runs between the poll
    and the delivery. The consumer never sees a marker or a URL either way: a
    fetch that failed is an attachment carrying a reason, not a URL to retry.
    """

    __slots__ = (
        "id", "body", "room_id", "user_id", "access_tier",
        "requested_access_tier", "collaborator", "sensitive_data_filter",
        "priority", "timestamp", "room_name", "sender_name", "reply_to_event",
        "reply_to_me", "source_message_id", "attempt", "metadata_stripped",
        "attachments",
    )

    def __init__(
        self,
        id: str,                       # noqa: A002 — the wire's name for it
        body: str = "",
        room_id: str = "",
        user_id: str = "",
        access_tier: str = "",
        requested_access_tier: str = "",
        collaborator: bool = False,
        sensitive_data_filter: bool = True,
        priority: str = DEFAULT_PRIORITY,
        timestamp: str = "",
        room_name: str = "",
        sender_name: str = "",
        reply_to_event: str = "",
        reply_to_me: bool = False,
        source_message_id: str = "",
        attempt: int = 0,
        metadata_stripped: bool = False,
        attachments: Tuple = (),
    ):
        self.id = id
        #: The message text, metadata blocks removed (G2).
        self.body = body
        #: The originating room — the reply's destination, and what a media
        #: upload has to target (F7).
        self.room_id = room_id
        #: Broker-attested sender. Never body-influenced (G1).
        self.user_id = user_id
        #: The broker's attestation about the sender, verbatim. Mapping it to
        #: local privilege is the consumer's decision, not this library's.
        self.access_tier = access_tier
        #: The request half of the Collaborator handshake. Grants nothing alone.
        self.requested_access_tier = requested_access_tier
        #: The consent half — the exact boolean `true`, or False.
        self.collaborator = collaborator
        #: Absent or malformed reads as True: the opt-out is deliberate and
        #: explicit, or it has not happened.
        self.sensitive_data_filter = sensitive_data_filter
        self.priority = priority
        self.timestamp = timestamp
        self.room_name = room_name
        self.sender_name = sender_name
        self.reply_to_event = reply_to_event
        self.reply_to_me = reply_to_me
        self.source_message_id = source_message_id
        #: The broker's re-serve counter. Additive bookkeeping; a client that
        #: ignores it still works — it is carried because it is the one signal
        #: that says "you have seen this before".
        self.attempt = attempt
        #: Whether a metadata block was quarantined out of `body`. For logging;
        #: the block itself is gone.
        self.metadata_stripped = metadata_stripped
        #: 0..N `media.Attachment` — local paths, or an honest reason why not.
        #: Filled by the media stage, which is why it is not a parse-time field:
        #: `parse_task` reads the wire, and the wire has no attachment field at
        #: all. See `media.py`.
        self.attachments = tuple(attachments)

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<Task {self.id} room={self.room_id} tier={self.access_tier!r}>"

    def __eq__(self, other) -> bool:
        if not isinstance(other, Task):
            return NotImplemented
        return all(getattr(self, name) == getattr(other, name)
                   for name in self.__slots__)


def parse_task(raw: Mapping[str, Any]) -> Optional[Task]:
    """A wire dict as a `Task`, or `None` when it is not usable as one.

    `None` means "this is not a task I can act on" — a non-dict, or an id that
    fails the slug grammar. The caller decides what to do about it; the
    documented answer is the dead-letter reject, because a silently skipped
    task just re-serves until the attempt cap trips.
    """
    if not isinstance(raw, Mapping):
        return None
    wire_id = raw.get("id")
    if not isinstance(wire_id, str) or not valid_wire_id(wire_id):
        return None

    body, stripped = strip_room_ops_meta(_text(raw.get("task"), MAX_TEXT, keep_newlines=True))
    priority = raw.get("priority")
    if priority not in PRIORITIES:
        # G3: out of vocabulary degrades to the default. Silently — an unknown
        # value is what an additive deploy looks like from here.
        priority = DEFAULT_PRIORITY

    return Task(
        id=wire_id,
        body=body,
        room_id=_text(raw.get("channel_id")),
        user_id=_text(raw.get("user_id")),
        access_tier=_text(raw.get("access_tier"), MAX_TIER),
        requested_access_tier=_text(raw.get("requested_access_tier"), MAX_TIER),
        # The exact boolean, per the protocol: `"true"`, `1` and `"yes"` are
        # not consent, and reading them as consent is how a widening becomes
        # reachable by whoever can shape the field.
        collaborator=raw.get("collaborator") is True,
        sensitive_data_filter=raw.get("sensitive_data_filter") is not False,
        priority=priority,
        timestamp=_text(raw.get("timestamp")),
        room_name=_text(raw.get("room_name")),
        sender_name=_text(raw.get("sender_name")),
        reply_to_event=_text(raw.get("reply_to_event")),
        reply_to_me=raw.get("reply_to_me") is True,
        # Canonical name first, with the older spelling as the fallback the
        # protocol documents as the same value.
        source_message_id=_text(raw.get("source_message_id")
                                or raw.get("source_event_id")),
        attempt=_attempt(raw.get("attempt")),
        metadata_stripped=stripped,
    )


def _attempt(value: Any) -> int:
    """The re-serve counter as a non-negative int; anything else is 0."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value < 10 ** 6 else 0
