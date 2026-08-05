"""Interoperability check: the ACP core against the *real* Claude Code bridge.

This is the check `docs/adr/0001` and the ACP spec said had to be settled before
anything was built on top — the Python schema library is on a visibly lower
version line than the bridge's TypeScript one, and if they did not talk, the
core would have had to hand-roll the JSON-RPC subset instead.

It is **opt-in**, because unlike every other test in this repository it needs a
real Local Agent, real credentials, real network, and real tokens:

    ACP_REAL_BRIDGE=1 python3 test_acp_real_bridge.py

Without that, it prints why it did nothing and exits 0, so it is safe to leave
in a suite that runs on machines with no bridge installed.

The bridge command defaults to `claude-agent-acp` (npm:
`@agentclientprotocol/claude-agent-acp`) and is overridable:

    AGENT_CONNECT_ACP_COMMAND="npx -y @agentclientprotocol/claude-agent-acp@0.64.2"
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil

COMMAND = shlex.split(
    os.environ.get("AGENT_CONNECT_ACP_COMMAND") or "claude-agent-acp"
)

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def skip(why: str) -> None:
    print(f"SKIP — {why}")
    raise SystemExit(0)


if not os.environ.get("ACP_REAL_BRIDGE"):
    skip("set ACP_REAL_BRIDGE=1 to run this (it spends real tokens)")
if shutil.which(COMMAND[0]) is None:
    skip(f"{COMMAND[0]} not on PATH — install the bridge or set "
         "AGENT_CONNECT_ACP_COMMAND")

# Imported after the skips so this file stays runnable — and honest about why it
# did nothing — in an environment without the ACP dependency installed.
from agent_connect.acp import AcpClient  # noqa: E402


async def main():
    seen = []
    async with AcpClient.spawn(COMMAND, on_update=seen.append) as client:
        agent = await client.initialize()
        print(f"  ... {agent.name} {agent.version}, protocol {agent.protocol_version}")
        session_id = await client.new_session(cwd=os.getcwd())
        turn = await client.prompt(
            session_id,
            "Reply with exactly the word PONG and nothing else. Do not use any tools.",
        )
    return agent, session_id, turn, seen


agent, session_id, turn, seen = asyncio.run(asyncio.wait_for(main(), timeout=180))

check(agent.protocol_version == 1, "the bridge negotiates protocol version 1")
check(bool(agent.name), "the bridge identifies itself")
check(bool(session_id), "a Session opens with a working directory")
check("PONG" in turn.text.upper(), f"the agent's text answer comes back ({turn.text!r})")
check(turn.stop_reason == "end_turn", f"the Turn stops normally ({turn.stop_reason!r})")
check(any(u.kind == "agent_message_chunk" for u in seen),
      "progress notifications are surfaced to the caller")

print("\n" + ("PASS — real bridge interoperates" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
