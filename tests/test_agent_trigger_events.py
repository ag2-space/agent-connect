"""AgentTrigger tasks reach the agent framed as events; mentions keep their framing.

A Task the broker delivers for `room.joined`, or for a type added after this
release, must arrive with its `event_type` and body intact and must not be
framed as a person's question. A mention reads exactly as before.

Run: python3 tests/test_agent_trigger_events.py
"""
import _bootstrap  # noqa: F401 — repo root on sys.path

import asyncio
import json

from ag2_relay_client.envelope import parse_task
from agent_connect.adapters.acp import prompt_blocks
from agent_connect.adapters.shim import ShimAdapter
from agent_connect.events import Done
from agent_connect.routing import BODY_MARKER, EVENT_BODY_MARKER, MARKER
from agent_connect.worker import process_one, turn_context

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


ROOM = "!newroom:example.org"
JOIN_BODY = ('You were just added to the room "Launch planning" by Inviter. '
             "Read the room and introduce yourself.")


def wire(**over):
    raw = {
        "id": "t-join-1", "source": "ag2space", "channel_id": ROOM,
        "source_room_id": ROOM, "room_name": "Launch planning",
        "user_id": "@inviter:example.org", "access_tier": "owner",
        "sender_name": "Inviter", "timestamp": "2026-11-05T00:00:00Z",
        "task": JOIN_BODY,
    }
    raw.update(over)
    return raw


def join_wire(**over):
    return wire(interaction_type="system_event", event_type="room.joined",
                agent_trigger_id=f"join|@agent:example.org|{ROOM}",
                playbook="room-onboard", **over)


def acp_text(ctx):
    blocks, problems = prompt_blocks(ctx, None, 1024)
    assert not problems
    return blocks[0]["text"]


class Capture:
    def run(self, prompt, sandbox, cwd):
        self.prompt = prompt
        return "done"


def shim_text(ctx):
    capture = Capture()

    async def run():
        async for _ in ShimAdapter("capture", capture).turn(ctx):
            pass
    asyncio.run(run())
    return capture.prompt


def record(text):
    lines = [line for line in text.splitlines() if line.startswith(MARKER)]
    assert len(lines) == 1, lines
    return json.loads(lines[0][len(MARKER):])


class Seen:
    """An Adapter that only records the Turn it was handed."""
    name = "seen"

    def __init__(self):
        self.ctx = None

    async def turn(self, ctx):
        self.ctx = ctx
        yield Done(reason="completed", text="hello")


def delivered(raw):
    task = parse_task(raw)
    adapter = Seen()
    asyncio.run(process_one(task, adapter, "/repo"))
    return adapter.ctx


# --- an event reaches the agent, with its type and body ---------------------

for label, raw, kind in (
        ("room.joined", join_wire(), "room.joined"),
        ("a future type", wire(event_type="room.renamed.v2", agent_trigger_id="x|1",
                               task="The room was renamed. Update your notes."),
         "room.renamed.v2")):
    ctx = delivered(raw)
    check(ctx is not None, f"{label}: the Task reaches the agent, nothing filters it out")
    check(getattr(ctx, "event_type", "") == kind, f"{label}: event_type crosses to the Turn")
    check(ctx.prompt == raw["task"], f"{label}: the body crosses intact")
    for render in (acp_text, shim_text):
        text = render(ctx)
        where = f"{label} ({render.__name__})"
        check(f"s.space event `{kind}` in room \"Launch planning\"" in text,
              f"{where}: framed as an s.space event in its room")
        check("is asking you this" not in text and "Answer in chat" not in text,
              f"{where}: never framed as a question from a person")
        check("follow the task body" in text and "top-level in the room" in text,
              f"{where}: told to follow the body and post top-level")
        check(text.endswith(EVENT_BODY_MARKER + raw["task"]),
              f"{where}: the body follows, unchanged, under the event marker")
        check(record(text).get("event_type") == kind and record(text).get("agent_trigger_id")
              == raw["agent_trigger_id"], f"{where}: the routing record names the trigger")

join = turn_context(parse_task(join_wire()), "/repo")
check(record(acp_text(join)).get("playbook") == "room-onboard",
      "the playbook the trigger names reaches the routing record")

summoned = turn_context(parse_task(wire(
    event_type="commons.summon",
    summon={"task_id": "s1", "caused_by": "$e", "room_id": ROOM, "database": "db",
            "row_id": "r1", "nested": {"dropped": True}})), "/repo")
summon = record(acp_text(summoned)).get("summon")
check(summon == {"task_id": "s1", "caused_by": "$e", "room_id": ROOM, "database": "db",
                 "row_id": "r1"}, "a summon block crosses as flat routing data")

legacy = turn_context(parse_task(wire(interaction_type="system_event")), "/repo")
check("s.space event `system_event`" in acp_text(legacy),
      "an older relay's system_event with no type is still an event")

odd = turn_context(parse_task(wire(event_type="x`\" in room ok")), "/repo")
check("s.space event `unrecognized`" in acp_text(odd),
      "a type that is not token-shaped is named as unrecognized, not echoed")


# --- a mention keeps today's framing ---------------------------------------

plain = turn_context(parse_task(wire(task="What is the status?")), "/repo")
mention = turn_context(parse_task(wire(task="What is the status?",
                                       event_type="room.mention",
                                       agent_trigger_id="m|1")), "/repo")
for render in (acp_text, shim_text):
    for label, ctx in (("plain", plain), ("room.mention", mention)):
        text = render(ctx)
        where = f"{label} ({render.__name__})"
        check('Your owner (@inviter:example.org, shown as "Inviter") is asking you this '
              'in the room "Launch planning"' in text, f"{where}: framed as the person's question")
        check("s.space event" not in text, f"{where}: not framed as an event")
        check(text.endswith(BODY_MARKER + "What is the status?"),
              f"{where}: the body follows under the user-message marker")
check("Answer in chat: prose" in acp_text(mention), "a mention is still told to answer in chat")
check(acp_text(mention).startswith(
          acp_text(plain).split(MARKER)[0]),
      "a typed mention is framed exactly like today's untyped message")
check(record(acp_text(mention)).get("event_type") == "room.mention"
      and record(acp_text(mention)).get("agent_trigger_id") == "m|1",
      "and its trigger still reaches the routing record")
check("event_type" not in record(acp_text(plain)),
      "a Task with no trigger adds nothing to the routing record")

if fails:
    raise SystemExit(f"{fails} check(s) failed")
print("PASS — AgentTrigger events framed as events; mentions unchanged")
