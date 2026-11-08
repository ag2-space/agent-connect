"""Activity labels: a title and an outcome for the owner's Activity row.

The agent ends its reply with `[activity:title]` and `[activity:outcome]`
lines, removed before the reply is posted; `_meta["ag2/activity"]` on the
prompt response wins when a host sends it. Labels never change the outcome's
kind, a failed Turn keeps only its title, and a collaborator's labels pass the
same guard as the reply.

Run: python3 tests/test_activity_labels.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio

from agent_connect import activity
from agent_connect.events import COMPLETED, FAILED, Done, MessageChunk, TurnContext
from agent_connect.reporter import ANSWERED, FAILED_OUTCOME, NO_ACTION, TurnReporter
from agent_connect.routing import routing_preamble
from agent_connect.worker import handle_one
from _taskqueue import FakeClient, task

ROOM = "!room:s.space"
fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


TRAILER = "\n\n[activity:title] Fix login redirect\n[activity:outcome] Fixed the loop and ran the tests."

print("-- split --")
body, labels = activity.split("Done, see the diff." + TRAILER)
check(body == "Done, see the diff." and labels.title == "Fix login redirect"
      and labels.outcome == "Fixed the loop and ran the tests.", "the trailer is removed and read")
body, labels = activity.split("x\n[activity:outcome] o\n[activity:title] t\n\n")
check(body == "x" and labels == activity.Labels("t", "o"), "either order, trailing blank lines ignored")
body, labels = activity.split("x\n[activity:title] only a title")
check(body == "x" and labels == activity.Labels("only a title", ""), "one line alone is read")
text = "```\ncode\n[activity:title] t\n[activity:outcome] o"
check(activity.split(text) == (text, activity.Labels()), "inside an open code block is content")
text = "[activity:title] a\nmiddle\n[activity:title] b"
body, labels = activity.split(text)
check(labels.title == "b" and body == "[activity:title] a\nmiddle", "only the final lines count")
text = "x\n[activity:title] a\n[activity:title] b"
body, labels = activity.split(text)
check(labels.title == "b" and body == "x\n[activity:title] a", "a repeated tag stops the trailer")
check(activity.split("no labels here") == ("no labels here", activity.Labels()), "no trailer, no change")

print("-- meta --")
check(activity.from_meta({"ag2/activity": {"title": "T", "outcome": 5}}) == activity.Labels("T", ""),
      "_meta labels are read; non-strings are ignored")
check(activity.pick(activity.Labels("M", ""), activity.Labels("A", "B")) == activity.Labels("M", "B"),
      "meta wins field by field")


class Scripted:
    def __init__(self, *events):
        self.events = events

    async def turn(self, ctx):
        for event in self.events:
            yield event


def ctx(**kw):
    base = dict(prompt="do it", task_id="t1", room=ROOM, access_tier="owner",
                user_id="@me:s.space", cwd="/repo")
    return TurnContext(**{**base, **kw})


def run(*events, c=None):
    return asyncio.run(TurnReporter(None).run(Scripted(*events), c or ctx()))


def said(text, **kw):
    return (MessageChunk(text=text), Done(reason=COMPLETED, text=text, **kw))


try:
    from acp.schema import PromptResponse
    from agent_connect.acp.core import _response_meta
    resp = PromptResponse.model_validate(
        {"stopReason": "end_turn", "_meta": {"ag2/activity": {"title": "T"}}})
    check(activity.from_meta(_response_meta(resp)) == activity.Labels("T", ""),
          "the SDK's prompt response carries _meta through to the labels")
    check(_response_meta(PromptResponse.model_validate({"stopReason": "end_turn"})) is None,
          "a response without _meta carries none")
except ImportError:
    print("  skip the ACP SDK is not installed")

print("-- reporter --")
out = run(*said("The answer." + TRAILER))
check(out.kind == ANSWERED and out.body == "The answer." and out.title == "Fix login redirect"
      and out.summary == "Fixed the loop and ran the tests.", "the posted answer has no trailer")
out = run(*said("NO_REPLY" + TRAILER))
check(out.kind == NO_ACTION and out.title == "Fix login redirect", "NO_REPLY plus a trailer is no action, labelled")
out = run(*said("Answer." + TRAILER, meta={"ag2/activity": {"title": "From meta"}}))
check(out.title == "From meta" and out.summary == "Fixed the loop and ran the tests.", "meta wins over the trailer")
out = run(Done(reason=FAILED, text=TRAILER.strip()))
check(out.kind == FAILED_OUTCOME, "a trailer alone on a failed stop is still a failure")
check(out.title == "Fix login redirect" and out.summary == "",
      "a failure keeps its title only: the outcome line could claim a success")
out = run(*said(TRAILER.strip()))
check(out.kind == NO_ACTION and out.title == "Fix login redirect",
      "a trailer alone on a normal stop is no action, labelled")
secret = "ghp_" + "a" * 36
collab = ctx(access_tier="guest", requested_access_tier="team", collaborator=True,
             user_id="@peer:s.space")
out = run(*said(f"Fine.\n[activity:title] Leak {secret}\n[activity:outcome] ok"), c=collab)
check(out.title == "" and out.summary == "ok" and out.body == "Fine.",
      "a collaborator's label the guard would hold is dropped; the rest is kept")

print("-- worker --")


def handled(*events):
    client = FakeClient()
    asyncio.run(handle_one(task("t-1", "do it", room=ROOM), Scripted(*events), "/repo", client=client))
    return client


client = handled(*said("The answer." + TRAILER))
check(client.completed == [("t-1", "The answer.")]
      and client.labels.get("t-1") == ("Fix login redirect", "Fixed the loop and ran the tests."),
      "complete carries the labels, and the body has no trailer")
client = handled(*said("NO_REPLY\n[activity:title] Ack thanks"))
check(client.no_actions == [("t-1", "The agent had nothing to add.")]
      and client.labels.get("t-1") == ("Ack thanks", "The agent had nothing to add."),
      "a no action without an outcome keeps the default outcome, with the title")
client = handled(*said("plain"))
check(client.labels == {}, "no trailer, no labels")

print("-- framing --")
check(activity.INSTRUCTION in routing_preamble(ctx()), "the framing asks for the two lines")

print("\n" + ("PASS — activity labels green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
