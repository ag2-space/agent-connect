"""Addressing: what a shared room tells the Worker, and what the Worker says back.

Two halves of one fact about shared rooms. Inbound, the broker delivers whom a
message named, whether it replied to this agent and to whom, and who is in the
room; the Worker used to drop all of it on the floor (`reply_to_me` included),
so a Local Agent could not tell a hand-off from a question and could not
address the other agent if it tried. Outbound, the broker delivers a message to
an agent only when the agent's full mxid is on it — stamped in `m.mentions`, or
written whole in the body — so a message that names an agent has to be stamped
for it, and one that names nobody is a hand-off to nobody.

Everything here is pure: the delivered Task into a `TurnContext`, the
`TurnContext` into the two preambles, and a body into the mxids it names. What
the room actually receives is `test_ladder.py`'s, against a fake relay; what
the ACP Agent is actually handed is `test_acp_adapter.py`'s, against a fake
agent.

Run: python3 tests/test_addressing.py
"""
import _bootstrap  # noqa: F401 — puts the repo root on sys.path
import asyncio

from _taskqueue import task
from agent_connect import addressing
from agent_connect.adapters import ShimAdapter
from agent_connect.events import TurnContext
from agent_connect.reporter import mentioned_mxids
from agent_connect.sandbox import sandbox_preamble
from agent_connect.worker import process_one, turn_context

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


ROSTER = ("@ada:ag2.space", "@codex.agent:ag2.space", "@sutando-b.agent:ag2.space")
PEER = "@sutando-b.agent:ag2.space"


# --- inbound: the broker's routing facts reach the Turn, verbatim -----------

ctx = turn_context(
    task("t1", "hand this to the other agent", room="!room:ag2.space",
         user_id="@ada:ag2.space", addressed_to=PEER, reply_to_me=True,
         reply_to_sender="@codex.agent:ag2.space", room_members=list(ROSTER),
         room_member_count=5),
    "/repo")
check(ctx.addressed_to == PEER, "addressed_to crosses")
check(ctx.reply_to_me is True, "reply_to_me crosses — it used to be dropped here")
check(ctx.reply_to_sender == "@codex.agent:ag2.space", "reply_to_sender crosses")
check(ctx.room_members == ROSTER, "the roster crosses, as a tuple of full mxids")
check(ctx.room_member_count == 5, "and the count beside it")
check(ctx.prompt == "hand this to the other agent",
      "and none of it touches what the person typed")

capped = turn_context(
    task("t2", "x", room_members="@ada:ag2.space, @codex.agent:ag2.space (+3 more)",
         room_member_count=5),
    "/repo")
check(capped.room_members == ("@ada:ag2.space", "@codex.agent:ag2.space"),
      "the broker's capped string form arrives as the members it names")

plain = turn_context(task("t3", "hi"), "/repo")
check((plain.addressed_to, plain.reply_to_me, plain.reply_to_sender,
       plain.room_members, plain.room_member_count) == ("", False, "", (), 0),
      "a Task that carried none of it has the empty values, not guesses")
check((TurnContext(prompt="x").room_members, TurnContext(prompt="x").addressed_to,
       TurnContext(prompt="x").reply_to_me) == ((), "", False),
      "and a TurnContext built by hand defaults the same way")


# --- the preamble: who else is here, and who was asked ----------------------

lines = addressing.addressing_preamble(ROSTER, "", 3)
check(lines.startswith("Others in this room: " + ", ".join(ROSTER) + "."),
      "the roster is stated, as full mxids")
check("write its full mxid in your answer" in lines
      and "is not delivered to it" in lines,
      "with the rule: the mxid is the address, and a message naming nobody "
      "reaches nobody")
check("addressed to" not in lines, "and nothing about an addressee when there was none")
check(addressing.addressing_preamble(ROSTER, "", 5).startswith(
          "Others in this room: " + ", ".join(ROSTER) + " and 2 more."),
      "a capped roster says how many it did not name — a short list is not a "
      "small room")
