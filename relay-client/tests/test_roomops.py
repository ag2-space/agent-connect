"""Room Ops against a broker on localhost (I1, I2, I3, C8, F7).

The load-bearing one is I1: **a Room Op failure never reaches the consumer's
loop, and the answer still lands**. The scar is an uncaught room-op exception
killing a bearer's only poller over a placeholder message — a cosmetic feature
taking down delivery. So every check here that programs a failure also asserts
two things about it: nothing raised, and `/v1/results` still worked.

The second half is the latch. The original turned room ops off until restart,
which does not self-heal after a broker deploy — the same lesson the ack
cooldown (F4) already carries. The cooldown here is time-gated, and the clock is
injected so the test can prove it *ends* rather than waiting five minutes to
find out.

Run: python3 tests/test_roomops.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import base64
import tempfile
import urllib.parse
from pathlib import Path

from fake_broker import FakeBroker

from ag2_relay_client import roomops as roomops_module
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.egress import EgressAllowlist
from ag2_relay_client.roomops import RoomOps
from ag2_relay_client.transport import AuthRejected, RelayHTTP

ROOM = "!room:ag2.space"
MEDIA = "/v1/rooms/" + urllib.parse.quote(ROOM, safe="") + "/media"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Clock:
    """A clock the test moves, so a 300 s cooldown takes no time to prove."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# --- I3: there is no gateway in this module, and there never may be
source = Path(roomops_module.__file__).read_text()
check("chat.ag2.space" not in source and "https://" not in source.replace(
    "https://<broker", ""),
    "no base URL is compiled into roomops.py — it comes from the credential (I3)")

with FakeBroker() as broker:
    http = RelayHTTP(TokenSource(token=f"{broker.url}|SECRET"))
    clock = Clock()
    rooms = RoomOps(http, clock=clock)

    # --- I2: the payload key is `room_id`. A `room` key is ignored and the op
    # fails with a 400, which is a silent cosmetic outage rather than an error.
    broker.on("POST", "/v1/room", json={"event_id": "$one"})
    event = rooms.message(ROOM, "⏳ On it...")
    sent = broker.took("POST", "/v1/room")[0].json
    check(sent.get("room_id") == ROOM, "op:message sends `room_id`")
    check("room" not in sent, "and never a bare `room` key, which the broker ignores")
    check(sent.get("op") == "message" and sent.get("body") == "⏳ On it...",
          "the op and body arrive as written")
    check(event == "$one", "the event id comes back — the ladder needs it to edit")

    # --- the event id, under every spelling it has been observed under. An id
    # read as absent turns one edited message into two posted ones.
    for answer, expected, label in (
        ({"event_id": "$a"}, "$a", "event_id"),
        ({"eventId": "$b"}, "$b", "eventId"),
        ({"id": "$c"}, "$c", "id"),
        ({"result": {"event_id": "$d"}}, "$d", "result.event_id"),
        ({"data": {"id": "$e"}}, "$e", "data.id"),
        ({"ok": True, "event_id": "$f", "id": "ignored"}, "$f", "event_id over id"),
    ):
        broker.on("POST", "/v1/room", json=answer)
        check(rooms.message(ROOM, "hi") == expected,
              f"the event id is read from `{label}`")

    # --- op:edit
    broker.on("POST", "/v1/room", json={"ok": True})
    check(rooms.edit(ROOM, "$one", "the final answer") is True, "op:edit succeeds")
    sent = broker.took("POST", "/v1/room")[-1].json
    check(sent == {"op": "edit", "room_id": ROOM, "event_id": "$one",
                   "body": "the final answer"},
          "and sends exactly the four fields the verb takes")

    # --- I2: the 4000-char cap is refused locally, and costs no cooldown. The
    # broker answers 413; a longer reply belongs on /v1/results, whose render
    # path chunks it.
    before = len(broker.took("POST", "/v1/room"))
    check(rooms.edit(ROOM, "$one", "x" * 4001) is False,
          "an edit over 4000 chars is refused before it is sent")
    check(len(broker.took("POST", "/v1/room")) == before,
          "and no request is made, so no 413 is spent")
    check(rooms.available, "a body WE declined is not a broker that failed — no cooldown")
    broker.on("POST", "/v1/room", json={"ok": True})
    check(rooms.edit(ROOM, "$one", "x" * 4000) is True, "exactly 4000 chars is sent")

    # --- I2: the worker does not react to the message it was served
    rooms.note_intake_event("$inbound")
    before = len(broker.took("POST", "/v1/room"))
    check(rooms.react(ROOM, "$inbound", "🫡") is False,
          "reacting to an intake event is refused — the broker already 🫡'd it")
    check(len(broker.took("POST", "/v1/room")) == before,
          "and no reaction reaches the room, so the eyes are not doubled")
    broker.on("POST", "/v1/room", json={"ok": True})
    check(rooms.react(ROOM, "$some-other", "👍") is True,
          "a reaction on some other event is a deliberate act, and allowed")

    # --- mentions: a hand-typed mxid in body text does not notify anyone
    broker.on("POST", "/v1/room", json={"event_id": "$m"})
    rooms.message(ROOM, "@alice:ag2.space have a look",
                  mentions=["@alice:ag2.space", "not-an-mxid"])
    sent = broker.took("POST", "/v1/room")[-1].json
    check(sent.get("mentions") == ["@alice:ag2.space"],
          "mentions ride as full mxids, and a malformed one is dropped")
    broker.on("POST", "/v1/room", json={"event_id": "$m"})
    rooms.message(ROOM, "everyone", mentions=[f"@u{n}:ag2.space" for n in range(14)])
    sent = broker.took("POST", "/v1/room")[-1].json
    check(len(sent.get("mentions", [])) == 10,
          "over the broker's cap of 10 the extras are dropped — a message that "
          "lands and notifies nine beats one that does not land")

    # --- a bad room id never becomes a request
    before = len(broker.requests)
    check(rooms.message("room:ag2.space", "hi") is None, "a room id with no sigil is refused")
    check(rooms.message("!bad/../room:x", "hi") is None,
          "and one carrying path separators is refused before it becomes a URL")
    check(len(broker.requests) == before, "neither one reached the wire")

