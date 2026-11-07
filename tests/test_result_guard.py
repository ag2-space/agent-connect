"""A collaborator's reply is checked before it reaches the room (`agent_connect.result_guard`)."""
import _bootstrap  # noqa: F401

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

from agent_connect import hitl_seam, result_guard
from agent_connect.events import Done, MessageChunk, TurnContext
from types import SimpleNamespace

from agent_connect.reporter import TurnReporter

NS_REQ = SimpleNamespace(tool_call={"toolCallId": "t1", "title": "x"}, options=[])

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
UNCHECKED = {result_guard.UNCHECKED_ENV: "1"}
check(result_guard.verdict(ctx(), "[channel: !other:s.space]\nhi", env=UNCHECKED).hold,
      "a redirect holds it even when the owner turned scanning off")
check(not result_guard.verdict(ctx(), SAMPLES["GitHub token"] + " [file: /tmp/x]", env=UNCHECKED).hold,
      "with the owner's opt-out, secrets and files pass")
check(result_guard.verdict(ctx(sensitive_data_filter=False), SAMPLES["GitHub token"], env={}).hold,
      "the room's filter flag alone cannot turn the check off")


def broken(_):
    raise RuntimeError("detector broke")


check(result_guard.verdict(ctx(), "fine", scan=broken).why == ("a scan that failed",),
      "a failed scan holds the reply")

# --- the Ladder: held for the owner, released or discarded from the room ------

from agent_connect import withheld
from agent_connect.worker import process_one


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


def run_turn(c, text, ops=None):
    async def go():
        o = Ops() if ops is None else ops
        return o, await TurnReporter(o).run(Adapter(text), c)
    return asyncio.run(go())


def owner_says(text, reply_to="$placeholder", room="!r:s.space", tier="owner"):
    task = SimpleNamespace(
        id="t-owner", body=text, room_id=room, room_name="", access_tier=tier,
        requested_access_tier="", collaborator=False, sensitive_data_filter=True,
        sender_name="Mark", user_id="@mark:s.space", source_message_id="$m",
        addressed_to="", reply_to_me=True, reply_to_sender="", reply_to_event=reply_to,
        room_members=(), room_member_count=0, attachments=())

    class Never:
        async def turn(self, c):
            raise AssertionError("a release must never reach the agent")
            yield  # pragma: no cover

    return asyncio.run(process_one(task, Never(), "/tmp"))


secret_reply = "the key is " + SAMPLES["GitHub token"]
workspace = tempfile.mkdtemp()
os.environ["AGENT_CONNECT_WORKSPACE"] = workspace
held_dir = Path(workspace) / "withheld"

ops, body = run_turn(ctx(), secret_reply)
room_text = "\n".join(ops.edits + ops.messages) + body
check(SAMPLES["GitHub token"] not in room_text, "a held reply never reaches the room")
check("GitHub token" not in room_text, "and the room is not told which secret type matched")
check("reply to this message" in room_text and "`release`" in room_text and "`discard`" in room_text,
      "the withheld message tells the owner how to release or discard it")
files = list(held_dir.glob("wr_*.json"))
check(len(files) == 1 and json.loads(files[0].read_text())["body"] == secret_reply,
      "the exact reply is stored on the Worker's machine")
check(oct(held_dir.stat().st_mode & 0o777) == "0o700" and oct(files[0].stat().st_mode & 0o777) == "0o600",
      "owner-only: the folder 0700, the file 0600")
check(files[0].name.split(".")[0] in room_text, "the message names the review id")

review_id = files[0].name.split(".")[0]
check(json.loads(files[0].read_text())["event_id"] == "$placeholder",
      "the review is bound to the room message that announced it")


def as_ctx(text, **kw):
    base = dict(prompt=text, room="!r:s.space", access_tier="owner",
                reply_to_event="$placeholder", reply_to_me=True)
    return TurnContext(**{**base, **kw})


check(withheld.decide(as_ctx("release", access_tier="guest", collaborator=True)) is None,
      "a collaborator saying release releases nothing")
check(withheld.decide(as_ctx("release", room="!other:s.space")) is not None
      and files[0].exists(), "a release in another room does not touch this review")
check(withheld.decide(as_ctx("please release the build notes")) is None,
      "an ordinary sentence is not a command")
released = owner_says("release")
check(released.startswith(secret_reply) and withheld.RELEASED_NOTE in released,
      "the owner's reply 'release' posts the exact held reply")
