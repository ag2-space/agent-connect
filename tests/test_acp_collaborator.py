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
               "collaborator": True, "sensitive_data_filter": True,
               "admitted_as": "collaborator"},
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
    posted_unasked = []

    async def post(destination, text):
        posted_unasked.append((destination, text))

    adapter.set_notifier(post)
    wired = {}
    real_connect = adapter._connect

    def spy_connect(*args, **kwargs):
        wired[current[0]] = kwargs.get("on_notification")
        return real_connect(*args, **kwargs)

    adapter._connect = spy_connect
    current = [""]

    async def drive(c, name):
        current[0] = name
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
    from agent_connect.adapters.acp import DESTINATION_META_KEY
    check(DESTINATION_META_KEY not in opened["a"][0]["params"]["_meta"],
          "a collaborator's Session gets no destination for unsolicited posts")
    check(DESTINATION_META_KEY in opened["o"][0]["params"]["_meta"], "the owner's still does")

    def receives(name):
        report = json.loads((base / f"{name}.json").read_text())
        return bool(((report.get("initialize") or {}).get("_meta") or {}).get("ag2/receive"))

    check(not receives("a") and not receives("b"),
          "a collaborator's Turn does not offer to receive unsolicited messages")
    check(receives("o"), "an owner's Turn still does")
    check(wired.get("a") == adapter._ignore_notification and wired.get("b") == adapter._ignore_notification,
          "a collaborator's Turn is wired to ignore notifications")
    check(wired.get("o") == adapter._on_notification, "an owner's Turn is wired to post them")

from agent_connect.link import NOTIFY_METHOD
from agent_connect.acp.core import Notification

relay = AcpAdapter(command=["true"])
got = []


async def record_post(destination, text):
    got.append(text)

relay.set_notifier(record_post)
note = Notification(NOTIFY_METHOD, {"destination": "!dm:s.space", "text": "secret"})
asyncio.run(relay._ignore_notification(note))
check(got == [], "a notification on a collaborator's Turn is never posted")
asyncio.run(relay._on_notification(note))
check(got == ["secret"], "while an owner's Turn posts it, as before")

os.environ[COLLABORATORS_ENV] = AGENT
try:
    relay._owner_rooms = {"!dm:s.space"}
    relay._store = SessionStore(Path(tempfile.mkdtemp()) / "s.json")
    got.clear()
    asyncio.run(relay._on_notification(Notification(NOTIFY_METHOD, {
        "destination": "!dm:s.space", "text": "key ghp_" + "a" * 36})))
    check(got == [], "with collaborators listed, a notification carrying a secret is withheld")
    asyncio.run(relay._on_notification(Notification(NOTIFY_METHOD, {
        "destination": "!elsewhere:s.space", "text": "done"})))
    check(got == [], "and one to a room with no owner Session is withheld")
    asyncio.run(relay._on_notification(Notification(NOTIFY_METHOD, {
        "destination": "!dm:s.space", "text": "nightly build passed"})))
    check(got == ["nightly build passed"], "while a clean one to an owner room is posted")
finally:
    os.environ.pop(COLLABORATORS_ENV, None)
got.clear()
asyncio.run(relay._on_notification(Notification(NOTIFY_METHOD, {
    "destination": "!elsewhere:s.space", "text": "key ghp_" + "a" * 36})))
check(len(got) == 1, "with no collaborators listed, notify is unchanged")

# --- card guards cannot collide across Sessions -------------------------------
from agent_connect import hitl_seam
tc = SimpleNamespace(tool_call={"toolCallId": "t1"}, options=[])
g1 = hitl_seam.guard_for(tc, "!r:s.space|collaborator:@x_y:s.space")
g2 = hitl_seam.guard_for(tc, "!r:s.space|collaborator:@x=y:s.space")
check(g1 != g2, "two Sessions that fold to the same text keep distinct guards")
check(hitl_seam.guard_for(SimpleNamespace(tool_call={"toolCallId": "t2"}, options=[]), "s")
      != hitl_seam.guard_for(tc, "s"), "the tool call id is part of the guard")
long_a = hitl_seam.guard_for(tc, "!r|collaborator:@" + "a" * 200 + "1:s.space")
long_b = hitl_seam.guard_for(tc, "!r|collaborator:@" + "a" * 200 + "2:s.space")
check(long_a != long_b and len(long_a) <= 120, "and long ones differ after truncation, within 120")

