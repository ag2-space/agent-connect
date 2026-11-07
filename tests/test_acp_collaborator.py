"""ACP admits a non-owner only as a listed sender the broker attested Team + collaborator."""
import _bootstrap  # noqa: F401

import asyncio
import uuid
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
    from agent_connect.adapters.acp import NOTIFY_WITHHELD
    check(got == [NOTIFY_WITHHELD],
          "with collaborators listed, a notification carrying a secret is withheld, and the room told")
    got.clear()
    asyncio.run(relay._on_notification(Notification(NOTIFY_METHOD, {
        "destination": "!elsewhere:s.space", "text": "done"})))
    check(got == [], "and one to a room with no owner Session is withheld silently")
    asyncio.run(relay._on_notification(Notification(NOTIFY_METHOD, {
        "destination": "!dm:s.space", "text": "nightly build passed"})))
    check(got == ["nightly build passed"], "while a clean one to an owner room is posted")
finally:
    os.environ.pop(COLLABORATORS_ENV, None)
got.clear()
asyncio.run(relay._on_notification(Notification(NOTIFY_METHOD, {
    "destination": "!elsewhere:s.space", "text": "key ghp_" + "a" * 36})))
check(len(got) == 1, "with no collaborators listed, notify is unchanged")

with tempfile.TemporaryDirectory() as tmp:
    owned = AcpAdapter(command=["true"], store=SessionStore(Path(tmp) / "s.json"))
    owned.store().remember(("!owned:s.space", "owner"), "sess-1", tmp, 1)
    owned.store().remember(("!r:s.space", f"collaborator:{AGENT}"), "sess-2", tmp, 1)
    check(owned.owner_rooms() == {"!owned:s.space"},
          "owner rooms come from the Session map's owner keys only")

# --- an action the Policy cannot allow is put to the owner privately -------
from types import SimpleNamespace as NS

from agent_connect.adapters.acp import AWAITING_APPROVAL, allow_option
from agent_connect.events import Notice, PermissionAsked
from agent_connect.questions import OwnerQuestions


class FakeSync:
    """The relay client's `questions`: records asks, answers with `choice`."""

    def __init__(self, choice, private=True):
        self.choice, self.private, self.asked = choice, private, []

    def ask(self, prompt, options, *, expires_in_s, operation_id):
        self.asked.append((prompt, tuple(options), operation_id))
        return f"q{len(self.asked)}" if self.private else None

    def inspect(self, question_id):
        if self.choice is None:
            return NS(status="pending", option="", revision="1")
        return NS(status="answered", option=self.choice, revision="2")


check(allow_option(NS(options=[{"optionId": "a2", "kind": "allow_always"},
                               {"optionId": "a1", "kind": "allow_once"}])) == "a1",
      "allow once is preferred over allow always")
check(allow_option(NS(options=[{"optionId": "r", "kind": "reject_once"}])) == "",
      "a request with no allow option cannot be allowed")

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

    def run_turn(c, sync):
        adapter = AcpAdapter(command=[sys.executable, FAKE, str(script)],
                             store=SessionStore(base / f"s-{uuid.uuid4().hex}.json"))
        adapter.set_questions(OwnerQuestions(sync) if sync is not None else None)
        report = base / "report.json"
        os.environ["FAKE_ACP_REPORT"] = str(report)
        os.environ["AGENT_CONNECT_APPROVAL_TIMEOUT"] = "3"
        os.environ[COLLABORATORS_ENV] = AGENT
        try:
            async def go():
                return [e async for e in adapter.turn(c)]
            events = asyncio.run(asyncio.wait_for(go(), timeout=30))
        finally:
            for name in ("FAKE_ACP_REPORT", "AGENT_CONNECT_APPROVAL_TIMEOUT", COLLABORATORS_ENV):
                os.environ.pop(name, None)
        answer = json.loads(report.read_text())["permissions"][0]["answer"]
        return events, json.dumps(answer)

    collab_ctx = TurnContext(prompt="hi", room="!r:s.space", room_name="OpenClaw Setup", cwd=str(repo),
                             access_tier=GUEST, requested_access_tier="team",
                             collaborator=True, user_id=AGENT, sender_name="cc-backend")
    owner_ctx = TurnContext(prompt="hi", room="!r:s.space", cwd=str(repo),
                            access_tier=OWNER, user_id="@mark:s.space")

    allowing = FakeSync("allow")
    events, answer = run_turn(collab_ctx, allowing)
    prompt = allowing.asked[0][0] if allowing.asked else ""
    check(bool(allowing.asked) and allowing.asked[0][1] == (("allow", "Allow"), ("deny", "Deny")),
          "the owner is asked privately, with Allow and Deny")
    check("write /etc/x" in prompt and "/etc/x" in prompt and "OpenClaw Setup" in prompt
          and f'{AGENT} ("cc-backend"), a collaborator, not you' in prompt,
          "the question says what, where, in which room, and which collaborator asked")
    check('"allow"' in answer, "the owner's Allow lets the action through")
    check(any(isinstance(e, Notice) and e.text == AWAITING_APPROVAL for e in events),
          "the room is told the Turn is waiting for the owner, without the details")
    asks = [e for e in events if isinstance(e, PermissionAsked)]
    check(bool(asks) and asks[0].allowed and asks[0].detail.get("asked_by") == f'{AGENT} ("cc-backend")',
          "and PermissionAsked records the allow and who asked")

    events, answer = run_turn(owner_ctx, FakeSync("deny"))
    check('"reject"' in answer, "Deny refuses it")
    owner_asks = [e for e in events if isinstance(e, PermissionAsked)]
    check(bool(owner_asks) and not owner_asks[0].allowed and "asked_by" not in owner_asks[0].detail,
          "the owner's own refusal names nobody")

    pending = FakeSync(None)
    events, answer = run_turn(owner_ctx, pending)
    check('"reject"' in answer, "no answer before the timeout refuses it")
    check("This is your own turn." in pending.asked[0][0], "the owner's question says it is their turn")

    events, answer = run_turn(owner_ctx, FakeSync("allow", private=False))
    check('"reject"' in answer and not any(isinstance(e, Notice) and e.text == AWAITING_APPROVAL
                                           for e in events),
          "with no private DM to ask in, it is refused and nobody is told to wait")

    events, answer = run_turn(owner_ctx, None)
    check('"reject"' in answer, "with no questions at all, the Policy's refusal stands")

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
