"""Bounded, broker-supplied room data, separate from the current Task.

This module validates shape and scope, not authorization. The authenticated
broker must apply current acting-identity and room policy before each delivery.
Nothing in a packet can grant a tier, fetch history, or persist a transcript.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

MAX_PAYLOAD_BYTES = 32768
MAX_BINDING_BYTES = 8192
MAX_ID = 1024
ROOM_CONTEXT_CAPABILITY = "room-context-v1"
ACP_META_KEY = "ag2/room_context"
_BINDING = frozenset((
    "schema_version", "context_id", "agent_id", "room_id", "task_id",
    "anchor_event_id", "access_tier", "policy_revision", "membership_epoch",
    "visibility", "observed_at", "expires_at",
))
_SECTIONS = frozenset(("room", "readme", "members", "messages"))
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _string(value: Any, limit: int, nonempty: bool = True) -> bool:
    return (isinstance(value, str) and len(value) <= limit
            and (not nonempty or bool(value.strip()))
            and not _CONTROL.search(value))


def _identifier(value: Any, limit: int = MAX_ID) -> bool:
    return _string(value, limit) and not any(c in value for c in "\r\n\t")


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**53 - 1


def _object(value: Any, required, optional=()) -> bool:
    return (isinstance(value, dict) and set(required) <= set(value)
            and set(value) <= set(required) | set(optional))


def _json(value: Any, limit: int) -> Optional[str]:
    # Reject arbitrary local objects/non-JSON keys and nesting before encoding.
    def shaped(one, depth=0):
        if depth > 12:
            return False
        if isinstance(one, dict):
            return all(isinstance(k, str) and shaped(v, depth + 1)
                       for k, v in one.items())
        if isinstance(one, list):
            return all(shaped(v, depth + 1) for v in one)
        return one is None or type(one) in (str, int, bool)
    try:
        if not shaped(value):
            return None
        result = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return result if len(result.encode("utf-8")) <= limit else None
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return None


@dataclass(frozen=True)
class RoomContext:
    """Validated packet stored as JSON so callers cannot mutate its scope.

    Properties return fresh objects. Revalidate immediately before inference:
    queueing and ACP startup can outlive a packet's expiration.
    """
    binding_json: str
    context_json: str

    @property
    def binding(self) -> dict:
        return json.loads(self.binding_json)

    @property
    def context(self) -> dict:
        return json.loads(self.context_json)

    @property
    def scope(self) -> str:
        held = self.binding
        fields = ("agent_id", "room_id", "access_tier", "policy_revision",
                  "membership_epoch", "visibility")
        return hashlib.sha256(json.dumps([held[k] for k in fields] + [self.context["status"]]).encode()).hexdigest()

    def current(self, *, task_id: str, room_id: str, access_tier: str,
                anchor_event_id: str = "", agent_id: str = "",
                now: Optional[float] = None) -> Tuple[Optional["RoomContext"], str]:
        return parse_room_context(
            self.binding, self.context, task_id=task_id, room_id=room_id,
            access_tier=access_tier, anchor_event_id=anchor_event_id,
            agent_id=agent_id, now=now)

    def as_meta(self) -> dict:
        return {"binding": self.binding, "context": self.context}


def parse_room_context(binding: Any, context: Any, *, task_id: str, room_id: str,
                       access_tier: str, anchor_event_id: str = "",
                       agent_id: str = "", now: Optional[float] = None
                       ) -> Tuple[Optional[RoomContext], str]:
    """Return a packet or a non-sensitive availability reason; never drop ask.

    `access_tier` must be the consumer's effective tier, so a consumer which
    demotes broker `team` to `guest` cannot inject a packet for `team`.
    `agent_id` is the configured actor if known, never the message sender.
    """
    if binding is None and context is None:
        return None, "absent"
    binding_json = _json(binding, MAX_BINDING_BYTES)
    context_json = _json(context, MAX_PAYLOAD_BYTES)
    if binding_json is None or context_json is None:
        return None, "invalid_or_oversized"
    if (not _object(binding, _BINDING)
            or not _object(context, _BINDING | {"status", "coverage"}, _SECTIONS)):
        return None, "invalid_schema"
    if type(binding["schema_version"]) is not int or binding["schema_version"] != 1:
        return None, "unsupported_version"
    if any(context[k] != binding[k] or type(context[k]) is not type(binding[k])
           for k in _BINDING):
        return None, "binding_mismatch"
    for field, cap in (("context_id", 256), ("agent_id", MAX_ID),
                       ("room_id", MAX_ID), ("task_id", MAX_ID),
                       ("policy_revision", 256), ("membership_epoch", 256)):
        if not _identifier(binding[field], cap):
            return None, "invalid_binding"
    anchor = binding["anchor_event_id"]
    if anchor is not None and not _identifier(anchor):
        return None, "invalid_binding"
    if (binding["access_tier"] not in ("owner", "team", "guest")
            or binding["visibility"] not in ("all", "mentions")):
        return None, "invalid_binding"
    if (binding["task_id"] != task_id or binding["room_id"] != room_id
            or binding["access_tier"] != access_tier
            or anchor != (anchor_event_id or None)
            or (agent_id and binding["agent_id"] != agent_id)):
        return None, "scope_mismatch"
    observed, expires = binding["observed_at"], binding["expires_at"]
    current = time.time() if now is None else now
    if (not _integer(observed) or not _integer(expires)
            or not 0 < expires - observed <= 3600 or observed > current + 60):
        return None, "invalid_lifetime"
    if expires <= current:
        return None, "expired"
    if context["status"] not in ("partial", "unavailable", "denied"):
        return None, "invalid_schema"
    coverage = context["coverage"]
    if (not _object(coverage, {"completeness", "truncated"}, {"cursor", "reason"})
            or coverage["completeness"] != "unknown"
            or type(coverage["truncated"]) is not bool
            or any(not _string(coverage[k], cap, nonempty=False)
                   for k, cap in (("cursor", MAX_ID), ("reason", 256)) if k in coverage)):
        return None, "invalid_coverage"
    if context["status"] != "partial" and _SECTIONS & set(context):
        return None, "denied_content"
    if "room" in context:
        room = context["room"]
        if (not _object(room, (), {"name", "topic", "category"})
                or any(not _string(room[k], cap, nonempty=False)
                       for k, cap in (("name", 512), ("topic", 2048), ("category", 256)) if k in room)):
            return None, "invalid_room"
    if "readme" in context:
        readme = context["readme"]
        if (not _object(readme, {"text", "content_hash", "truncated"})
                or not _string(readme["text"], 8192, nonempty=False)
                or not _string(readme["content_hash"], 256)
                or type(readme["truncated"]) is not bool):
            return None, "invalid_readme"
    for key in ("members", "messages"):
        if key in context and (not isinstance(context[key], list) or len(context[key]) > 20):
            return None, "invalid_" + key
    for member in context.get("members", []):
        if (not _object(member, {"user_id"}, {"display_name"})
                or not _identifier(member["user_id"])
                or ("display_name" in member and not _string(member["display_name"], 256, False))):
            return None, "invalid_members"
    for message in context.get("messages", []):
        if (not _object(message, {"event_id", "sender", "body", "selection"},
                        {"origin_server_ts", "thread_root", "reply_to_event_id"})
                or not _identifier(message["event_id"])
                or not _identifier(message["sender"])
                or not _string(message["body"], 2048, False)
                or message["selection"] not in ("mention", "policy")
                or ("origin_server_ts" in message and not _integer(message["origin_server_ts"]))
                or any(not _identifier(message[k])
                       for k in ("thread_root", "reply_to_event_id") if k in message)
                or (binding["visibility"] == "mentions" and message["selection"] != "mention")):
            return None, "invalid_messages"
    return RoomContext(binding_json, context_json), "available"
