"""Bare-interpreter room-context shape/byte/scope/expiry contract tests."""
import _bootstrap
import copy
import json
from pathlib import Path
import time

from ag2_relay_client.envelope import parse_task
from ag2_relay_client.room_context import parse_room_context

raw = json.loads((Path(__file__).parent / "fixtures/room-context-v1-example.json").read_text())
now = int(time.time())
for key in ("room_context_binding", "room_context"):
    raw[key].update(observed_at=now, expires_at=now+600)
fails = 0

def check(cond, label):
    global fails
    print(("  ok   " if cond else "  FAIL ") + label)
    fails += not cond


def packet(value):
    return parse_task(value).room_context

check(packet(raw) is not None, "valid v1 packet parses without dependencies")
for category in ("", "Research", "  Research  ", "a" * 256, "🦊" * 256, "e\u0301" * 128):
    changed = copy.deepcopy(raw); changed["room_context"]["room"]["category"] = category
    accepted = packet(changed)
    check(accepted is not None and accepted.context["room"]["category"] == category,
          "optional category preserves exact Unicode/code-point boundary: " + repr(category[:16]))
changed = copy.deepcopy(raw); changed["room_context"]["room"].pop("category", None)
check(packet(changed) is not None and "category" not in packet(changed).context["room"],
      "category may be omitted for older consumers")
for category in (None, True, 1, [], {}, "a" * 257, "🦊" * 257, "a" * 256 + " ", "\ud800"):
    changed = copy.deepcopy(raw); changed["room_context"]["room"]["category"] = category
    rejected = parse_task(changed)
    check(rejected.room_context is None and rejected.body == raw["task"],
          "invalid/oversized category drops context without trimming or coercion: " + repr(category)[:32])
changed = copy.deepcopy(raw); changed["room_context"]["room"]["category_extra"] = "Research"
check(packet(changed) is None, "other unknown room fields remain rejected")
for key in ("room_context_binding", "room_context"):
    for bad in (None, [], "fake", 1, True):
        changed = copy.deepcopy(raw); changed[key] = bad
        got = parse_task(changed)
        check(got.room_context is None and got.room_context_reason != "absent"
              and got.body == raw["task"], key + " malformed shape preserves ask")
for changed in ({"id": "only-null", "room_context": None},
                {"id": "only-null", "room_context_binding": None}):
    check(parse_task(changed).room_context_reason != "absent", "explicit null is managed-unavailable, not legacy")
check(parse_task({"id": "legacy", "task": "hello"}).room_context_reason == "absent", "legacy absence is distinct")
for field, value in (("schema_version", True), ("observed_at", True),
                     ("expires_at", float('nan')), ("agent_id", "x\nowner"),
                     ("anchor_event_id", ""), ("visibility", "anything"),
                     ("expires_at", now+3601), ("observed_at", now+100)):
    changed = copy.deepcopy(raw)
    for key in ("room_context_binding", "room_context"):
        changed[key][field] = value
    check(packet(changed) is None, "rejects invalid binding " + field + ": " + repr(value))
context = packet(raw)
check(context.current(task_id=raw['id'], room_id=raw['channel_id'], access_tier="owner",
                      anchor_event_id="$anchor", now=now+601)[1] == "expired",
      "queued packet expires again before inference")
check(parse_task(raw, agent_id="@different:example.test").room_context is None,
      "configured actor checked separately from sender")
fresh = context.context; fresh["readme"]["text"] = "mutated"
check("README_SENTINEL" in context.context["readme"]["text"], "callers cannot mutate accepted packet")
changed = copy.deepcopy(raw); changed["room_context"]["messages"] = [dict(raw['room_context']['messages'][0], body="🙂"*2048)] * 5
check(packet(changed) is None, "whole UTF-8 byte budget rejects Unicode aggregate overflow")
changed = copy.deepcopy(raw)
for key in ("room_context_binding", "room_context"): changed[key]['anchor_event_id'] = None
changed.pop('source_message_id')
check(packet(changed) is not None, "genuinely eventless task binds null anchor")
changed = copy.deepcopy(raw)
changed['room_context']['messages'][0]['origin_server_ts'] = 2**53
check(packet(changed) is None, "timestamps require cross-language JSON-safe integers")
changed = copy.deepcopy(raw); changed['room_context']['coverage'].update(truncated=True,cursor="opaque cursor")
changed['room_context']['messages'] = []
check(packet(changed) is not None, "empty message page with cursor is partial, not invented empty-room proof")
print("\n" + ("PASS — bounded room context" if not fails else f"FAIL — {fails}"))
raise SystemExit(bool(fails))
