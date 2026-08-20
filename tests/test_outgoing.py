"""Tests for a file the agent produced arriving in the room.

**What this file used to assert, and why none of it was true.** The Worker
*staged* a file into an outgoing result directory and named it with a
`[file: <path>]` marker, and a second process — `ag2-sparrow` — was supposed to
resolve the marker, check the path against its own allowlist and upload. The
assertions here were written against a transcription of that process's rules,
and they passed. What nobody had checked was the wire in between: the broker's
`parse_result` knows `[no-send]`, `[REPLIED]`, `[deduped:]` and `[channel:]`,
and **not** `[file:]`. So the file was never uploaded by anyone, and the marker
reached the room as literal text naming an absolute local path — *after* the
Ladder had edited the placeholder to promise the person a file.

A test that transcribes what the other side is believed to do proves the
transcription. So this one no longer has a stand-in in it. It drives the real
`ag2_relay_client.RelayClient` with only its HTTP replaced, which means the real
`markers` parser, the real `Outbound`, the real `EgressAllowlist` and the real
upload route — and the two things the ticket promises are asserted on what the
broker would actually have received:

* an allowed file reaches `POST /v1/rooms/<room>/media`, and the body that
  follows it has no marker left in it;
* a path outside the allowlist is refused **in this process**, nothing is
  uploaded, and the refusal is in the body the room reads.

The Adapters are stubs emitting the event vocabulary, so no ACP Agent and no
network are involved.

Run: python3 tests/test_outgoing.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import os
import tempfile
import urllib.parse
from pathlib import Path

from ag2_relay_client import RelayClient, TokenSource
from ag2_relay_client.outbound import MAX_FILES
from agent_connect import outgoing
from agent_connect.events import COMPLETED, Done, MessageChunk
from agent_connect.outgoing import named_files
from agent_connect.reporter import (
    FILE_POINTER,
    FILE_POINTER_MANY,
    NO_SEND,
    PLACEHOLDER,
    REPLIED,
    LadderSettings,
)
from agent_connect.roomops import room_ops_for
from agent_connect.sandbox import sandbox_preamble
from agent_connect.worker import handle_one

ROOM = "!room:ag2.space"
MEDIA = "/v1/rooms/" + urllib.parse.quote(ROOM, safe="") + "/media"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# ---------------------------------------------------------------------------
# The broker, as far as the library can tell.
# ---------------------------------------------------------------------------

class Wire:
    """Every request the library made, in order — and plausible answers.

    Not a fake of the Worker's side of anything: the client under it is a real
    `RelayClient`, and this is the socket. So `room_ops` and `media` below are
    literally what the broker would have been sent.
    """

    base_url = "https://broker.example/relay"

    def __init__(self):
        self.serving = []
        self.posted = []
        self._events = 0

    # -- what a test does to it ---------------------------------------------

    def serve(self, *raw):
        self.serving.extend(raw)

    def of(self, path):
        return [payload for sent, payload in self.posted if sent == path]

    def last(self, path):
        """The most recent payload on `path`, or an empty one.

        Empty rather than an `IndexError`, so a *missing* request fails the
        check that names it instead of aborting the file at the first one. This
        suite's whole subject is a request that was not being made.
        """
        sent = self.of(path)
        return sent[-1] if sent else {}

    def room_ops(self, op=""):
        return [p for p in self.of("/v1/room") if not op or p.get("op") == op]

    @property
    def media(self):
        return self.of(MEDIA)

    @property
    def bodies(self):
        return [p.get("body", "") for p in self.of("/v1/results")]

    @property
    def posted_body(self):
        return self.last("/v1/results").get("body", "")

    def edited(self):
        edits = self.room_ops("edit")
        return edits[-1].get("body", "") if edits else ""

    # -- what the library does to it ----------------------------------------

    def get(self, path, params=None, timeout=None):
        served, self.serving = self.serving, []
        return {"tasks": served}

    def post(self, path, payload=None, timeout=None):
        self.posted.append((path, dict(payload or {})))
        if path == "/v1/room":
            self._events += 1
            return {"event_id": f"$ev{self._events}"}
        if path.endswith("/media"):
            return {"mxc": "mxc://ag2.space/upload"}
        return {"ok": True}


_runs = [0]
tmp = Path(tempfile.mkdtemp())


def bench(roots=None):
    """A repo the agent works in, and a real client allowed to send from it.

    `egress_roots` is the whole of the egress policy and it is fixed here, at
    construction — which is the ticket's point, and the reason a test can name
    the permitted area in one place and then attack it from six directions.
    """
    _runs[0] += 1
    repo = tmp / f"repo-{_runs[0]}"
    repo.mkdir()
    wire = Wire()
    client = RelayClient(
        TokenSource(token="https://broker.example/relay|s3cret"),
        state_dir=tmp / f"state-{_runs[0]}",
        instance="test",
        http=wire,
        egress_roots=(outgoing.egress_roots(repo, {}) if roots is None else roots),
    )
    client.prepare()
    return wire, client, repo


def deliver(wire, client, task_id, room=ROOM):
    """One task, through the library's own poll — journal, sidecar and all.

    The room is captured at accept (F7) and read back out of the journal by
    `complete`, so a test that seeded the queue by hand would be asserting
    against its own seeding of the one field an upload needs.
    """
    raw = {"id": task_id, "task": "make me a report", "access_tier": "owner"}
    if room:
        raw["channel_id"] = room
    wire.serve(raw)
    client.poll_once()
    return client.tasks.get_nowait()


class Scripted:
    def __init__(self, *events):
        self.events = events

    async def turn(self, ctx):
        for event in self.events:
            await asyncio.sleep(0)
            yield event


def answers(text):
    return Scripted(MessageChunk(text=text), Done(reason=COMPLETED, text=text))


def turn(wire, client, repo, said, task_id="task-1", room=ROOM, ladder=True):
    """One whole Task: delivered, run, answered — the Worker's own path."""
    task = deliver(wire, client, task_id, room)
    ops = room_ops_for(client) if ladder else None
    return asyncio.run(handle_one(
        task, answers(said), str(repo), None, ops,
        LadderSettings(throttle=0.0), client=client))


