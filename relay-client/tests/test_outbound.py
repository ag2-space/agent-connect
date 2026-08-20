"""The result body: markers applied, files sent once, refusals said out loud.

Three scars meet in this file.

**H1** — `[no-send]`, `[REPLIED]` and `[deduped:]` complete the lease *with* a
POST. The temptation is to skip the POST too, since nothing will be rendered;
that leaves the lease to expire and the task to be re-served for ever. So the
marker body goes on the wire verbatim and the deliverer posts nothing.

**F6** — uploads happen before the result POST so failures can be annotated
in-band, and that ordering is exactly what makes a retried POST re-upload. The
room used to fill with copies of the same chart. Here: two `prepare` calls for
one task send one file.

**H3** — the parser strips `[channel:]` for the consumer; the broker's deliverer
is what performs the move, so it is re-stitched onto the POSTed body. Except
under `[dm-only]`, where the redirect was suppressed and re-stitching it would
hand the private body to the shared room anyway.

Run: python3 tests/test_outbound.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import base64
import tempfile
import threading
import urllib.parse
from pathlib import Path

from fake_broker import FakeBroker

from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.egress import EgressAllowlist
from ag2_relay_client.outbound import Outbound
from ag2_relay_client.roomops import RoomOps
from ag2_relay_client.transport import RelayHTTP

ROOM = "!room:ag2.space"
MEDIA = "/v1/rooms/" + urllib.parse.quote(ROOM, safe="") + "/media"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- H1: a skip completes the lease, and the marker body is what is POSTed
out = Outbound()
for body, reason in (
    ("[no-send]", "no-send"),
    ("[REPLIED]", "REPLIED"),
    ("[deduped: task-1755]", "deduped"),
):
    prepared = out.prepare("task-1", ROOM, body)
    check(prepared.skip == reason, f"{body} is recognised as a skip")
    check(prepared.silent, "and marked as completing the lease with no visible post")
    check(prepared.body == body,
          "the body POSTed is the marker itself — a skipped POST would leave the "
          "lease to expire and the task to be re-served")
check(out.prepare("t", ROOM, "[deduped: task-9]").skip_id == "task-9",
      "a deduped skip carries the holder id through")

prepared = out.prepare("task-1", ROOM, "[no-send] recorded for the log only")
check(prepared.body == "[no-send] recorded for the log only",
      "a [no-send] body keeps its text: the broker records it, it just posts nothing")

# --- H3: the redirect goes back onto the POSTed body
prepared = out.prepare("task-2", ROOM, "[channel: !other:ag2.space]\nthe answer")
check(prepared.redirect == "!other:ag2.space", "the redirect is reported")
check(prepared.body == "[channel: !other:ag2.space]\nthe answer",
      "and re-stitched onto the first line — the broker performs the move, not us")

prepared = out.prepare("task-3", ROOM,
                       "[channel: !shared:ag2.space]\n[dm-only]\nprivate")
check(prepared.dm_only and prepared.redirect == "",
      "[dm-only] suppresses the redirect wherever it sits")
check("[channel:" not in prepared.body,
      "and the marker is NOT re-stitched — a suppressed action must not be "
      "handed to the deliverer anyway")
check(prepared.body == "private", "the private body is delivered where it started")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    top = Path(tmp).resolve()
    root = top / "outbox"
    root.mkdir()
    chart = root / "chart.png"
    chart.write_bytes(b"chart-bytes")
    notes = root / "notes.md"
    notes.write_text("notes")
    outside = top / "elsewhere"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE KEY")

    http = RelayHTTP(TokenSource(token=f"{broker.url}|SECRET"))
    clock = Clock()
    rooms = RoomOps(http, allowlist=EgressAllowlist([root]), clock=clock)
    out = Outbound(rooms)
    broker.on("POST", MEDIA, json={"ok": True, "mxc": "mxc://ag2.space/1"})

    # --- the ordinary case
    prepared = out.prepare("task-10", ROOM, f"here it is [file: {chart}]")
    check(prepared.uploaded == ("chart.png",), "the named file is uploaded")
    check(prepared.body == "here it is", "and the marker is out of the body")
    check(len(broker.took("POST", MEDIA)) == 1, "one file, one upload")
    check(base64.b64decode(broker.took("POST", MEDIA)[0].json["content_b64"])
          == b"chart-bytes", "carrying the bytes of the judged file")

    # --- F6: a result-POST retry re-prepares the same body and must not
    # re-upload. This is the check the room's duplicated media asked for.
    again = out.prepare("task-10", ROOM, f"here it is [file: {chart}]")
    check(len(broker.took("POST", MEDIA)) == 1,
          "a second prepare for the same task uploads nothing more (F6)")
    check(again.body == prepared.body and again.uploaded == ("chart.png",),
          "and still reports the file as attached, so the body does not change "
          "between the failed POST and its retry")
    check(out.already_sent("task-10") == ("chart.png",),
          "the ledger says what this task has already put in its room")

    # --- ...but only for that task. A different task is a different delivery.
    out.prepare("task-11", ROOM, f"here it is [file: {chart}]")
    check(len(broker.took("POST", MEDIA)) == 2,
          "a different task uploads the same file again — two answers, two files")

    # --- only success retires an id, so only success forgets what it sent
    out.forget("task-10")
    out.prepare("task-10", ROOM, f"here it is [file: {chart}]")
    check(len(broker.took("POST", MEDIA)) == 3,
          "after forget() — which the wire loop calls when the POST succeeds — "
          "the task starts clean")

    # --- one file named two ways in one answer is still one file
    sent_before = len(broker.took("POST", MEDIA))
    prepared = out.prepare(
        "task-12", ROOM,
        f"[file: {chart}] and again [file: {root}/./chart.png]",
    )
    check(len(broker.took("POST", MEDIA)) == sent_before + 1,
          "two spellings of one path put it in the room once")

    # --- a refused path is annotated in-band, and the answer still lands
    prepared = out.prepare("task-20", ROOM,
                           f"the key is here [file: {outside}/id_rsa]")
    check(prepared.uploaded == (), "an out-of-allowlist path uploads nothing")
    check(prepared.body.startswith("the key is here"),
          "the answer's own text is delivered regardless")
    check("[attachment not sent:" in prepared.body,
          "and the room is told, in the answer: " + repr(prepared.body[-90:]))
    check(len(prepared.refused) == 1, "one refusal, reported once")

    # --- an answer that *explains* the marker is not an answer that issued one
    # The instruction the consumer puts in the agent's preamble spells the value
    # `<path>`, so the sentence an agent writes when a person asks how to send a
    # file contains this marker in ordinary prose, where no code mask reaches.
    # It used to lose the marker out of the middle of that sentence and gain a
    # refusal under it, for a file nobody had asked for.
    taught = ("To put a file in the room, write [file: <path>] on a line of "
              "its own and agent-connect attaches it to this reply.")
    sent_before = len(broker.took("POST", MEDIA))
    prepared = out.prepare("task-22", ROOM, taught)
    check(prepared.body == taught,
          "the explanation is delivered exactly as written: " + repr(prepared.body))
    check(prepared.refused == () and prepared.uploaded == (),
          "with nothing refused and nothing sent — no `[attachment not sent:` "
          "under a sentence that only described the feature")
    check(len(broker.took("POST", MEDIA)) == sent_before,
          "and nothing reached the media route at all")

    # --- a refusal cannot forge a marker out of the path it repeats
    prepared = out.prepare("task-21", ROOM, "[file: /nope/a]b] evil]")
    check("[file:" not in prepared.body and "]b]" not in prepared.body,
          "brackets in a refused path are neutralised before it is repeated")

    # --- more files than one reply carries
    for n in range(12):
        (root / f"f{n}.txt").write_text("x")
    body = " ".join(f"[file: {root}/f{n}.txt]" for n in range(12))
    sent_before = len(broker.took("POST", MEDIA))
    prepared = out.prepare("task-30", ROOM, body)
    check(len(broker.took("POST", MEDIA)) == sent_before + 10,
          "at most ten files go with one reply")
    check(len(prepared.refused) == 2 and "no more than 10" in prepared.refused[0],
          "and the two that did not are said out loud")

    # --- an answer that is only files still needs words
    prepared = out.prepare("task-40", ROOM, f"[file: {notes}]")
    check(prepared.body == "📎 Attached: notes.md.",
          "a body of nothing but markers becomes a sentence, not an empty message")

    # --- I1 again, from the media side: room ops in cooldown still deliver text
    broker.on("POST", MEDIA, status=500, body="down")
    prepared = out.prepare("task-50", ROOM, f"the chart [file: {chart}]")
    check(not rooms.available, "a failed upload cools room ops down")
    check(prepared.body.startswith("the chart") and prepared.refused,
          "the answer still carries its text, with the file's absence explained")
    sent_before = len(broker.requests)
    prepared = out.prepare("task-51", ROOM, f"the chart [file: {chart}]")
    check(len(broker.requests) == sent_before,
          "and the next answer does not pay the timeout again")
    check(prepared.body.startswith("the chart"),
          "while still landing its text — the degradation I1 asks for")

    # --- a client with no room ops at all: markers stripped, refusal in-band,
    # and nothing leaks as literal text
    textonly = Outbound(None)
    prepared = textonly.prepare("task-60", ROOM, f"see this [file: {chart}]")
    check("[file:" not in prepared.body, "the marker is stripped, never delivered raw")
    check("not configured to send files" in prepared.body,
          "and the room is told why nothing arrived")

    # --- I1 at the seam the consumer actually calls. `prepare` says "Never
    # raises", and a docstring is not an enforcement mechanism: the layer under
    # it reads files off a mount that is allowed to fail. This is the last frame
    # before a bearer's only poller.
    class Exploding:
        """A room-ops layer that forgot it must not raise."""

        available = True

        def upload(self, *args, **kwargs):
            raise OSError(5, "Input/output error")

    raised, prepared = None, None
    try:
        prepared = Outbound(Exploding()).prepare(
            "task-90", ROOM, f"the chart [file: {chart}]")
    except BaseException as exc:  # noqa: BLE001 — there must not be one
        raised = exc
    check(raised is None,
          "an upload that raises reaches nothing above it (I1)")
    check(prepared is not None and prepared.body.startswith("the chart")
          and prepared.refused,
          "and the answer still lands, with the file's absence explained")

    # --- a marker shown in a code fence is not an upload instruction
    sent_before = len(broker.requests)
    prepared = out.prepare(
        "task-70", ROOM, f"write:\n\n```\n[file: {outside}/id_rsa]\n```\n\ndone")
    check(len(broker.requests) == sent_before,
          "a marker inside a code fence sends nothing — on this transport that "
          "is an egress guard, not a formatting nicety")
    check("[file:" in prepared.body, "and stays visible in the answer")

# --- the F6 ledger, read from a status call while a delivery writes to it. The
# spec has agent-connect calling this library from an executor, so both happen at
# once. This does NOT go red without the lock on a GIL-bearing CPython — each
# ledger operation was one dict operation, and the interpreter does not let go
# inside one. That is the point: the safety was the interpreter's, not this
# module's, and a free-threaded build hands it back. The lock states it, and this
# says what "stated" has to mean — no exception out of a status read, and nothing
# lost. The ledger is what is raced, so the ledger is what is touched directly.
racy = Outbound()
stop = threading.Event()
crash = []


def fill():
    for n in range(20000):
        racy._remember("task-race", f"/outbox/f{n}.png", f"f{n}.png")
    stop.set()


def poll():
    while not stop.is_set():
        try:
            racy.already_sent("task-race")
        except Exception as exc:  # noqa: BLE001 — the crash is the finding
            crash.append(exc)
            return


writer = threading.Thread(target=fill)
reader = threading.Thread(target=poll)
writer.start()
reader.start()
writer.join()
reader.join()
check(not crash,
      "reading the ledger while it is written does not raise: " + repr(crash[:1]))
check(len(racy.already_sent("task-race")) == 20000,
      "and everything written is still there")

# --- an empty answer is still an empty answer: this module does not invent one
out = Outbound()
check(out.prepare("task-80", ROOM, "").body == "",
      "nothing in, nothing out — H5's 'not ready' guard belongs to the seam that "
      "hands the result across, not here")
check(out.prepare("task-80", ROOM, None).body == "", "and None does not raise")

# --- the seal is only as good as the handle callers already hold ------------
# `RelayClient` seals `_room_ops`, and `RoomOps` seals its allowlist — but
# `Outbound` *carries* the RoomOps, so leaving it writable left the whole thing
# one assignment wide (review 2026-08-20). Swapping in an Outbound built with
# wider roots uploads anything on the machine.
sealed = Outbound(None)
for how, attempt in (
    ("attribute", lambda: setattr(sealed, "room_ops", "WIDER")),
    ("__dict__", lambda: sealed.__dict__.__setitem__("room_ops", "WIDER")),
    ("delete", lambda: delattr(sealed, "room_ops")),
):
    refused = False
    try:
        attempt()
    except AttributeError:
        refused = True
    except TypeError:
        refused = True  # __slots__: there is no __dict__ to write into
    check(refused, f"room_ops cannot be replaced by {how} — it holds the allowlist")

check(not hasattr(sealed, "__dict__"),
      "and there is no instance dict to go round the seal with")

print("\n" + ("PASS — outbound green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