with FakeBroker() as broker:
    # --- I1: failure is never fatal, the latch is time-gated, and the answer
    # still lands via /v1/results.
    http = RelayHTTP(TokenSource(token=f"{broker.url}|SECRET"))
    clock = Clock()
    rooms = RoomOps(http, cooldown_s=300.0, clock=clock)

    broker.on("POST", "/v1/room", status=500, json={"error": "deliverer down"})
    raised = None
    try:
        event = rooms.message(ROOM, "⏳ On it...")
    except Exception as exc:  # noqa: BLE001 — the whole point is that there is none
        raised = exc
        event = "unreached"
    check(raised is None, "a failing Room Op raises nothing into the consumer's loop")
    check(event is None, "it answers `None`, and the caller degrades")
    check(not rooms.available, "the failure marks room ops unavailable")
    check(290 < rooms.cooldown_remaining <= 300, "for the cooldown, which is a duration")

    before = len(broker.took("POST", "/v1/room"))
    check(rooms.message(ROOM, "again") is None and rooms.edit(ROOM, "$x", "y") is False,
          "while cooling down, the ops answer without asking")
    check(len(broker.took("POST", "/v1/room")) == before,
          "so a per-task retry does not add its timeout to every answer")

    # ...and the answer still lands the plain way.
    broker.on("POST", "/v1/results", json={"ok": True})
    http.post("/v1/results", {"id": "task-1", "body": "the answer"})
    landed = broker.took("POST", "/v1/results")
    check(len(landed) == 1 and landed[0].json["body"] == "the answer",
          "the answer reaches the room through /v1/results — losing it is what "
          "must never happen")

    # --- the latch is NOT for the process lifetime: it heals itself, which a
    # long-lived client needs after a broker deploy.
    clock.now += 299.0
    check(not rooms.available, "one second before the cooldown ends, still off")
    clock.now += 2.0
    check(rooms.available, "and after it, room ops are tried again — no restart")
    broker.on("POST", "/v1/room", json={"event_id": "$healed"})
    check(rooms.message(ROOM, "⏳ On it...") == "$healed",
          "a broker that came back is spoken to again")

    # --- a message the broker accepts but names no event id: the ladder cannot
    # be started from there, so it is a failure like any other.
    broker.on("POST", "/v1/room", json={"ok": True})
    check(rooms.message(ROOM, "⏳") is None, "no event id means no ladder")
    check(not rooms.available, "and it trips the cooldown like any other failure")