print("\n-- a file the agent produced arrives in the room --")

wire, client, repo = bench()
(repo / "report.md").write_text("# what I found\n")
body = turn(wire, client, repo, "Here is the report.\n\n[file: report.md]")

check(len(wire.media) == 1 and wire.media[0].get("filename") == "report.md",
      "the file is uploaded to the room's media route, by name")
check(wire.media and wire.media[0].get("content_b64"),
      "with its bytes — read off the descriptor the allowlist judged")
check(wire.bodies == ["Here is the report."],
      "and the body that follows is the answer, with no marker left in it: "
      "the room reads prose and receives a file")
check("[file:" not in "".join(wire.bodies),
      "no `[file:]` reaches the room as literal text — the regression this "
      "ticket closes")
check((repo / "report.md").exists(),
      "the agent's own copy is left where it wrote it, uncopied and unmoved")

check(len(wire.room_ops("message")) == 1,
      "one message in the room: the placeholder")
check(wire.edited() == FILE_POINTER,
      "edited into a pointer, because the reply itself travels the result path")
check(not body.startswith(REPLIED),
      "the result is NOT marked replied — a skip marker is terminal in the "
      "grammar, and nothing would ever read past it to find the file")
check(not wire.room_ops("react"),
      "and the Worker still places no reaction: that one is the broker's")


print("\n-- the ladder, through the library: [REPLIED] completes the lease --")

# The other half of the seam, and the half that has to keep working: an answer
# short enough to edit in goes *into the placeholder*, and the result body is
# the terminal marker. A skip is terminal in the grammar, so the marker body
# travels to `/v1/results` **verbatim** — H1's rule, and the one that stops a
# lease expiring and the task being re-served for ever.
wire, client, repo = bench()
body = turn(wire, client, repo, "It is fine.")
check(wire.room_ops("message")[:1] == [{"op": "message", "room_id": ROOM,
                                        "body": PLACEHOLDER}],
      "the placeholder is posted when the work starts")
check(wire.edited().startswith("It is fine."),
      "and edited into the answer when it ends — one message, not two")
check(body.startswith(REPLIED), "the result body carries the terminal marker")
check(wire.posted_body == body,
      "which reaches /v1/results verbatim: a skip is still a POST, because a "
      "skipped POST leaves the lease to expire and the task to come back (H1)")
check(client.journal.inflight() == 0 and not client.journal.pending_results(),
      "and the lease is completed — nothing owed, nothing left to re-serve")
check(not wire.media,
      "with no upload attempted: a body nobody parses past names no file, and "
      "this one genuinely named none")


print("\n-- an out-of-allowlist path is refused in-process --")

wire, client, repo = bench()
secret = tmp / "private.key"
secret.write_text("-----BEGIN PRIVATE KEY-----\n")
body = turn(wire, client, repo, f"Done.\n\n[file: {secret}]")