check(addressing.addressing_preamble(ROSTER, "", 0).startswith(
          "Others in this room: " + ", ".join(ROSTER) + "."),
      "and a count the broker did not send adds nothing")
check(addressing.addressing_preamble((), PEER)
      == f"This message is addressed to {PEER}; answer only if it asks you too.\n",
      "an addressee is stated, and the agent is left to read whether it is the one")
check(addressing.addressing_preamble(ROSTER, PEER, 3).count("\n") == 2,
      "both together are two lines, each ending in a newline")
check(addressing.addressing_preamble() == ""
      and addressing.addressing_preamble((), "  ") == ""
      and addressing.addressing_preamble(("", None), "") == "",
      "nothing to say, nothing said")


# --- the shim's preamble carries it, after the sandbox line, before the prompt

p = sandbox_preamble("workspace-write", "owner", room_members=ROSTER,
                     addressed_to=PEER, room_member_count=3)
check(p.startswith("[agent-connect: this run's sandbox is 'workspace-write'"),
      "the sandbox line still leads")
check("Others in this room: @ada:ag2.space" in p and f"addressed to {PEER}" in p,
      "the addressing lines follow it")
check(p.index("[file:") < p.index("Others in this room"),
      "after the file instruction, which is about this run rather than the room")
check(p.endswith("too.\n\n"), "and the blank line before the prompt is still last")
check(sandbox_preamble("workspace-write", "owner")
      == sandbox_preamble("workspace-write", "owner", (), "", 0),
      "with nothing to say the preamble is exactly what it was")
check("Others in this room" not in sandbox_preamble("read-only", "guest")
      and "addressed to" not in sandbox_preamble("read-only", "guest"),
      "a Task with no roster says nothing about one")
check("Others in this room: @ada:ag2.space" in
      sandbox_preamble("read-only", "guest", room_members=ROSTER),
      "a read-only run is told who is there too — addressing is routing, not "
      "privilege")


class StubAdapter:
    def __init__(self):
        self.calls = []

    def run(self, task, sandbox, cwd):
        self.calls.append(task)
        return "ok"


stub = StubAdapter()
asyncio.run(process_one(
    task("t4", "who is here?", room="!room:ag2.space", room_members=list(ROSTER),
         room_member_count=3, addressed_to="@codex.agent:ag2.space"),
    ShimAdapter("stub", stub), "/repo"))
sent = stub.calls[0]
check("Others in this room: " + ", ".join(ROSTER) + "." in sent,
      "through the Worker, a shimmed Adapter is handed the roster")
check("addressed to @codex.agent:ag2.space" in sent, "and the addressee")
check(sent.endswith("\n\nwho is here?"), "and the person's words, last and untouched")


# --- outbound: the mxids a body names ---------------------------------------

check(mentioned_mxids(f"{PEER} can you take this? cc @ada:ag2.space.")
      == [PEER, "@ada:ag2.space"],
      "full mxids are found in an answer, in order, punctuation aside")
check(mentioned_mxids("ask sutando-b.agent or @sutando-b about it") == [],
      "a localpart or a display name is not an address — only a full mxid routes")
check(mentioned_mxids("@ada:ag2.space and @ada:ag2.space again") == ["@ada:ag2.space"],
      "each once")
check(mentioned_mxids("@ada:ag2.space and @nobody:elsewhere.example", ROSTER)
      == ["@ada:ag2.space"],
      "with a roster, only members are kept — a stranger is a mention nobody "
      "receives")
check(mentioned_mxids("@nobody:elsewhere.example", ()) == ["@nobody:elsewhere.example"],
      "with no roster nothing is filtered: the Worker does not know who is "
      "not there")
check(mentioned_mxids("") == [] and mentioned_mxids("nothing here", ROSTER) == [],
      "and a body naming nobody stamps nobody")

print("\n" + ("PASS — addressing green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
