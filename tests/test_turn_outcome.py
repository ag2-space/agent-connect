"""How a Turn ends: one `Outcome`, delivered by the broker (ADR 0002, 0.4.0).

- No placeholder: the Worker posts and edits nothing for an ordinary answer.
- `ANSWERED` carries the answer; `NO_ACTION` is empty text or exactly
  `NO_REPLY`; `FAILED_OUTCOME` is a Turn that stopped with nothing to say.
- A `Notice` is its own message, mentioning only the members it names and
  never the requester.
- `handle_one` answers each Task through exactly one relay call.

Run: python3 tests/test_turn_outcome.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio

from agent_connect.events import (
    COMPLETED,
    FAILED,
    TIMEOUT,
    TOKEN_LIMIT,
    Done,
    MessageChunk,
    Notice,
    ToolFinished,
    ToolStarted,
    TurnContext,
)
from agent_connect.reporter import (
    ANSWERED,
    FAILED_OUTCOME,
    NO_ACTION,
    STOP_LINES,
    TurnReporter,
)
from agent_connect.worker import handle_one
from _taskqueue import FakeClient, task

ROOM = "!room:ag2.space"
REQUESTER = "@peer.agent:ag2.space"
MEMBER = "@ada:ag2.space"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Ops:
    """Room Ops that record what would reach the room."""

    available = True

    def __init__(self):
        self.messages = []
        self.edits = []

    async def message(self, room, text, mentions=None):
        self.messages.append((room, text, list(mentions or [])))
        return f"$m{len(self.messages)}"

    async def edit(self, room, event_id, body):
        self.edits.append(body)


class Scripted:
    def __init__(self, *events):
        self.events = events

    async def turn(self, ctx):
        for event in self.events:
            await asyncio.sleep(0)
            yield event


def ctx(**kw):
    base = dict(prompt="do it", task_id="t1", room=ROOM, access_tier="owner",
                user_id=REQUESTER, room_members=(MEMBER, REQUESTER), cwd="/repo")
    return TurnContext(**{**base, **kw})


def run(*events, ops=None, c=None):
    return asyncio.run(TurnReporter(ops).run(Scripted(*events), c or ctx()))


def said(text):
    return (MessageChunk(text=text), Done(reason=COMPLETED, text=text))


print("\n-- no placeholder --")

ops = Ops()
out = run(ToolStarted(tool_id="a", title="Read x.py"),
          ToolFinished(tool_id="a", title="Read x.py", status=COMPLETED),
          *said("here it is"), ops=ops)
check(out.kind == ANSWERED and out.body == "here it is", "an answer is ANSWERED, as written")
check(ops.messages == [] and ops.edits == [],
      "nothing is posted or edited: no placeholder, no progress")


print("\n-- no reply --")

check(run(Done(reason=COMPLETED, text="")).kind == NO_ACTION,
      "empty text with a normal stop is NO_ACTION")
check(run(*said("NO_REPLY")).kind == NO_ACTION, "exactly NO_REPLY is NO_ACTION")
check(run(*said("  NO_REPLY \n")).kind == NO_ACTION, "surrounding whitespace is ignored")
split = run(MessageChunk(text="NO_"), MessageChunk(text="REPLY"), Done(reason=COMPLETED))
check(split.kind == NO_ACTION, "NO_REPLY split across chunks is still NO_ACTION")
check(split.body == "", "and carries no body")
longer = run(*said("NO_REPLY please"))
check(longer.kind == ANSWERED and longer.body == "NO_REPLY please",
      "NO_REPLY inside a longer reply is an answer, never a substring match")
ops = Ops()
run(*said("NO_REPLY"), ops=ops)
check(ops.messages == [] and ops.edits == [], "a no-reply Turn posts nothing")


print("\n-- abnormal stops --")

failed = run(ToolStarted(tool_id="a", title="Read x.py"), Done(reason=FAILED, text=""))
check(failed.kind == FAILED_OUTCOME, "no text and no note on a failure is FAILED_OUTCOME")
check(f"reason: {FAILED}" in failed.body and "Read x.py" in failed.body
      and "Operator diagnostics" in failed.body,
      "whose archived body has the reason and the diagnostics")
noted = run(Done(reason=FAILED, text="", note="agent-connect: not logged in"))
check(noted.kind == ANSWERED and "not logged in" in noted.body,
      "an Adapter's explanation of a failure is answered, so the room learns why")
partial = run(MessageChunk(text="half"), Done(reason=TOKEN_LIMIT, text="half"))
check(partial.kind == ANSWERED and partial.body.startswith("half")
      and STOP_LINES[TOKEN_LIMIT] in partial.body,
      "partial text with an abnormal stop is answered with the stop line")
timed = run(MessageChunk(text="some"), Done(reason=TIMEOUT, text="some"))
check(STOP_LINES[TIMEOUT] in timed.body, "a timeout says so")
check(not any(line in run(*said("all of it")).body for line in STOP_LINES.values()),
      "a Turn that simply finished carries no stop line")


print("\n-- notices --")

ops = Ops()
text = f"the context was reset; ask {MEMBER} or {REQUESTER}, not @stranger:ag2.space"
out = run(Notice(text=text), *said("done"), ops=ops)
check(len(ops.messages) == 1 and ops.messages[0][1] == text and ops.edits == [],
      "a notice is posted as its own message")
check(ops.messages and ops.messages[0][2] == [MEMBER],
      "mentioning only the room members it names, never the requester")
check(out.kind == ANSWERED and out.body == "done", "and the answer is unchanged by it")
out = run(Notice(text="🧠 the context was reset"), *said("done"))
check(out.body.startswith("🧠 the context was reset") and out.body.endswith("done"),
      "a notice that cannot be posted leads the answer")
out = run(Notice(text="🧠 the context was reset"), *said("NO_REPLY"))
check(out.kind == ANSWERED and "the context was reset" in out.body,
      "a NO_REPLY Turn with an unposted notice still delivers the notice")


print("\n-- handle_one: exactly one relay call --")


class Adapter:
    def __init__(self, *events):
        self.events = events

    async def turn(self, c):
        for event in self.events:
            yield event


def handled(adapter, body="do it"):
    client = FakeClient()
    out = asyncio.run(handle_one(task("t-1", body, room=ROOM), adapter, "/repo",
                                 client=client))
    calls = (len(client.completed) + len(client.no_actions) + len(client.failed)
             + len(client.rejected))
    return client, out, calls


client, out, calls = handled(Adapter(*said("the answer")))
check(calls == 1 and client.completed == [("t-1", "the answer")] and out == "the answer",
      "ANSWERED is `complete(body)`, and returns the body")
check(client.base_dirs == [("t-1", "/repo")], "with the Turn's directory as the base")
client, out, calls = handled(Adapter(*said("NO_REPLY")))
check(calls == 1 and len(client.no_actions) == 1 and client.no_actions[0][0] == "t-1"
      and out == "", "NO_ACTION is `no_action`, and returns \"\"")
client, out, calls = handled(Adapter(Done(reason=FAILED, text="")))
check(calls == 1 and len(client.failed) == 1 and "reason: failed" in client.failed[0][1]
      and out == "", "FAILED_OUTCOME is `fail(body)`, and returns \"\"")
client, out, calls = handled(Adapter(*said("unused")), body="")
check(calls == 1 and client.refusal("t-1") and out == "",
      "a Task with no text and no files is `reject`")

print("\n" + ("PASS — turn outcome green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