check(not wire.media, "nothing is uploaded")
posted = wire.posted_body
check("private.key" in posted and "attachment not sent" in posted,
      "the room is told, by name, that the file did not go — a file that "
      "silently fails to arrive is indistinguishable from an agent that "
      "ignored the request")
check("outside the directories this client may send from" in posted,
      "and why: it is not inside a root this client was built with")
check("Done." in posted, "in the same reply as the answer it came with")
check("[file:" not in posted, "with the marker itself stripped, not delivered")
check("BEGIN PRIVATE KEY" not in str(wire.posted),
      "and not one byte of it left this process")

# The refusal is the *library's*, decided on the descriptor. These are the
# escapes `egress.py`'s adversarial suite is built around, asserted here again
# because this is the path a real Turn takes to reach it.
(repo / "link.key").symlink_to(secret)
body = turn(wire, client, repo, "Done.\n\n[file: link.key]", task_id="task-2")
check(not wire.media and "attachment not sent" in wire.posted_body,
      "a symlink out of the permitted area is resolved before it is judged")

body = turn(wire, client, repo, "[file: ../private.key]", task_id="task-3")
check(not wire.media and "attachment not sent" in wire.posted_body,
      "a relative path climbing out of it is refused too")

sibling = Path(str(repo) + "-old")
sibling.mkdir()
(sibling / "notes.md").write_text("old")
body = turn(wire, client, repo, f"[file: {sibling / 'notes.md'}]", task_id="task-4")
check(not wire.media and "attachment not sent" in wire.posted_body,
      "and `<repo>-old` is not inside `<repo>` — the separator is part of the "
      "test, because a look-alike directory name is the cheapest attack there is")

body = turn(wire, client, repo, "[file: /etc/hosts]", task_id="task-5")
check(not body.startswith(NO_SEND),
      "a reply that is only a refused marker is not archived in silence")
check("hosts" in wire.posted_body,
      "the room hears about it rather than a log doing")


print("\n-- a relative path is read against the directory the Turn ran in --")

wire, client, repo = bench()
(repo / "sub").mkdir()
(repo / "sub" / "chart.png").write_bytes(b"\x89PNG")
turn(wire, client, repo, "Look.\n[file: sub/chart.png]")
check(len(wire.media) == 1 and wire.media[0].get("filename") == "chart.png",
      "the Worker hands `complete` the Turn's working directory as the base, "
      "so a relatively-named file is found rather than quietly refused")


print("\n-- several files, and the point past which a reply is a file dump --")

wire, client, repo = bench()
for name, data in (("report.md", b"one"), ("chart.png", b"\x89PNG two"),
                   ("diff.patch", b"three")):
    (repo / name).write_bytes(data)
body = turn(wire, client, repo,
            "Three things.\n[file: report.md]\n[file: chart.png]\n[file: diff.patch]")
check([m.get("filename") for m in wire.media] == ["report.md", "chart.png", "diff.patch"],
      "all three reach the room, in the order the agent named them")
check(wire.posted_body.strip() == "Three things.", "under one reply, once")
check(wire.edited() == FILE_POINTER_MANY.format(count=3),
      "and the pointer says how many files follow")

wire, client, repo = bench()
for i in range(MAX_FILES + 3):
    (repo / f"f{i}.txt").write_text(str(i))
turn(wire, client, repo,
     "\n".join(f"[file: f{i}.txt]" for i in range(MAX_FILES + 3)))
check(len(wire.media) == MAX_FILES, f"at most {MAX_FILES} files go with one reply")
check(wire.posted_body.count("no more than") == 3,
      "and each one over the count is refused out loud, by name")


print("\n-- a reply whose whole content is a file --")

wire, client, repo = bench()
(repo / "chart.png").write_bytes(b"chart")
body = turn(wire, client, repo, "[file: chart.png]")
check(not body.startswith(NO_SEND),
      "a body that is only a marker is NOT the rejection an empty answer is")
check(len(wire.media) == 1, "the file goes")
check("chart.png" in wire.posted_body,
      "and the reply says what is attached, rather than being an empty message "
      "with something hanging off it")

# The genuinely empty Turn is untouched by any of this.
wire, client, repo = bench()
task = deliver(wire, client, "task-empty")
body = asyncio.run(handle_one(task, Scripted(Done(reason=COMPLETED, text="")),
                              str(repo), None, None, None, client=client))
