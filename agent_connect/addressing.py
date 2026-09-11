"""What an agent is told about the room it is answering in, and who was asked.

Two facts the broker attaches to a Task in a shared room, and one rule about
them that no Local Agent could know on its own:

*Who else is here.* `room_members` — full mxids, capped by the broker. An agent
that wants to hand a question to another agent has to address it, and on this
platform the address is the mxid: the broker delivers a message to an agent
when the agent's mxid is stamped on it (`m.mentions`) or written whole in the
body, and it stamps the mention itself for any room member it finds there. An
answer that says "ask the other agent" names nobody and reaches nobody — in a
shared room the other agent ignores agent-authored messages it is not named in,
by design — and the model would find that out from nothing at all. So the
roster is put in front of it, with the rule.

*Who was asked.* `addressed_to` is the broker's reading of whom the message
named. It is stated rather than judged: the Worker never learns its own mxid
(`adapters.acp.concierge_for` says why), so whether the name is this agent's is
for the agent to read, and the sentence is written so that either reading is
right — an agent the message asks answers it.

Written once, here, because two preambles say it. The shim's sandbox line and
the ACP framing describe different confinements and must not inherit each
other's sentences (`sandbox.sandbox_preamble`, `adapters.acp.preamble`); the
room, and the broker's routing rule, are the same for both. Like `events`, this
module imports nothing from the rest of the package: both preambles depend on
it, and it must not depend on either.
"""
from __future__ import annotations

from typing import Sequence

#: What the agent is told about who else is in the room. One sentence, and the
#: rule it needs with it: the mxid is the address, and a message that carries
#: none is not a hand-off.
ROSTER = (
    "Others in this room: {members}. To hand a question to another agent, "
    "write its full mxid in your answer; a message that does not name an agent "
    "is not delivered to it."
)

#: How a roster the broker capped is finished. The list names who it names and
#: the count says how many it did not; a short list must not read as a small
#: room.
ROSTER_MORE = "{members} and {more} more"

#: What the agent is told when the message was addressed to someone in
#: particular. "Too" is deliberate: a message may name this agent as well as
#: another, and an agent it asks answers it.
ADDRESSED = "This message is addressed to {who}; answer only if it asks you too."


def addressing_preamble(
    room_members: Sequence[str] = (),
    addressed_to: str = "",
    room_member_count: int = 0,
) -> str:
    """The addressing lines for one Turn, each ending in a newline — or `""`.

    Empty when the Task carried nothing to say: a direct message has no roster
    worth stating and names nobody, and a preamble that said so anyway would be
    a sentence about nothing in front of every question.
    """
    lines = []
    members = [member for member in room_members if member]
    if members:
        named = ", ".join(members)
        more = int(room_member_count or 0) - len(members)
        if more > 0:
            named = ROSTER_MORE.format(members=named, more=more)
        lines.append(ROSTER.format(members=named))
    who = (addressed_to or "").strip()
    if who:
        lines.append(ADDRESSED.format(who=who))
    return "".join(line + "\n" for line in lines)
