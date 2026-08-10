"""Tests for a file the agent produced arriving in the room.

Two outside-world surfaces, and nothing else is asserted on:

* the **Room Ops** a real local HTTP relay recorded — what the Worker asked the
  room to show, and, just as importantly, what it never asked for: there is no
  upload among them, because the Worker must not post media itself;
* the **result body** the Worker wrote, put through `DeliveryPath` below — a
  transcription of what `ag2_sparrow` does with a result: `result_markers`'
  marker parsing, `send_allowlist.is_path_sendable`'s allowlist, and
  `remote_gateway_bridge._post_ready_results`' upload-then-post order, including
  the `[attachment not sent: …]` line it annotates a refusal with. It is a
  stand-in, not the real client — but it is the real client's *rule*, so a file
  that this refuses is a file that would not leave the machine.

The Adapters are stubs emitting the event vocabulary, so no ACP Agent, no
network and no credentials are involved, and this runs under bare `python3`.

Run: python3 tests/test_outgoing.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from agent_connect import outgoing
from agent_connect.events import COMPLETED, Done, MessageChunk, TurnContext
from agent_connect.outgoing import MAX_FILES, Delivery, Outbox, carries_files
from agent_connect.reporter import (
    FILE_POINTER,
    FILE_POINTER_MANY,
    NO_SEND,
    PLACEHOLDER,
    REPLIED,
    LadderSettings,
    TurnReporter,
)
from agent_connect.roomops import RoomOps
from agent_connect.sandbox import sandbox_preamble
from agent_connect.worker import handle_one

ROOM = "!room:ag2.space"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


# ---------------------------------------------------------------------------
# The fake relay: a real HTTP server, recording the Room Ops it receives.
# ---------------------------------------------------------------------------

class FakeRelay:
    def __init__(self):
        self.ops = []
        self._n = 0
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                relay._n += 1
                relay.ops.append(dict(payload, path=self.path))
                raw = json.dumps({"event_id": f"$ev{relay._n}"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def ops_of(self, kind):
        return [o for o in self.ops if o.get("op") == kind]

    def bodies(self):
        return [o.get("body", "") for o in self.ops]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def relay_ops():
    relay = FakeRelay()
    return relay, RoomOps(relay.url, "test-token")


# ---------------------------------------------------------------------------
# The delivery path, as ag2_sparrow performs it. Transcribed from
# `result_markers.parse_markers`, `send_allowlist.is_path_sendable` and
# `remote_gateway_bridge._post_ready_results` — see the module docstring.
# ---------------------------------------------------------------------------

_ATTACH_RE = re.compile(r"\[(?:file|send|attach):\s*([^\]]+)\]")
_SKIP_RE = re.compile(r"^\s*(\[no-send\]|\[REPLIED\]|\[deduped:[^\]]*\])")


def is_path_sendable(fpath: str, roots) -> bool:
    if not os.path.isfile(fpath):
        return False
    real = os.path.realpath(fpath)
    for root in roots:
        top = os.path.realpath(root)
        if real == top or real.startswith(top + os.sep):
            return True
    return False


class DeliveryPath:
    """What the relay client would do with one result body.

    `uploads` are the files that reached the room, as `(filename, bytes)`;
    `posted` is the single message body that followed them; `archived` is True
    when a skip marker meant nothing was delivered at all.
    """

    def __init__(self, results_dir):
        self.roots = [str(results_dir)]
        self.uploads = []
        self.posted = None
        self.archived = False
        self.refused = []

    def run(self, body: str) -> "DeliveryPath":
        if _SKIP_RE.match(body or ""):
            self.archived = True
            return self
        out = _ATTACH_RE.sub("", body).strip()
        sent = 0
        for match in _ATTACH_RE.finditer(body):
            named = os.path.realpath(os.path.expanduser(match.group(1).strip()))
            if is_path_sendable(named, self.roots):
                self.uploads.append((os.path.basename(named), Path(named).read_bytes()))
                sent += 1
            else:
                self.refused.append(named)
                out += f"\n[attachment not sent: {named} (path not allowlisted)]"
        if not out.strip() and sent:
            out = "(file attached)"
        self.posted = out
        return self


# ---------------------------------------------------------------------------
# Stubs and scaffolding.
# ---------------------------------------------------------------------------

class Scripted:
    def __init__(self, *events):
        self.events = events

    async def turn(self, ctx):
        for event in self.events:
            await asyncio.sleep(0)
            yield event


def answers(text):
    return Scripted(MessageChunk(text=text), Done(reason=COMPLETED, text=text))


def workspace():
    """A repo the agent works in and a results dir the transport reads."""
    tmp = Path(tempfile.mkdtemp())
    repo, results, tasks = tmp / "repo", tmp / "results", tmp / "tasks"
    for d in (repo, results, tasks):
        d.mkdir()
    return tmp, repo, results, tasks


def ctx_for(repo, room=ROOM, task_id="task-1"):
    return TurnContext(prompt="make me a report", task_id=task_id, room=room,
                       access_tier="owner", cwd=str(repo))


def write_task(tasks, task_id, body, room=ROOM):
    path = tasks / f"task-{task_id}.txt"
    path.write_text(f"id: {task_id}\nchannel_id: {room}\ntask: {body}\n"
                    f"access_tier: owner\n")
    return path


def report(reporter, adapter, ctx):
    return asyncio.run(reporter.run(adapter, ctx))


print("\n-- a file the agent produced arrives in the room --")

tmp, repo, results, tasks = workspace()
(repo / "report.md").write_text("# what I found\n")
relay, ops = relay_ops()
body = report(
    TurnReporter(ops, LadderSettings(throttle=0.0), outbox=Outbox(results)),
    answers("Here is the report.\n\n[file: report.md]"),
    ctx_for(repo),
)

marker = [line for line in body.splitlines() if line.startswith("[file:")]
staged = Path(marker[0][len("[file: "):-1]) if marker else None

check(len(marker) == 1, "the result body carries exactly one send marker")
check(staged is not None and staged.is_file(), "which names a file that exists")
check(str(staged).startswith(str(results) + os.sep),
      "inside the outgoing result directory — the one root the send allowlist trusts")
check(staged.read_bytes() == (repo / "report.md").read_bytes(),
      "byte for byte the file the agent produced")
check((repo / "report.md").exists(),
      "and the agent's own copy is left where it wrote it")
check(not body.startswith(REPLIED),
      "the result is NOT marked replied — a marked body is archived unread and the "
      "file would never leave")
check(not any(o.get("path", "").endswith("/media") for o in relay.ops),
      "the Worker asked the relay for no upload at all: not its job, and not its route")
check(not hasattr(ops, "upload"), "Room Ops offer no upload op to reach for")
check(len(relay.ops_of("message")) == 1, "one message in the room: the placeholder")
check(relay.ops[-1]["op"] == "edit" and relay.ops[-1]["body"] == FILE_POINTER,
      "edited into a pointer, because the reply itself travels the delivery path")
check("Here is the report." not in "".join(relay.bodies()),
      "so the answer is not also edited into the room — one reply, not two")

sent = DeliveryPath(results).run(body)
check(len(sent.uploads) == 1, "the delivery path uploads exactly one file")
check(sent.uploads[0][0] == "report.md", "under the name the agent gave it")
check(sent.uploads[0][1] == b"# what I found\n", "with the bytes it produced")
check(not sent.refused, "the allowlist accepted it — that is what staging is for")
check("Here is the report." in sent.posted, "and the reply text is posted with it")
check("[file:" not in sent.posted, "with the marker stripped: the room reads prose")
relay.stop()


print("\n-- the reply text and the file are one reply, not two messages --")

check(sent.posted.strip() == "Here is the report.",
      "the delivered body is the answer and nothing else")
check(len(relay.ops_of("message")) == 1 and len(sent.uploads) == 1,
      "one placeholder, one upload, one body: the room reads a single reply")
check(FILE_POINTER.startswith("✅"),
      "and the placeholder resolves to a pointer, not to a second answer")


print("\n-- a file outside the permitted area is not sent, and the room is told --")

tmp, repo, results, tasks = workspace()
secret = tmp / "private.key"
secret.write_text("-----BEGIN PRIVATE KEY-----\n")
(repo / "link.key").symlink_to(secret)
relay, ops = relay_ops()
body = report(
    TurnReporter(ops, LadderSettings(throttle=0.0), outbox=Outbox(results)),
    answers(f"Done.\n\n[file: {secret}]"),
    ctx_for(repo),
)
check("[file:" not in body, "no send marker survives for a file outside the area")
check("private.key" in body and "outside the working directory" in body,
      "the body names the file and says why it did not go")
check(body.startswith(REPLIED),
      "and, with nothing to deliver, the reply is an ordinary one: edited in, marked")
answer_edit = relay.ops[-1]["body"]
check("private.key" in answer_edit and "not be sent" in answer_edit,
      "the ROOM is told it was not sent — a file that silently fails to arrive is "
      "indistinguishable from an agent that ignored the request")
check("Done." in answer_edit, "in the same message as the answer it came with")
check(not list((results / "outgoing").rglob("*")) if (results / "outgoing").exists()
      else True, "nothing was staged, so nothing became sendable")
sent = DeliveryPath(results).run(body)
check(sent.archived and not sent.uploads,
      "the delivery path uploads nothing and archives the reply the room already has")
relay.stop()

# The same file, reached through a symlink from inside the working directory.
relay, ops = relay_ops()
body = report(
    TurnReporter(ops, LadderSettings(throttle=0.0), outbox=Outbox(results)),
    answers("Done.\n\n[file: link.key]"),
    ctx_for(repo),
)
check("[file:" not in body,
      "a symlink out of the working directory is resolved before it is judged")
check("outside the working directory" in body, "and refused for what it points at")
check(not any("BEGIN PRIVATE KEY" in p.read_text()
              for p in results.rglob("*") if p.is_file()),
      "the bytes never reach the directory the allowlist trusts")
relay.stop()

# A sibling directory whose name merely starts the same way.
sibling = tmp / "repo-old"
sibling.mkdir()
(sibling / "notes.md").write_text("old")
body = asyncio.run(TurnReporter(None, LadderSettings(), outbox=Outbox(results)).run(
    answers(f"[file: {sibling / 'notes.md'}]"), ctx_for(repo)))
check("[file:" not in body,
      "`/x/repo-old` is not inside `/x/repo` — the separator is part of the test")

# `..` from inside the working directory.
body = asyncio.run(TurnReporter(None, LadderSettings(), outbox=Outbox(results)).run(
    answers("[file: ../private.key]"), ctx_for(repo)))
check("[file:" not in body and "outside" in body,
      "a relative path climbing out of the working directory is refused too")


print("\n-- several files produced by one Turn are all delivered --")

tmp, repo, results, tasks = workspace()
(repo / "report.md").write_text("one")
(repo / "chart.png").write_bytes(b"\x89PNG two")
(repo / "diff.patch").write_text("three")
relay, ops = relay_ops()
body = report(
    TurnReporter(ops, LadderSettings(throttle=0.0), outbox=Outbox(results)),
    answers("Three things.\n[file: report.md]\n[file: chart.png]\n[file: diff.patch]"),
    ctx_for(repo),
)
sent = DeliveryPath(results).run(body)
check(len(sent.uploads) == 3, "all three files reach the room")
check([n for n, _ in sent.uploads] == ["report.md", "chart.png", "diff.patch"],
      "in the order the agent named them")
check(dict(sent.uploads)["chart.png"] == b"\x89PNG two",
      "each with its own bytes, unconverted")
check(sent.posted.strip() == "Three things.", "under one reply, once")
check(len(relay.ops_of("message")) == 1, "and still one message in the room")
check(relay.ops[-1]["body"] == FILE_POINTER_MANY.format(count=3),
      "the pointer says how many files follow")
relay.stop()

# Two files of the same name, from two directories, are two files.
(repo / "sub").mkdir()
(repo / "sub" / "report.md").write_text("the other one")
body = asyncio.run(TurnReporter(None, LadderSettings(), outbox=Outbox(results)).run(
    answers("[file: report.md]\n[file: sub/report.md]"), ctx_for(repo, task_id="task-2")))
sent = DeliveryPath(results).run(body)
check(len(sent.uploads) == 2, "both are delivered")
check(len({n for n, _ in sent.uploads}) == 2,
      "under names the room can tell apart, rather than one overwriting the other")
check({b for _, b in sent.uploads} == {b"one", b"the other one"},
      "and each keeps its own contents")

# A reply cannot be a file dump.
for i in range(MAX_FILES + 3):
    (repo / f"f{i}.txt").write_text(str(i))
body = asyncio.run(TurnReporter(None, LadderSettings(), outbox=Outbox(results)).run(
    answers("\n".join(f"[file: f{i}.txt]" for i in range(MAX_FILES + 3))),
    ctx_for(repo, task_id="task-3")))
sent = DeliveryPath(results).run(body)
check(len(sent.uploads) == MAX_FILES, f"at most {MAX_FILES} files go with one reply")
check(body.count("no more than") == 3 and "3 files" in body,
      "and each one over the count is refused out loud, by name")


print("\n-- a reply whose whole content is a file --")

tmp, repo, results, tasks = workspace()
(repo / "chart.png").write_bytes(b"chart")
body = asyncio.run(TurnReporter(None, LadderSettings(), outbox=Outbox(results)).run(
    answers("[file: chart.png]"), ctx_for(repo)))
check(not body.startswith(NO_SEND),
      "a body that is only a marker is NOT the rejection an empty answer is")
check("[file:" in body, "the file is still on its way")
check("chart.png" in body, "and the reply says what is attached, rather than nothing")
sent = DeliveryPath(results).run(body)
check(len(sent.uploads) == 1 and sent.posted.strip(),
      "so the room gets the file and a sentence with it")

# The genuinely empty Turn is untouched by any of this.
body = asyncio.run(TurnReporter(None, LadderSettings(), outbox=Outbox(results)).run(
    Scripted(Done(reason=COMPLETED, text="")), ctx_for(repo)))
check(body.startswith(NO_SEND),
      "a Turn that produced nothing at all is still a structured rejection")

# A marker for a file that may not go is not nothing either: it is a refusal.
body = asyncio.run(TurnReporter(None, LadderSettings(), outbox=Outbox(results)).run(
    answers("[file: /etc/passwd]"), ctx_for(repo)))
check(not body.startswith(NO_SEND),
      "a reply that is only a refused marker is not archived in silence")
check("passwd" in body and "not be sent" in body,
      "the room is told which file did not go, and that it did not")


print("\n-- what may be staged, and what may not --")

tmp, repo, results, tasks = workspace()
(repo / "fine.txt").write_text("ok")
(repo / "folder").mkdir()
os.mkfifo(str(repo / "pipe"))
(repo / "big.bin").write_bytes(b"x" * 4096)


_boxes = [0]


def refusal(answer, limit=None, ctx=None):
    """One judgement, into an outgoing directory of its own.

    A fresh one per call so that a staged name is the name it was given: two
    stages of `fine.txt` into the same directory produce `fine.txt` and
    `fine-1.txt`, which is the collision rule tested for its own sake below.
    """
    _boxes[0] += 1
    out = tmp / f"out{_boxes[0]}"
    out.mkdir()
    return Outbox(out, limit=limit).stage(answer, ctx or ctx_for(repo))


check(refusal("[file: fine.txt]").sent == ("fine.txt",),
      "a regular file inside the working directory is staged")
check(refusal("[file: ./fine.txt]").sent == ("fine.txt",),
      "named relatively, against the directory the Turn ran in")
check(refusal(f"[file: {repo / 'fine.txt'}]").sent == ("fine.txt",),
      "or absolutely — the same file either way")
check("there is no such file" in "".join(refusal("[file: gone.txt]").refused),
      "a file that is not there is said to be not there")
check("not a regular file" in "".join(refusal("[file: folder]").refused),
      "a directory is not a file to send")
check("not a regular file" in "".join(refusal("[file: pipe]").refused),
      "and neither is a FIFO — judged on the descriptor, so it cannot hang the Turn")
check("its path is not a path" in "".join(refusal("[file: fi\x00ne.txt]").refused),
      "a NUL in the path is refused before any syscall")
check(refusal("[file: big.bin]", limit=4096).sent == ("big.bin",),
      "a file exactly at the limit still goes")
over = refusal("[file: big.bin]", limit=100).refused
check("0.0 MB" in "".join(over) and outgoing.MAX_BYTES_ENV in "".join(over),
      "one over it is refused, in megabytes and by the setting's own name")
check(refusal("[file: big.bin]", limit=0).sent == ("big.bin",),
      "and a limit of 0 is no limit")
check("could not tell which directory" in "".join(
          Outbox(results).stage("[file: fine.txt]", None).refused),
      "with no working directory to judge against, nothing is sent")

check(outgoing.max_bytes({}) == outgoing.DEFAULT_MAX_BYTES,
      "the size limit has a default")
check(outgoing.max_bytes({outgoing.MAX_BYTES_ENV: "1234"}) == 1234, "which is a setting")
check(outgoing.max_bytes({outgoing.MAX_BYTES_ENV: "0"}) == 0, "0 removes it")
check(outgoing.max_bytes({outgoing.MAX_BYTES_ENV: "banana"}) == outgoing.DEFAULT_MAX_BYTES,
      "and a value typed wrong is the default, not a Worker that will not start")
check(outgoing.DEFAULT_MAX_BYTES == 25 * 1024 * 1024,
      "defaulting to the relay's own upload cap, so the refusal arrives with words")

# A file the agent wrote straight into the outgoing directory is already sendable.
# The outgoing directory is the staging subdirectory, not the results directory
# around it — that one holds every other Task's archived reply.
(results / "outgoing").mkdir(exist_ok=True)
(results / "outgoing" / "already.txt").write_text("there")
delivery = Outbox(results).stage(
    f"[file: {results / 'outgoing' / 'already.txt'}]", ctx_for(repo))
named = [Path(m[len("[file: "):-1]) for m in delivery.markers]
check(named == [(results / "outgoing" / "already.txt").resolve()],
      "a file already in the outgoing directory is left where it is, not copied")
check(len(list(results.rglob("already*.txt"))) == 1, "so there is one of it, not two")

# Markers, recognised the way the transport recognises them.
check(carries_files("see [send: /x] here") and carries_files("[attach: /x]")
      and carries_files("[file: /x]"),
      "all three spellings the delivery path reads are read here too")
check(not carries_files("no files here") and not carries_files(""),
      "and an ordinary answer names nothing")
check(Delivery(text="hi").asked is False and Delivery(refused=("x",)).asked is True,
      "a Turn asked to send something even when nothing went")

# Staged copies do not accumulate for ever in a directory the transport trusts.
old = results / outgoing.STAGING / "task-ancient"
old.mkdir(parents=True)
(old / "leftover.txt").write_text("stale")
os.utime(old, (0, time.time() - outgoing.KEEP_SECONDS - 60))
Outbox(results).stage("[file: fine.txt]", ctx_for(repo, task_id="task-9"))
check(not old.exists(), "a staged folder older than a day is swept")
check((results / outgoing.STAGING / "task-9").exists(), "and today's is not")


print("\n-- the agent is told the convention, where it can act on it --")

owner = sandbox_preamble("workspace-write", "owner")
reader = sandbox_preamble("read-only", "other")
check("[file:" in owner, "a run that may write files is told how to send one")
check("working directory" in owner, "and where they have to be")
check("[file:" not in reader,
      "a read-only run is not told how to produce something it cannot produce")


print("\n-- through the Worker's seam, end to end --")

tmp, repo, results, tasks = workspace()
(repo / "report.md").write_text("the whole report")
relay, ops = relay_ops()
path = write_task(tasks, "W1", "write me a report")
asyncio.run(handle_one(path, answers("Here it is.\n\n[file: report.md]"), str(repo),
                       results, None, ops, LadderSettings(throttle=0.0)))
body = (results / "task-W1.txt").read_text()
sent = DeliveryPath(results).run(body)
check(len(sent.uploads) == 1 and sent.uploads[0][1] == b"the whole report",
      "one Task through the Worker puts one file in the room")
check("Here it is." in sent.posted, "with its answer")
check(not any(o.get("path", "").endswith("/media") for o in relay.ops),
      "and the Worker still uploaded nothing itself")
check(str(results) in body and str(repo) not in body,
      "the marker the Worker wrote points into the outgoing directory, never at the "
      "working copy — the allowlist would refuse that one")
relay.stop()

# The same Task with no relay at all: the answer and the file both still travel.
tmp, repo, results, tasks = workspace()
(repo / "report.md").write_text("no relay here")
path = write_task(tasks, "W2", "write me a report")
asyncio.run(handle_one(path, answers("Here it is.\n\n[file: report.md]"), str(repo),
                       results, None, None, None))
body = (results / "task-W2.txt").read_text()
sent = DeliveryPath(results).run(body)
check(len(sent.uploads) == 1, "a Worker holding no relay token still delivers the file")
check("Here it is." in sent.posted, "and the answer with it")

# And a Task whose file may not be sent leaves a room that knows why.
tmp, repo, results, tasks = workspace()
path = write_task(tasks, "W3", "send me the keys")
asyncio.run(handle_one(path, answers("Sure.\n\n[file: /etc/hosts]"), str(repo),
                       results, None, None, None))
body = (results / "task-W3.txt").read_text()
sent = DeliveryPath(results).run(body)
check(not sent.uploads, "nothing is uploaded")
check("/etc/hosts" in sent.posted and "not be sent" in sent.posted,
      "and the room is told, by name, in the reply it did get")
check("[attachment not sent" not in sent.posted,
      "the transport never has to refuse it: the Worker did not offer it one")

print("\n-- another Task's archived reply is not a file this Task may send --")

# The results directory is the transport's own sendable root, and it holds every
# other Task's archived answer. Only the *staging* subdirectory inside it is the
# permitted area, or one room could ask for another room's reply by name and the
# allowlist — which trusts that directory — could not tell the difference.
tmp, repo, results, tasks = workspace()
victim = results / "task-other.txt"
victim.write_text("the other room's private answer\n")
box = Outbox(results)
delivery = box.stage(f"Here.\n\n[file: {victim}]", ctx_for(repo))
check(not delivery.markers, "another Task's result is not staged")
check(not delivery.sent, "and nothing is reported as sent")
check(any("task-other" in line for line in delivery.refused),
      "the room is told, by name, that it was not sent")
check(victim.read_text() == "the other room's private answer\n",
      "and the file it named is left untouched")

staged = results / "outgoing" / "task-1"
staged.mkdir(parents=True)
already = staged / "report.md"
already.write_text("# staged already\n")
delivery = box.stage(f"Here.\n\n[file: {already}]", ctx_for(repo))
check(len(delivery.sent) == 1, "a file already in the staging area is still sendable")
check(str(already) in delivery.markers[0], "and is delivered where it lies, uncopied")

print("\n-- a Turn that ends normally with nothing to say still finishes the ladder --")

# Not a failure: nothing went wrong, so the broker posts no notice, and a
# rejection here would leave the placeholder reading "on it" for ever.
relay, ops = relay_ops()
try:
    reporter = TurnReporter(ops, LadderSettings(live=False), outbox=Outbox(results))
    body = report(reporter, Scripted(Done(reason=COMPLETED, text="")), ctx_for(repo))
    check(not reporter.rejected, "a silent completion is not a structured rejection")
    check(not body.startswith("[no-send]"), "so the result is not marked unsendable")
    edits = relay.ops_of("edit")
    check(len(edits) == 1, "the placeholder is edited exactly once")
    check("without an answer" in edits[0]["body"],
          "into an honest line rather than being left on the placeholder")
finally:
    relay.stop()

print("\n" + ("PASS — outgoing files green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