check(body.startswith(NO_SEND),
      "a Turn that produced nothing at all is still a structured rejection")


print("\n-- a Worker with no room ops still delivers the file --")

wire, client, repo = bench()
(repo / "report.md").write_text("no ladder here")
body = turn(wire, client, repo, "Here it is.\n\n[file: report.md]", ladder=False)
check(not wire.room_ops(), "nothing is posted or edited: there is no Ladder")
check(len(wire.media) == 1 and wire.posted_body == "Here it is.",
      "and the answer and its file still travel — egress is the result path's, "
      "not the Ladder's")


print("\n-- the roots, and where they come from --")

check(outgoing.egress_roots("/x/repo", {}) == ("/x/repo",),
      "the working directory this Worker's Turns run in is the permitted area")
check(outgoing.egress_roots("/x/repo", {outgoing.EGRESS_ROOTS_ENV: "/x/out"})
      == ("/x/repo", "/x/out"),
      "an operator with a second one says so, in one setting")
check(outgoing.egress_roots("/x/repo", {outgoing.EGRESS_ROOTS_ENV:
                                        f"/a{os.pathsep}{os.pathsep}/b"})
      == ("/x/repo", "/a", "/b"),
      "several, separated the way this platform separates paths, blanks dropped")
check(outgoing.egress_roots(None, {}) == (),
      "and a Worker that cannot say where its agent works vouches for nothing")

_, closed, _ = bench(())
check(closed.room_ops.allowlist.roots == (),
      "which the client honours fail-closed: no roots, no allowlist, no files")

check(outgoing.egress_roots("~/agents", {})[0].startswith(str(Path.home())),
      "a root written with a `~` is the operator's home, not a directory "
      "literally called that")
check(outgoing.egress_roots("/x/repo", {outgoing.EGRESS_ROOTS_ENV: "/x/repo"})
      == ("/x/repo",),
      "and a root named twice is one root")

# Box 4: nothing anywhere in the package reads the retired airlock's setting.
PACKAGE = "\n".join(p.read_text()
                    for p in sorted((_bootstrap.ROOT / "agent_connect").rglob("*.py")))
check("AGENT_CONNECT_RESULT_DIR" not in PACKAGE.replace(
          "`AGENT_CONNECT_RESULT_DIR`", ""),
      "no code path reads AGENT_CONNECT_RESULT_DIR — the airlock is retired, "
      "and only the prose recording that it was still names it")


print("\n-- the markers are read by the library, and only there --")

check(named_files("see [send: /x] here") == ("/x",)
      and named_files("[attach: /x]") == ("/x",)
      and named_files("[file: /x]") == ("/x",),
      "all three spellings, through the one parser that acts on them")
check(named_files("no files here") == () and named_files("") == (),
      "and an ordinary answer names nothing")
check(named_files("```\n[file: /etc/passwd]\n```") == (),
      "a marker inside a code fence is being shown, not issued — which on this "
      "transport is the difference between an example and an upload")

# The expression itself lives in exactly one place, and it is not this one.
OUTGOING_SOURCE = (_bootstrap.ROOT / "agent_connect" / "outgoing.py").read_text()
check("re.compile" not in OUTGOING_SOURCE,
      "there is no second copy of the marker grammar in this package: the old "
      "one promised it 'cannot drift apart' from the delivery path's and had "
      "already drifted from a delivery path that read no `[file:]` at all")


print("\n-- the agent is told the convention, where it can act on it --")

owner = sandbox_preamble("workspace-write", "owner")
reader = sandbox_preamble("read-only", "other")
check("[file:" in owner, "a run that may write files is told how to send one")
check("working directory" in owner, "and where they have to be")
check("[file:" not in reader,
      "a read-only run is not told how to produce something it cannot produce")


print("\n-- the placeholder is not left promising a file that never came --")

# The live regression, from the room's point of view. The Ladder edits the
# placeholder into "the reply and its file follow below" *before* the result is
# posted; if the file then does not go, the person is looking at a promise. It
# is kept by the refusal arriving in the same reply, which is why the refusal
# is in the body rather than in a log.
wire, client, repo = bench()
body = turn(wire, client, repo, f"Here.\n\n[file: {tmp / 'private.key'}]")
check(wire.edited() == FILE_POINTER,
      "the placeholder still points below — the Ladder cannot know yet")
check("attachment not sent" in wire.posted_body and "Here." in wire.posted_body,
      "and what follows says so, in the reply the pointer pointed at")


print("\n" + ("PASS — outgoing files green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
