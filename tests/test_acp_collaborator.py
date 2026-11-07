"""ACP admits a non-owner only as a listed sender the broker attested Team + collaborator."""
import _bootstrap  # noqa: F401

import asyncio
from types import SimpleNamespace

from agent_connect.adapters.acp import COLLABORATORS_ENV, REFUSAL, AcpAdapter, admitted
from agent_connect.events import REFUSED, TurnContext
from agent_connect.worker import GUEST, OWNER, attested_collaborator

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


AGENT = "@mark-cc-backend.agent:s.space"
ENV = {COLLABORATORS_ENV: f"{AGENT}, @other:s.space"}


def task(requested="team", collaborator=True):
    return SimpleNamespace(requested_access_tier=requested, collaborator=collaborator)


check(attested_collaborator(task()), "Team + collaborator true is a collaborator")
check(not attested_collaborator(task(collaborator=False)), "Team without collaborator is not")
check(not attested_collaborator(task(collaborator="true")), "only the exact boolean counts")
check(not attested_collaborator(task(requested="guest")), "guest with collaborator is not")
check(not attested_collaborator(SimpleNamespace()), "a task without the fields is not")


def ctx(tier=GUEST, collaborator=True, user=AGENT):
    return TurnContext(prompt="hi", room="!r:s.space", access_tier=tier,
                       collaborator=collaborator, user_id=user)


check(admitted(ctx(tier=OWNER, collaborator=False, user=""), {}), "owner is always admitted")
check(admitted(ctx(), ENV), "listed collaborator is admitted")
check(not admitted(ctx(), {}), "unlisted collaborator is refused")
check(not admitted(ctx(user="@stranger:s.space"), ENV), "a collaborator not on the list is refused")
check(not admitted(ctx(collaborator=False), ENV), "a listed sender without Team collaborator is refused")
check(not admitted(ctx(user=""), {COLLABORATORS_ENV: ""}), "no user id is refused")


async def first_event(c, env):
    import os
    saved = os.environ.get(COLLABORATORS_ENV)
    os.environ.update(env) if env else os.environ.pop(COLLABORATORS_ENV, None)
    try:
        async for event in AcpAdapter().turn(c):
            return event
    finally:
        if saved is None:
            os.environ.pop(COLLABORATORS_ENV, None)
        else:
            os.environ[COLLABORATORS_ENV] = saved


refused = asyncio.run(first_event(ctx(), {}))
check(refused.reason == REFUSED, "turn refuses an unlisted collaborator")
check("on this agent alone" in REFUSAL, "the refusal names the narrower option")
passed = asyncio.run(first_event(ctx(), ENV))
check(getattr(passed, "reason", None) != REFUSED, "turn does not refuse a listed collaborator")

# --- per-sender Sessions and the requester metadata, on the wire -------------
import json
import os
import sys
import tempfile
from pathlib import Path

from agent_connect.adapters.acp import REQUESTER_META_KEY, requester_meta
from agent_connect.sessions import SessionStore

OTHER = "@other:s.space"
check(ctx().session_key == ("!r:s.space", f"collaborator:{AGENT}"),
      "a collaborator's Session is keyed by sender")
check(ctx(user=OTHER).session_key != ctx().session_key, "two collaborators never share one")
check(ctx(tier=OWNER, collaborator=False).session_key == ("!r:s.space", OWNER),
      "the owner's key is unchanged")
check(ctx(collaborator=False).session_key == ("!r:s.space", GUEST), "a plain guest's key is unchanged")

meta = requester_meta(TurnContext(prompt="p", access_tier=GUEST, requested_access_tier="team",
                                  collaborator=True, user_id=AGENT))[REQUESTER_META_KEY]
check(meta == {"user_id": AGENT, "access_tier": GUEST, "requested_access_tier": "team",
               "collaborator": True, "admitted_as": "collaborator"},
      "requester metadata keeps the sender, both tiers, the flag and the admission apart")
check(requester_meta(ctx(tier=OWNER))[REQUESTER_META_KEY]["admitted_as"] == "owner",
      "the owner is admitted as owner")

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")
with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    script = base / "script.json"
    script.write_text(json.dumps({}))
    adapter = AcpAdapter(command=[sys.executable, FAKE, str(script)],
                         store=SessionStore(base / "sessions.json"))

    async def drive(c, name):
        os.environ["FAKE_ACP_REPORT"] = str(base / f"{name}.json")
        os.environ[COLLABORATORS_ENV] = f"{AGENT} {OTHER}"
        try:
            return [event async for event in adapter.turn(c)]
        finally:
            os.environ.pop("FAKE_ACP_REPORT", None)
            os.environ.pop(COLLABORATORS_ENV, None)

    def seen(name):
        report = json.loads((base / f"{name}.json").read_text())
        return report["sessions"][0], report["prompts"][0]

    turns = {
        "a": TurnContext(prompt="hi", room="!r:s.space", cwd=str(base), access_tier=GUEST,
                         requested_access_tier="team", collaborator=True, user_id=AGENT),
        "b": TurnContext(prompt="hi", room="!r:s.space", cwd=str(base), access_tier=GUEST,
                         requested_access_tier="team", collaborator=True, user_id=OTHER),
        "o": TurnContext(prompt="hi", room="!r:s.space", cwd=str(base), access_tier=OWNER,
                         user_id="@mark:s.space"),
    }
    for name, c in turns.items():
        events = asyncio.run(asyncio.wait_for(drive(c, name), timeout=30))
        check(all(getattr(e, "reason", None) != REFUSED for e in events), f"turn {name} ran")
    opened = {name: seen(name) for name in turns}
    check(all(o[0]["method"] == "session/new" for o in opened.values()),
          "no Turn resumed another sender's Session")
    stored = json.loads((base / "sessions.json").read_text())["sessions"]
    check(sorted(e["access_tier"] for e in stored) == sorted(
              [f"collaborator:{AGENT}", f"collaborator:{OTHER}", OWNER]),
          "the Session map holds one Session per sender")
    sent = opened["a"][1]["_meta"][REQUESTER_META_KEY]
    check(sent["user_id"] == AGENT and sent["admitted_as"] == "collaborator",
          "session/prompt carried the requester under _meta")
    check(opened["a"][0]["params"]["_meta"][REQUESTER_META_KEY]["user_id"] == AGENT,
          "and so did session/new")
    check(opened["o"][1]["_meta"][REQUESTER_META_KEY]["admitted_as"] == "owner",
          "the owner's prompt says owner")

if fails:
    raise SystemExit(f"FAIL — {fails} check(s)")
print("PASS — ACP collaborator admission")