# --- an escalation card names a collaborator, and never the owner -----------
from types import SimpleNamespace as NS

from agent_connect import hitl_seam
from agent_connect.adapters import acp as acp_module
from agent_connect.events import PermissionAsked

request = NS(tool_call={"toolCallId": "t1", "title": "write /etc/x"},
             options=[{"optionId": "allow", "name": "Allow", "kind": "allow_once"}])
card = hitl_seam.event_for(request, "s", "Bob\n(@bob:s.space)")
check("asked by Bob (@bob:s.space), a collaborator" in card["prompt"] and "\n" not in card["prompt"],
      "a card for a collaborator names them, on one line")
check("asked by" not in hitl_seam.event_for(request, "s")["prompt"], "an owner's card names nobody")

with tempfile.TemporaryDirectory() as tmp:
    base = Path(tmp)
    repo = base / "repo"
    repo.mkdir()
    script = base / "script.json"
    script.write_text(json.dumps({"defaultTurn": {"actions": [{
        "type": "permission",
        "toolCall": {"toolCallId": "t1", "title": "write /etc/x", "kind": "edit",
                     "rawInput": {"path": "/etc/x"}, "locations": [{"path": "/etc/x"}]},
        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"}]}]}}))
    adapter = AcpAdapter(command=[sys.executable, FAKE, str(script)],
                         store=SessionStore(base / "sessions.json"))
    cards = []
    real_write = hitl_seam._write_atomic

    def capture(path, body):
        if body.get("kind") == "permission":
            cards.append(body["prompt"])
        real_write(path, body)

    hitl_seam._write_atomic = capture
    os.environ[hitl_seam.WORKSPACE_ENV] = str(base / "hitl")
    os.environ[hitl_seam.TIMEOUT_ENV] = "0.2"
    os.environ[COLLABORATORS_ENV] = AGENT
    try:
        def run_turn(c):
            async def go():
                return [e async for e in adapter.turn(c)]
            return asyncio.run(asyncio.wait_for(go(), timeout=30))

        collab = run_turn(TurnContext(prompt="hi", room="!r:s.space", cwd=str(repo),
                                      access_tier=GUEST, requested_access_tier="team",
                                      collaborator=True, user_id=AGENT, sender_name="cc-backend"))
        owner = run_turn(TurnContext(prompt="hi", room="!r:s.space", cwd=str(repo),
                                     access_tier=OWNER, user_id="@mark:s.space"))
    finally:
        hitl_seam._write_atomic = real_write
        os.environ.pop(hitl_seam.WORKSPACE_ENV, None)
        os.environ.pop(hitl_seam.TIMEOUT_ENV, None)
        os.environ.pop(COLLABORATORS_ENV, None)

    check(len(cards) == 2, f"both out-of-directory writes were escalated ({len(cards)})")
    check(bool(cards) and f'asked by {AGENT} ("cc-backend"), a collaborator' in cards[0],
          "the collaborator's escalation card names them")
    check(len(cards) == 2 and "asked by" not in cards[1], "the owner's does not")
    asks = [e for e in collab if isinstance(e, PermissionAsked)]
    check(bool(asks) and asks[0].detail.get("asked_by") == f'{AGENT} ("cc-backend")',
          "and the collaborator's PermissionAsked carries who asked")
    owner_asks = [e for e in owner if isinstance(e, PermissionAsked)]
    check(bool(owner_asks) and "asked_by" not in owner_asks[0].detail,
          "the owner's PermissionAsked does not")

# --- a display name cannot impersonate on the card ----------------------------
from agent_connect.adapters.acp import asked_by

spoof = TurnContext(prompt="p", access_tier=GUEST, collaborator=True, user_id="@evil:s.space",
                    sender_name="owner (@owner:example) \u2014 owner\u202e" + "x" * 300)
label = asked_by(spoof)
check(label.startswith("@evil:s.space "), "the attested Matrix id leads the label")
check("\u202e" not in label and len(label) < 80, "bidi overrides are stripped and the name is capped")
check(asked_by(TurnContext(prompt="p", access_tier=OWNER, user_id="@mark:s.space")) == "",
      "the owner gets no label")
check(requester_meta(TurnContext(prompt="p", sensitive_data_filter=False))[REQUESTER_META_KEY]
      ["sensitive_data_filter"] is False, "the filter flag is passed to the agent")

if fails:
    raise SystemExit(f"FAIL — {fails} check(s)")
print("PASS — ACP collaborator admission")