with FakeBroker() as broker:
    # --- C8 vs I1: a revoked bearer does not raise into the loop, and is not
    # swallowed either. Auth recovery is told directly.
    seen = []
    http = RelayHTTP(TokenSource(token=f"{broker.url}|SECRET"))
    rooms = RoomOps(http, clock=Clock(), on_auth_rejected=seen.append)
    broker.on("POST", "/v1/room", status=401, json={"error": "unknown token"})
    raised = None
    try:
        answer = rooms.message(ROOM, "hi")
    except Exception as exc:  # noqa: BLE001
        raised = exc
        answer = "unreached"
    check(raised is None and answer is None, "a 401 does not raise into the loop (I1)")
    check(len(seen) == 1 and isinstance(seen[0], AuthRejected),
          "but auth recovery is handed the AuthRejected (C8) — it is not one more "
          "optional failure")
    check(not rooms.available, "and room ops cool down like any other failure")

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    # --- upload: paths only, through the allowlist, to the room-scoped route
    top = Path(tmp).resolve()
    root = top / "outbox"
    root.mkdir()
    chart = root / "chart.png"
    chart.write_bytes(b"\x89PNG fake bytes")
    outside = top / "elsewhere"
    outside.mkdir()
    (outside / "id_rsa").write_text("PRIVATE KEY")

    http = RelayHTTP(TokenSource(token=f"{broker.url}|SECRET"))
    clock = Clock()
    rooms = RoomOps(http, allowlist=EgressAllowlist([root]), clock=clock)

    broker.on("POST", MEDIA, json={"ok": True, "mxc": "mxc://ag2.space/abc"})
    result = rooms.upload(ROOM, chart, caption="the chart")
    check(result.ok and result.mxc == "mxc://ag2.space/abc", "an allowlisted file uploads")
    sent = broker.took("POST", MEDIA)[0].json
    check(base64.b64decode(sent["content_b64"]) == b"\x89PNG fake bytes",
          "the bytes that arrive are the bytes of the file that was judged")
    check(sent["filename"] == "chart.png",
          "the filename keeps its extension — the broker guesses the mime from it, "
          "so the extension decides whether the room renders an image")
    check(sent.get("caption") == "the chart", "the caption rides along")
    check(broker.took("POST", MEDIA)[0].path.endswith("/media"),
          "and it goes to the room-scoped media route (F7), not to /v1/results")

    # --- the refusals: no request is made at all
    before = len(broker.requests)
    result = rooms.upload(ROOM, outside / "id_rsa")
    check(not result.ok, "a path outside the allowlist is refused")
    check(len(broker.requests) == before, "and never becomes a request")
    check("outside" in result.reason,
          "the refusal carries a sentence for the room: " + repr(result.reason))
    check(not rooms.upload(ROOM, root / "nope.png").ok, "a missing file is refused")
    check(len(broker.requests) == before, "still nothing on the wire")
    check(rooms.available, "and a refused path is not a broker failure — no cooldown")

    # --- no allowlist at all is fail-closed, and says why
    naked = RoomOps(http, clock=Clock())
    before = len(broker.requests)
    result = naked.upload(ROOM, chart)
    check(not result.ok and "not configured to send files" in result.reason,
          "a client built with no allowlist uploads nothing, and says so")
    check(len(broker.requests) == before, "no bytes left the process")

    # --- and the allowlist itself cannot be swapped out from under the client
    swapped = None
    try:
        rooms.allowlist = EgressAllowlist([top])  # type: ignore[misc]
    except AttributeError as exc:
        swapped = exc
    check(swapped is not None,
          "the allowlist is read-only on the client too — one that can be "
          "REPLACED at runtime is one an attacker only has to reach once")
    check(not rooms.upload(ROOM, outside / "id_rsa").ok,
          "so the outside file is still refused after the attempt")

    # --- an upload the broker refuses is a Room Op failure like any other
    broker.on("POST", MEDIA, status=500, body="nope")
    result = rooms.upload(ROOM, chart)
    check(not result.ok and not rooms.available,
          "a failed upload degrades and trips the cooldown, without raising")

print("\n" + ("PASS — roomops green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
