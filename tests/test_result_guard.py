"""A collaborator's reply is checked before it reaches the room (`agent_connect.result_guard`)."""
import _bootstrap  # noqa: F401

import asyncio
import json
import os
import tempfile
from pathlib import Path

from agent_connect import hitl_seam, result_guard
from agent_connect.events import Done, MessageChunk, TurnContext
from agent_connect.reporter import HELD, TurnReporter

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


SAMPLES = {
    "AWS access key": "AKIA" + "ABCDEFGHIJKLMNOP",
    "AWS access key (temporary)": "ASIA" + "ABCDEFGHIJKLMNOP",
    "GitHub token": "ghp_" + "a" * 36,
    "GitHub fine-grained token": "github_pat_" + "a" * 40,
    "Google API key": "AIza" + "b" * 35,
    "Matrix access token": "syt_" + "c" * 24,
    "JSON Web Token": "eyJhbGciOi.eyJzdWIiOi.sig_part",
    "Slack token": "xoxb-1234-abcd",
    "OpenAI key": "sk-proj-" + "d" * 40,
    "Anthropic key": "sk-ant-" + "e" * 40,
    "Stripe key": "sk_live_" + "f" * 30,
    "Telegram bot token": "123456789:" + "g" * 35,
    "private key": "-----BEGIN OPENSSH PRIVATE KEY-----",
}
for name, sample in SAMPLES.items():
    check(name.split(" (")[0] in result_guard.secret_types(f"here it is: {sample} ok"), f"detects a {name}")
check(result_guard.secret_types("The build passed; see worker.py line 12.") == (),
      "ordinary prose has no secrets")

COLLAB = dict(prompt="p", room="!r:s.space", access_tier="guest", collaborator=True,
              user_id="@cc:s.space", task_id="t1")


def ctx(**kw):
    return TurnContext(**{**COLLAB, **kw})


owner = TurnContext(prompt="p", room="!r:s.space", access_tier="owner")
check(not result_guard.verdict(owner, SAMPLES["GitHub token"]).hold, "the owner is never held")
check(not result_guard.verdict(ctx(collaborator=False), SAMPLES["GitHub token"]).hold,
      "a non-collaborator never reaches here, and is not held")
check(not result_guard.verdict(ctx(), "All done.").hold, "a clean collaborator reply passes")
v = result_guard.verdict(ctx(), "token " + SAMPLES["GitHub token"])
check(v.hold and v.why == ("a possible GitHub token",), "a secret holds the reply, naming its type")
check(result_guard.verdict(ctx(), "see [file: /tmp/x.png]").why == ("a file, which cannot be scanned",),
      "a file holds the reply")
check(result_guard.verdict(ctx(), "[channel: !other:s.space]\nhi").hold,
      "a redirect holds the reply")
check(result_guard.verdict(ctx(sensitive_data_filter=False), "[channel: !other:s.space]\nhi").hold,
      "a redirect holds it even with the filter off")
off = ctx(sensitive_data_filter=False)
check(not result_guard.verdict(off, SAMPLES["GitHub token"] + " [file: /tmp/x]").hold,
      "with the room's filter off, secrets and files pass")


def broken(_):
    raise RuntimeError("detector broke")


check(result_guard.verdict(ctx(), "fine", scan=broken).why == ("a scan that failed",),
      "a failed scan holds the reply")

# --- the Ladder: held, then released or withheld --------------------------------


class Ops:
    available = True

    def __init__(self):
        self.messages, self.edits = [], []

    async def message(self, room, text, mentions=None):
        self.messages.append(text)
        return "$placeholder"

    async def edit(self, room, event_id, body):
        self.edits.append(body)


class Adapter:
    def __init__(self, text):
        self.text = text

    async def turn(self, c):
        yield MessageChunk(text=self.text)
        yield Done()


def run_turn(c, text, answer=None, hitl=True):
    """One Turn through the reporter; `answer` is the owner's click, or None for none."""
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        if hitl:
            os.environ[hitl_seam.WORKSPACE_ENV] = str(ws)
        os.environ[hitl_seam.TIMEOUT_ENV] = "2.5"
        cards = []

        async def owner_clicks():
            events = ws / "state" / "hitl" / "events"
            while True:
                for path in events.glob("*.json") if events.is_dir() else ():
                    posted = json.loads(path.read_text())
                    if posted.get("kind") == "permission" and not cards:
                        cards.append(posted)
                        if answer:
                            req = ws / "state" / "hitl" / "requirements"
                            req.mkdir(parents=True, exist_ok=True)
                            (req / "hitl_1.json").write_text(json.dumps({"requirement": {
                                "id": "hitl_1", "guard": posted["guard"],
                                "chosen_action": answer, "status": "in_progress"}}))
                await asyncio.sleep(0.01)

        async def go():
            ops = Ops()
            clicker = asyncio.create_task(owner_clicks())
            try:
                body = await TurnReporter(ops).run(Adapter(text), c)
            finally:
                clicker.cancel()
            return ops, body

        try:
            ops, body = asyncio.run(asyncio.wait_for(go(), timeout=30))
        finally:
            os.environ.pop(hitl_seam.WORKSPACE_ENV, None)
            os.environ.pop(hitl_seam.TIMEOUT_ENV, None)
        return ops, body, cards


secret_reply = "the key is " + SAMPLES["GitHub token"]

ops, body, cards = run_turn(ctx(sender_name="cc"), secret_reply, answer=result_guard.RELEASE)
check(HELD in ops.edits, "a held reply tells the room it is waiting for the owner")
check(bool(cards) and "a possible GitHub token" in cards[0]["prompt"]
      and "@cc:s.space" in cards[0]["prompt"],
      "the owner's card names the reason and the collaborator")
check(SAMPLES["GitHub token"] not in (cards[0]["prompt"] + cards[0]["subject"]["input"] if cards else ""),
      "the card never shows the secret itself")
check(SAMPLES["GitHub token"] in body or any(SAMPLES["GitHub token"] in e for e in ops.edits),
      "a released reply is posted")

ops, body, _ = run_turn(ctx(), secret_reply, answer=result_guard.DISCARD)
posted = "\n".join(ops.edits + ops.messages) + body
check(SAMPLES["GitHub token"] not in posted, "a discarded reply is never posted or returned")
check("withheld" in posted and "GitHub token" in posted, "the room is told it was withheld, and why")

ops, body, _ = run_turn(ctx(), secret_reply, answer=None)
check(SAMPLES["GitHub token"] not in "\n".join(ops.edits) + body, "a timeout withholds it")

ops, body, _ = run_turn(ctx(), secret_reply, hitl=False)
check(SAMPLES["GitHub token"] not in "\n".join(ops.edits) + body,
      "with no approval cards configured, it is withheld")

ops, body, cards = run_turn(ctx(), "All done.")
check(not cards and HELD not in ops.edits and "All done." in "\n".join(ops.edits) + body,
      "a clean collaborator reply is posted without asking")

ops, body, cards = run_turn(owner, secret_reply)
check(not cards and SAMPLES["GitHub token"] in "\n".join(ops.edits) + body,
      "the owner's reply is never held")

if fails:
    raise SystemExit(f"FAIL — {fails} check(s)")
print("PASS — collaborator result guard")
