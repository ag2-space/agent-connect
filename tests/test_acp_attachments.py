"""Tests for an attachment reaching the Local Agent, at the Worker's seam.

Nothing here is mocked. A Task carrying the attachments the Relay Client hands
over — `ag2_relay_client.media.Attachment`, built here exactly as the fetcher
builds it — goes through the Worker, a real fake ACP Agent child process
receives real JSON-RPC over stdio, and the assertions are on what the agent's
own report says it was sent: the content blocks, in order, with their bytes.

There is no header and no marker anywhere in this file, because there is none
on the wire: the library resolved both before the Task was delivered.

The load-bearing assertion is byte equality: what arrives base64'd in the
`image` block, decoded, is `read_bytes()` of the file on disk. That is the whole
of "never converted, resized or transcoded" — anything that resized a screenshot
on the way would fail it.

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: .venv/bin/python tests/test_acp_attachments.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import base64
import json
import os
import sys
import tempfile
from pathlib import Path

try:
    from ag2_relay_client.media import UNREACHABLE, Attachment
    from agent_connect.adapters.acp import AcpAdapter
    from agent_connect.attachments import MAX_BYTES_ENV
    from agent_connect.sessions import SessionStore
    from _taskqueue import task as queued_task
    from agent_connect.worker import handle_one
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_attachments.py: {exc}\n"
        "This test has a dependency (see docs/adr/0001). Run it from an\n"
        "environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python tests/test_acp_attachments.py"
    )

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")

#: A real PNG header followed by every byte value, several times: content that
#: is unmistakably binary, so a base64 round trip that lost or "fixed" anything
#: shows up as a length or a byte mismatch rather than as nothing.
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Bench:
    """One Worker workspace, one working directory, one scripted fake Agent."""

    def __init__(self, script: dict):
        self._dir = tempfile.TemporaryDirectory()
        base = Path(self._dir.name)
        self.base = base
        self.results = base / "results"
        self.repo = base / "repo"
        # Where the relay client already downloaded what someone attached. It
        # is deliberately *not* inside the working directory: an attachment is
        # something the Worker was handed, not something the agent may go and
        # find for itself.
        self.media = base / "relay-media"
        for d in (self.results, self.repo, self.media):
            d.mkdir()
        self.script_path = base / "script.json"
        self.script_path.write_text(json.dumps(script))
        self.report_path = base / "report.json"
        self.adapter = AcpAdapter(
            command=[sys.executable, FAKE, str(self.script_path)],
            store=SessionStore(base / "sessions.json"),
        )

    def file(self, name: str, data: bytes, mime: str = "image/png") -> Attachment:
        """A file the fetcher saved, and the attachment it hands over for it."""
        path = self.media / name
        path.write_bytes(data)
        return Attachment(path=str(path), mime=mime, name=name,
                          size=len(data), ok=True)

    def absent(self, name: str, mime: str = "image/png",
               where: str = "", reason: str = "") -> Attachment:
        """An attachment the fetcher could not produce bytes for.

        Two shapes, and the difference matters: a fetch that never happened has
        no path at all and carries the library's own `reason`, while a path that
        stopped being readable between the save and the Turn is `ok` and fails
        in `agent_connect.attachments` instead.
        """
        if reason:
            return Attachment(mime=mime, name=name, ok=False, reason=reason)
        return Attachment(path=where or str(self.media / name), mime=mime,
                          name=name, ok=True)

    def handle(self, task_id: str, body: str, attachments=(), **fields) -> str:
        """Run one Task — carrying what the library delivers — through the Worker."""
        fields.setdefault("tier", fields.pop("access_tier", "owner"))
        task = queued_task(task_id, body, attachments=tuple(attachments), **fields)
        previous = os.environ.get("FAKE_ACP_REPORT")
        os.environ["FAKE_ACP_REPORT"] = str(self.report_path)
        try:
            return asyncio.run(asyncio.wait_for(
                handle_one(task, self.adapter, str(self.repo)),
                timeout=30,
            ))
        finally:
            if previous is None:
                os.environ.pop("FAKE_ACP_REPORT", None)
            else:
                os.environ["FAKE_ACP_REPORT"] = previous

    def report(self):
        if not self.report_path.exists():
            return None
        return json.loads(self.report_path.read_text())

    def blocks(self):
        """The content blocks of the one prompt the agent received."""
        report = self.report()
        return report["prompts"][0]["prompt"] if report and report["prompts"] else []


ANSWERS = {"turns": [{"actions": [{"type": "message",
                                   "text": "the button is cut off on the right"}],
                      "stopReason": "end_turn"}]}

# --- an image reaches the Local Agent as prompt content ---------------------

bench = Bench(ANSWERS)
shot = bench.file("shot.png", PNG)
out = bench.handle(
    "a1", "what is wrong with this?", attachments=[shot],
    channel_id="!room:ag2.space", sender_name="Ada",
)
blocks = bench.blocks()

check("the button is cut off on the right" in out,
      "the Turn is answered, so the attachment did not cost the question")
check(len(blocks) == 2, "the prompt carried two content blocks: the text and the image")
check(blocks[0]["type"] == "text" and blocks[1]["type"] == "image",
      "the text comes first and the attachment follows it as content")
check(blocks[1].get("mimeType") == "image/png",
      "the image is declared with the media type the Relay Client recorded")
check(base64.b64decode(blocks[1]["data"]) == Path(shot.path).read_bytes(),
      "and the bytes the Local Agent received are the bytes on disk, exactly — "
      "nothing was converted, resized or transcoded")
check(shot.path not in blocks[0]["text"],
      "the local path is not pasted into the prompt as a filename to go and read")
check("shot.png" in blocks[0]["text"],
      "the text names what follows it, so the agent can tell two images apart")
check(blocks[0]["text"].endswith("what is wrong with this?"),
      "and what the person typed is the last thing in it, unchanged")

# --- several attachments on one message are all passed ----------------------

bench = Bench(ANSWERS)
one = bench.file("one.png", PNG)
two = bench.file("two.png", PNG[:64] + b"second")
notes = bench.file("notes.pdf", b"%PDF-1.4\nnot really a pdf\n",
                   mime="application/pdf")
out = bench.handle("a2", "compare these", attachments=[one, two, notes])
blocks = bench.blocks()
check(len(blocks) == 4, "three attachments on one message all reach the agent")
check([b["type"] for b in blocks] == ["text", "image", "image", "resource"],
      "each as the kind of block its media type calls for, in the order sent")
check(base64.b64decode(blocks[1]["data"]) == Path(one.path).read_bytes()
      and base64.b64decode(blocks[2]["data"]) == Path(two.path).read_bytes(),
      "the two images are not confused with each other")
resource = blocks[3]["resource"]
check(base64.b64decode(resource["blob"]) == Path(notes.path).read_bytes()
      and resource["mimeType"] == "application/pdf",
      "a file that is neither image nor audio rides as an embedded resource, "
      "byte for byte")
check(resource["uri"] == Path(notes.path).resolve().as_uri(),
      "which names where it came from, rather than replacing it with the name")
check("one.png" in blocks[0]["text"] and "two.png" in blocks[0]["text"]
      and "notes.pdf" in blocks[0]["text"],
      "and all three are named in the text, so the agent can be asked about one")
check(blocks[0]["text"].endswith("compare these"),
      "the person's own text is still the last thing, and still untouched")

# --- a kind the Local Agent does not advertise is said out loud -------------

blind = {"agentCapabilities": {"promptCapabilities": {"image": False,
                                                      "embeddedContext": False}},
         "turns": [{"actions": [{"type": "message", "text": "I see no image."}],
                    "stopReason": "end_turn"}]}
bench = Bench(blind)
out = bench.handle("b1", "what is wrong with this?",
                   attachments=[bench.file("screenshot.png", PNG)])
blocks = bench.blocks()
check(len(blocks) == 1 and blocks[0]["type"] == "text",
      "an agent that did not advertise images is sent none")
check("I can't read that kind of attachment" in out,
      "and the room is told so, in as many words, rather than left in silence")
check("screenshot.png" in out, "the message names the file that did not arrive")
check("Paste the content" in out,
      "and says what to do instead, which is the point of saying anything")
check("no attachments at all" in out,
      "an agent that takes nothing says that, rather than listing an empty set")
check("I see no image." in out,
      "the Turn still runs and its answer still comes back")
check(blocks[0]["text"].endswith("what is wrong with this?"),
      "the person's own text is unchanged by an attachment that failed")
check("not readable" not in blocks[0]["text"],
      "and the agent is not told the file is unreadable when the truth is that "
      "it arrived and the agent said it could not take it")

# Advertised for images, not for anything else: the same message, but now it
# can say what *would* work.
partly = {"agentCapabilities": {"promptCapabilities": {"image": True}},
          "turns": [{"actions": [{"type": "message", "text": "ok"}],
                     "stopReason": "end_turn"}]}
bench = Bench(partly)
out = bench.handle(
    "b2", "look and listen",
    attachments=[bench.file("shot.png", PNG),
                 bench.file("voice.ogg", b"OggS\x00\x02voice", mime="audio/ogg")])
blocks = bench.blocks()
check([b["type"] for b in blocks] == ["text", "image"],
      "the kind it advertised goes; the kind it did not stays behind")
check("voice.ogg" in out and "shot.png" not in out,
      "and only the one that stayed behind is reported")
check("audio attachments" in out,
      "the reason is what the agent did not advertise, not a guess about the file")
check("This agent accepts: images." in out,
      "and the person is told what would have worked")

# --- an attachment that cannot be read is reported the same way -------------

bench = Bench(ANSWERS)
out = bench.handle("c1", "have a look", attachments=[bench.absent("gone.png")])
check(len(bench.blocks()) == 1, "a file that is not there is not sent")
check("I can't read that kind of attachment" in out and "gone.png" in out,
      "and its absence is stated in the room rather than swallowed")
check("not on this machine" in out, "with the reason it could not be read")

bench = Bench(ANSWERS)
out = bench.handle(
    "c2", "have a look",
    attachments=[bench.absent("a-directory", where=str(bench.media))])
check(len(bench.blocks()) == 1 and "regular file" in out,
      "a path pointing at something that is not a file is refused and said")

# Two attachments, two different failures, one message about them.
bench = Bench(ANSWERS)
out = bench.handle(
    "c3", "have a look",
    attachments=[bench.absent("nope.png"),
                 bench.absent("relative.png", where="relative/path.png")])
check(out.count("I can't read that kind of attachment") == 1,
      "several failures are one fact about the run, said once")
check("nope.png" in out and "relative.png" in out,
      "and every file that failed is named in it")
check("2 of its attachments" in out, "counted, so nothing looks like it got through")

# --- a fetch that never happened degrades to words, and the Turn goes on ----
# The Relay Client delivers a Task whose media it could not download rather than
# holding or rejecting it: the gateway answers 502 for every cause, so waiting
# for a good answer is waiting for ever, and dead-lettering steals the person's
# chance of being answered in words. What is left is a name and a reason, and
# both ends hear it — the agent in its prompt, the room in a notice.

seen = {"turns": [{"actions": [{"type": "message",
                                "text": "I can see you attached something, but "
                                        "it did not reach me."}],
                   "stopReason": "end_turn"}]}
bench = Bench(seen)
out = bench.handle("f1", "what is wrong with this?",
                   attachments=[bench.absent("photo.jpg", mime="image/jpeg",
                                             reason=UNREACHABLE)])
blocks = bench.blocks()
check(len(blocks) == 1 and blocks[0]["type"] == "text",
      "an attachment with no bytes behind it is sent as no content block")
check("it did not reach me" in out,
      "the Turn runs and is answered — a failed fetch is not a failed Turn")
check("photo.jpg" in blocks[0]["text"] and "not readable" in blocks[0]["text"],
      "the agent is told in band that a file was meant to be here and is not")
check(blocks[0]["text"].endswith("what is wrong with this?"),
      "in the framing, never inside what the person typed")
check(UNREACHABLE in out and "photo.jpg" in out,
      "and the room is told, in the Relay Client's own words for why")
check("not absolute" not in out and "not on this machine" not in out,
      "not in this module's words for a path, which is a different failure")
check("://" not in out and "://" not in blocks[0]["text"],
      "neither end is shown the address that could not be fetched")

# An agent that would not have taken the file anyway is not the reason it is
# missing. Asked in that order the room hears "this agent did not say it can
# take image attachments" about a file that never reached the machine — a true
# sentence about the wrong failure, under a media type read off a marker hint.
bench = Bench({"agentCapabilities": {"promptCapabilities": {"image": False,
                                                            "embeddedContext": False}},
               "turns": [{"actions": [{"type": "message", "text": "ok"}],
                          "stopReason": "end_turn"}]})
out = bench.handle("f3", "have a look",
                   attachments=[bench.absent("photo.jpg", mime="image/jpeg",
                                             reason=UNREACHABLE)])
check(UNREACHABLE in out,
      "a fetch that never happened is answered before the agent's capabilities "
      "are asked about it")
check("did not say it can take" not in out,
      "so what the agent advertises is never reported as why a file is absent")

# The same, with nothing typed alongside it: an upload with no caption whose
# fetch also failed is the thinnest Task the wire can produce, and it still has
# to reach the agent as a question rather than be dead-lettered as empty.
bench = Bench(seen)
out = bench.handle("f2", "",
                   attachments=[bench.absent("photo.jpg", mime="image/jpeg",
                                             reason=UNREACHABLE)])
blocks = bench.blocks()
check("it did not reach me" in out, "a captionless upload that failed is still asked")
check("photo.jpg" in blocks[0]["text"],
      "and the one thing known about it — its name — is what it is asked about")

# --- the size limit refuses; it never resizes -------------------------------

bench = Bench(ANSWERS)
big = bench.file("big.png", PNG * 40)
previous = os.environ.get(MAX_BYTES_ENV)
os.environ[MAX_BYTES_ENV] = "1024"
try:
    out = bench.handle("d1", "what is wrong with this?", attachments=[big])
finally:
    if previous is None:
        os.environ.pop(MAX_BYTES_ENV, None)
    else:
        os.environ[MAX_BYTES_ENV] = previous
check(len(bench.blocks()) == 1,
      "an attachment over the limit is not sent")
check(MAX_BYTES_ENV in out and "big.png" in out,
      "the room is told which file, and which setting decided it")
check("resiz" not in out.lower() and "shrunk" not in out.lower(),
      "nothing offers to shrink it: a resized screenshot is a different screenshot")

# --- a non-owner Task never gets as far as opening anything -----------------

bench = Bench(ANSWERS)
out = bench.handle("e1", "what is in this file?",
                   attachments=[bench.file("shot.png", PNG)],
                   access_tier="other")
check("only answer my owner" in out and bench.report() is None,
      "a Task at another Tier is refused before an attachment is opened at all")

print("\n" + ("PASS — acp attachments green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