check(not files[0].exists(), "and the stored copy is deleted")
check(owner_says("release") == withheld.NOT_FOUND, "a second release finds nothing")

ops, body = run_turn(ctx(), secret_reply)
review_id = next(held_dir.glob("wr_*.json")).name.split(".")[0]
check(owner_says(f"release {review_id}", reply_to="").startswith(secret_reply),
      "`release <id>` works without replying to the message")

ops, body = run_turn(ctx(), secret_reply)
check(owner_says("discard") == withheld.DISCARDED and not list(held_dir.glob("wr_*.json")),
      "discard deletes it without posting")

ops, body = run_turn(ctx(), secret_reply)
path = next(held_dir.glob("wr_*.json"))
record = json.loads(path.read_text())
record["body"] = "something else the agent wrote"
path.write_text(json.dumps(record))
check(owner_says("release") == withheld.TAMPERED, "a reply changed on disk after it was held is refused")

ops, body = run_turn(ctx(), secret_reply)
withheld.sweep(now=time.time() + withheld.TTL_S + 1)
check(not list(held_dir.glob("wr_*.json")), "reviews expire after 24 hours")

ops, body = run_turn(ctx(), "[channel: !other:s.space]\nhi")
check(not list(held_dir.glob("wr_*.json")) and "cannot be released" in "\n".join(ops.edits) + body,
      "a redirect is withheld and never releasable")

ops, body = run_turn(ctx(), "All done.")
check("All done." in "\n".join(ops.edits) + body and not list(held_dir.glob("wr_*.json")),
      "a clean collaborator reply is posted without review")
ops, body = run_turn(owner, secret_reply)
check(SAMPLES["GitHub token"] in "\n".join(ops.edits) + body, "the owner's reply is never held")
# --- notes, notices, zero-width, stale releases --------------------------------


class NoteAdapter:
    async def turn(self, c):
        yield MessageChunk(text="All done.")
        yield Done(note="agent-connect: " + SAMPLES["GitHub token"])


async def note_turn():
    ops = Ops()
    body = await TurnReporter(ops).run(NoteAdapter(), ctx())
    return ops, body

ops, body = asyncio.run(note_turn())
check(SAMPLES["GitHub token"] not in "\n".join(ops.edits) + body, "a secret in the note is held too")


async def notice_turn(c):
    ops = Ops()
    reporter = TurnReporter(ops)
    reporter._ctx = c
    reporter._room = c.room
    await reporter.notice("could not resume; " + SAMPLES["GitHub token"])
    return ops

check(SAMPLES["GitHub token"] not in "\n".join(asyncio.run(notice_turn(ctx())).messages),
      "a collaborator's notice carrying a secret is withheld")
check(SAMPLES["GitHub token"] in "\n".join(asyncio.run(notice_turn(owner)).messages),
      "an owner's notice is posted as is")
check("GitHub token" in result_guard.secret_types("ghp_" + "a" * 10 + "\u200b" + "a" * 30),
      "a zero-width space inside a token does not hide it")


for value in ("0", "false", "2", "y", "enable", "@a:b", "\uff11", "1\u200b"):
    check(result_guard.verdict(ctx(), SAMPLES["GitHub token"],
                               env={result_guard.UNCHECKED_ENV: value}).hold,
          f"an opt-out value of {value!r} keeps the check on")
check(not result_guard.verdict(ctx(), SAMPLES["GitHub token"],
                               env={result_guard.UNCHECKED_ENV: "yes"}).hold, "'yes' turns it off")
check("a'b" in result_guard.asked_by(ctx(sender_name='a"b')), "a quote in a display name is replaced")
card = hitl_seam.event_for(NS_REQ, "s", "x\u202ey")
check("\u202e" not in card["prompt"], "the card strips a bidi override itself")
check(result_guard.notify_verdict("[channel: !x:s.space]\nhi", "!r:s.space", {"!r:s.space"},
                                  env={result_guard.UNCHECKED_ENV: "1"}).hold,
      "a redirected notification is held even with scanning off")


async def no_placeholder():
    return await TurnReporter(None).run(Adapter(secret_reply), ctx())

body = asyncio.run(no_placeholder())
check(SAMPLES["GitHub token"] not in body and "release" in body,
      "with no Room Ops the reply is still withheld, and the body says how to release it")

if fails:
    raise SystemExit(f"FAIL — {fails} check(s)")
print("PASS — collaborator result guard")
