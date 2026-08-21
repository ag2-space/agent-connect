"""The events channel, one section-K scar per block.

Every check below re-enacts something that happened: a channel that stopped
permanently on its first real connect because the edge answered its default
User-Agent with a 403 it called fatal; a channel that reconnected forever from
the same cursor because one bad frame could not be inserted; a channel that went
quiet during a token rotation and never came back; a connection that stayed
"connected" for hours over a black-holed TCP path. And underneath all of them,
the one property the whole module exists to keep: a failure here must cost
nothing on the task side.

Run: python3 tests/test_events.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import ast
import io
import json
import queue
import socket
import tempfile
import threading
import time
from pathlib import Path

from fake_broker import FakeBroker, StreamAborted

from ag2_relay_client.backoff import Backoff
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.events import (
    EVENTS_STREAM_PATH,
    MAX_LINE_BYTES,
    SKIP_LOOKAHEAD,
    STREAM_READ_TIMEOUT_S,
    EventChannel,
    events,
    parse_frames,
)
from ag2_relay_client.transport import RelayHTTP, close_stream

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def until(predicate, timeout=5.0):
    """Wait for something a background thread is doing. True if it happened."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def channel_threads():
    """Every thread this module started, by the name it gives them."""
    return [t for t in threading.enumerate() if t.name.startswith("ag2-relay-events")]


def fast_backoff():
    """The D1 ladder, in milliseconds — the shape is what is under test, not
    the duration."""
    return Backoff(start=0.02, cap=0.08)


class Sink:
    """The consumer's durable store, as small as the contract allows.

    `commit` returning is the durable point, so `durable_cursor` only moves
    after it: exactly the window the resume rule is about.
    """

    def __init__(self):
        self.events = []
        self.cursor = None
        self.rejects = 0        # how many commits are still programmed to fail
        self.reject_cursor = None  # ...or exactly which one is, once
        self.failures = 0
        self._lock = threading.Lock()

    def durable_cursor(self):
        with self._lock:
            return self.cursor

    def commit(self, event):
        with self._lock:
            if self.reject_cursor is not None and event.get("cursor") == self.reject_cursor:
                self.reject_cursor = None
                self.failures += 1
                raise RuntimeError("the sink is down")
            if self.rejects:
                self.rejects -= 1
                self.failures += 1
                raise RuntimeError("the sink is down")
            self.events.append(event)
            found = event.get("cursor")
            if isinstance(found, int):
                self.cursor = found

    def kinds(self):
        with self._lock:
            return [e.get("kind") for e in self.events]

    def count(self):
        with self._lock:
            return len(self.events)


class Delivery(threading.Thread):
    """A stand-in for the poll loop ticket 02 is building on another branch.

    Deliberately shares everything the real one will share with the channel —
    the same `RelayHTTP`, the same `TokenSource`, the same gateway, the same
    process — so that "the channel died and delivery did not notice" is a claim
    about coupling rather than about two unrelated objects.
    """

    def __init__(self, http):
        super().__init__(name="delivery-standin", daemon=True)
        self.http = http
        self.delivered = queue.Queue()
        self.errors = []
        self.rounds = 0
        # NOT `_stop`: `threading.Thread._stop` is a private method 3.9 calls
        # from `join()`, and shadowing it with an Event makes the join raise.
        self._halt = threading.Event()

    def run(self):
        while not self._halt.is_set():
            try:
                answer = self.http.get("/v1/tasks", params={"wait": 0}, timeout=2)
                for task in answer.get("tasks", []):
                    self.delivered.put(task)
                self.rounds += 1
            except Exception as exc:  # noqa: BLE001 — the point is that there are none
                self.errors.append(repr(exc))
            self._halt.wait(0.005)

    def stop(self):
        self._halt.set()
        self.join(5)


def frame(cursor, payload):
    """One SSE event as the broker writes it: `id:` then `data:`."""
    return f"id: {cursor}\ndata: {payload}\n\n"


def event_frame(cursor, **body):
    return frame(cursor, json.dumps(body, separators=(",", ":")))


# =============================================================================
# Off by default: nothing starts a channel but a consumer asking for one
# =============================================================================
print("\n-- off unless asked")

PACKAGE = _bootstrap.ROOT / "ag2_relay_client"
importers = []
for source in sorted(PACKAGE.glob("*.py")):
    if source.name == "events.py":
        continue
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("events"):
            importers.append(source.name)
        elif isinstance(node, ast.Import):
            if any(a.name.endswith("events") for a in node.names):
                importers.append(source.name)
check(not importers,
      "no other module in the package imports events" +
      (f" (found: {importers})" if importers else ""))

with FakeBroker() as broker:
    creds = TokenSource(token=f"{broker.url}|SECRET")
    http = RelayHTTP(creds)
    sink = Sink()

    broker.sse(EVENTS_STREAM_PATH, lambda request, write: broker.closing.wait(5))
    check(not channel_threads(), "no channel thread exists before one is asked for")

    channel = EventChannel(http, sink, backoff=fast_backoff())
    check(not channel.running, "constructing a channel starts nothing")
    check(not channel_threads(), "constructing a channel spawns no thread")
    check(broker.requests == [], "constructing a channel opens no connection")
    check(channel.health()["status"] == "init", "and it says so: status is 'init'")

    channel.start()
    check(until(lambda: len(channel_threads()) == 1), "start() spawns exactly one thread")
    channel.start()
    check(len(channel_threads()) == 1, "start() twice is still one thread")
    check(until(lambda: broker.took("GET", EVENTS_STREAM_PATH)),
          "and only then does a connection open")
    channel.stop()
    check(not channel.running, "stop() ends the thread")
    check(until(lambda: not channel_threads(), 2.0), "and leaves none behind")
    check(channel.health()["status"] == "stopped", "the snapshot says stopped")
    channel.stop()  # twice, from the same thread, after it is already dead
    check(True, "stop() is safe twice, and safe on a channel already stopped")

# =============================================================================
# B1 + the stream's own headers
# =============================================================================
print("\n-- what the stream asks for (B1)")

with FakeBroker() as broker:
    seen = []

    def one_shot(request, write):
        seen.append(request)
        write(event_frame(1, kind="hello"))

    broker.sse(EVENTS_STREAM_PATH, one_shot)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: sink.count() >= 1), "an event reaches the sink")
        first = seen[0]
        check(first.header("User-Agent") == "sutando-gateway-client/1.0",
              "the stream carries the explicit User-Agent the edge accepts")
        check("urllib" not in first.header("User-Agent").lower(),
              "urllib's default User-Agent never reaches the stream either")
        check(first.header("Accept") == "text/event-stream",
              "the stream asks for an event stream")
        check(first.header("Authorization") == "Bearer SECRET", "with the bearer")
        check(first.header("Last-Event-ID") == "",
              "a cold start resumes from nowhere — no Last-Event-ID")
        check(first.query == "", "the cursor is said once, in the header the broker prefers")
        check(sink.kinds() == ["hello"], "the event body arrives intact")
        check(sink.events[0].get("cursor") == 1,
              "the `id:` fills in the cursor when the body omits it")
    finally:
        channel.stop()

# =============================================================================
# Durable-cursor resume (K: nothing between durable and received is lost)
# =============================================================================
print("\n-- resume from the durable cursor")

with FakeBroker() as broker:
    calls = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            turn = len(calls)
            calls.append(request)
        if turn == 0:
            write(event_frame(7, kind="a"))
            write(event_frame(8, kind="b"))
            return  # clean EOF — reconnect
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, script)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: len(calls) >= 2), "a dropped stream reconnects")
        check(sink.cursor == 8, "the sink committed through cursor 8")
        check(calls[1].header("Last-Event-ID") == "8",
              "the reconnect resumes from the last DURABLE cursor")
        check(channel.health()["last_cursor"] == 8, "the snapshot carries the cursor")
        check(channel.health()["last_event_at"] is not None, "and when it last saw one")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # The window the rule is about: an event received but NOT committed. The
    # sink raises, so the resume must come from cursor 4, not 5 — and the event
    # arrives a second time, which is why a sink must be idempotent.
    calls = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            turn = len(calls)
            calls.append(request)
        if turn == 0:
            write(event_frame(4, kind="durable"))
            write(event_frame(5, kind="lost-in-the-window"))
            broker.closing.wait(2)
        elif turn == 1:
            write(event_frame(5, kind="lost-in-the-window"))
            return
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, script)
    sink = Sink()
    sink.reject_cursor = 5  # cursor 4 lands durably; cursor 5's commit fails
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: len(calls) >= 2), "a sink that raises ends the connection")
        check(sink.failures == 1, "the failing commit happened")
        check(calls[1].header("Last-Event-ID") == "4",
              "the resume is the durable cursor, not the received one")
        check(until(lambda: sink.kinds() == ["durable", "lost-in-the-window"]),
              "so the uncommitted event is delivered again — at-least-once")
        check(channel.running, "and a sink failure never ends the channel")
    finally:
        channel.stop()

# =============================================================================
# Poison frames (K: skipped, never inserted, never replayed forever)
# =============================================================================
print("\n-- a garbled frame is skipped, and not asked for twice")

with FakeBroker() as broker:
    calls = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            turn = len(calls)
            calls.append(request)
        if turn == 0:
            write(event_frame(1, kind="before"))
            write(frame(2, "{not json at all"))          # garbled
            write(frame(3, "[]"))                        # valid JSON, not an event
            write(frame(4, '"a string"'))                # valid JSON, not an event
            write(event_frame(5, kind="after"))
            write(": keepalive\n\n")
            write(event_frame(6, kind="last"))
            return
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, script)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: sink.kinds() == ["before", "after", "last"]),
              "the stream survives three unusable frames and keeps delivering")
        check(sink.count() == 3, "no unusable frame was ever handed to the sink")
        check(until(lambda: len(calls) >= 2), "the connection ends normally afterwards")
        check(calls[1].header("Last-Event-ID") == "6",
              "and the reconnect resumes past everything it handled")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # The poison loop itself: a bad frame is the LAST thing on the connection,
    # so the durable cursor is still behind it. Resuming from the durable cursor
    # would serve the same bad frame again, and again, forever. The channel
    # resumes past it instead.
    calls = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            turn = len(calls)
            calls.append(request)
        if turn == 0:
            write(event_frame(1, kind="good"))
            write(frame(2, "{{{ truncated"))
            return
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, script)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: len(calls) >= 2), "the stream ends after the bad frame")
        check(sink.durable_cursor() == 1, "the sink is durable only through 1")
        check(calls[1].header("Last-Event-ID") == "2",
              "but the reconnect asks from 2 — the bad frame is not replayed")
        check(sink.count() == 1, "and it never reached the sink, not even once")
    finally:
        channel.stop()

# =============================================================================
# Fatal vs retryable (K), and rotation reaching the stream (C2/C3)
# =============================================================================
print("\n-- 404 is a wall, 401 is a window only while rotation is armed")

with FakeBroker() as broker:
    # No durable token source, so nothing could ever present a different bearer.
    broker.on("GET", EVENTS_STREAM_PATH, status=401, json={"error": "unauthorized"})
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    check(until(lambda: not channel.running, 3.0),
          "401 with no rotation recovery armed stops the channel")
    check(channel.health()["status"] == "auth_failed", "and says why: auth_failed")
    check(len(broker.took("GET", EVENTS_STREAM_PATH)) == 1,
          "it does not spin — one attempt, then it stops")
    check("SECRET" not in json.dumps(channel.health()),
          "no bearer appears in the health snapshot")
    channel.stop()

with FakeBroker() as broker:
    broker.on("GET", EVENTS_STREAM_PATH, status=403, json={"error": "missing grant"})
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), Sink(),
                     backoff=fast_backoff())
    check(until(lambda: not channel.running, 3.0), "403 is classified the same way")
    # Not merely "it stopped": a 403 that stopped the channel for some other
    # reason — a parse error, a bug in the loop — would satisfy that alone. The
    # classification is the thing under test, so the snapshot has to name it.
    check(channel.health()["status"] == "auth_failed",
          "and it stopped for the auth reason, not for another one")
    check(channel.health()["error"] == "HTTP 403",
          "naming the status it was refused with")
    check(len(broker.took("GET", EVENTS_STREAM_PATH)) == 1,
          "after exactly one attempt — a missing grant is not something to hammer")
    channel.stop()

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    # Armed: a durable token source exists, so 401 is a window — a revoked key
    # with a rotation on its way. This is the incident: the poller recovered
    # through rotation and the channel, having called its 401 fatal, never came
    # back until the process was restarted.
    token_file = Path(tmp) / "token.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    creds = TokenSource(token_file=token_file)

    bearers = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            bearers.append(request.header("Authorization"))
        write(event_frame(1, kind="through"))
        broker.closing.wait(5)

    broker.on("GET", EVENTS_STREAM_PATH, status=401, json={"error": "unauthorized"})
    sink = Sink()
    channel = events(RelayHTTP(creds), sink, backoff=fast_backoff())
    try:
        check(until(lambda: len(broker.took("GET", EVENTS_STREAM_PATH)) >= 2, 3.0),
              "401 with rotation recovery armed keeps reconnecting")
        check(channel.running, "the channel is still alive through the auth window")
        check(channel.health()["status"] == "auth_failed",
              "while saying honestly what it is stuck on")

        # The rotation lands, applied by whoever owns auth recovery. The
        # channel is handed nothing: it reads the same credential object.
        token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CSECOND\n")
        rotation = creds.reload()
        check(rotation.rotated, "the durable token source rotated")
        broker.sse(EVENTS_STREAM_PATH, script)

        check(until(lambda: sink.count() >= 1, 5.0),
              "the stream comes back on its own — no restart, no rebuilt object")
        check(bearers and bearers[0] == "Bearer SECOND",
              "and the reconnect carries the ROTATED bearer, held by reference")
    finally:
        channel.stop()

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    # A rotation with no auth failure at all: the broker re-auths a live stream
    # on every wake and closes it when the bearer it started with stops
    # resolving, so the reconnect is the only place the new token can land.
    token_file = Path(tmp) / "token.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    creds = TokenSource(token_file=token_file)
    bearers = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            turn = len(bearers)
            bearers.append(request.header("Authorization"))
        write(event_frame(turn + 1, kind="tick"))
        if turn == 0:
            return  # the broker drops the stream, as it does on a lapsed bearer
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, script)
    sink = Sink()
    channel = events(RelayHTTP(creds), sink, backoff=fast_backoff())
    try:
        check(until(lambda: len(bearers) >= 1), "the stream is up on the first bearer")
        token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CSECOND\n")
        creds.reload()
        check(until(lambda: len(bearers) >= 2), "and reconnects after the drop")
        check(bearers[0] == "Bearer FIRST" and bearers[1] == "Bearer SECOND",
              "the rotation reached the stream without restarting the channel")
    finally:
        channel.stop()

with FakeBroker() as broker, tempfile.TemporaryDirectory() as tmp:
    token_file = Path(tmp) / "token.env"
    token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
    channel = events(RelayHTTP(TokenSource(token_file=token_file)), Sink(),
                     path="/v1/events/stream", backoff=fast_backoff())
    # Nothing is programmed for the route, so the fake broker answers 404 — a
    # deployment without the endpoint, which is what 404 means here.
    check(until(lambda: not channel.running, 3.0),
          "404 is fatal even with rotation recovery armed")
    check(channel.health()["status"] == "fatal", "and is named as such")
    check(len(broker.took("GET", EVENTS_STREAM_PATH)) == 1,
          "one attempt: a missing route is not something to hammer")
    channel.stop()

with FakeBroker() as broker:
    # Everything else the broker can say is a passing state — including the
    # stream-count cap, which is the one a busy fleet actually meets.
    broker.on("GET", EVENTS_STREAM_PATH, status=503, json={"error": "too many event streams"})
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), Sink(),
                     backoff=fast_backoff())
    try:
        check(until(lambda: len(broker.took("GET", EVENTS_STREAM_PATH)) >= 3, 3.0),
              "a 503 is retried, not fatal")
        check(channel.health()["status"] == "reconnecting", "status: reconnecting")
        check(channel.health()["error"] == "HTTP 503", "carrying the status, not the body")
    finally:
        channel.stop()

# =============================================================================
# The read timeout (K: 120 s, because a black-holed path never says anything)
# =============================================================================
print("\n-- a black-holed stream is detected, not waited on forever")

check(STREAM_READ_TIMEOUT_S == 120.0,
      "the stream read timeout is 120 s, as the scar records")

with FakeBroker() as broker:
    calls = []
    lock = threading.Lock()

    def silent(request, write):
        with lock:
            calls.append(request)
        # Connected, and then nothing: not even a keepalive. This is what a
        # black-holed TCP path looks like from the client's side.
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, silent)
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), Sink(),
                     read_timeout=0.4, backoff=fast_backoff())
    try:
        check(until(lambda: len(calls) >= 2, 4.0),
              "silence past the read timeout reconnects rather than hanging")
        check(channel.health()["status"] in ("connected", "reconnecting"),
              "and the channel keeps working through it")
    finally:
        channel.stop()

# =============================================================================
# The ladder resets on progress, not on reconnection (K)
# =============================================================================
print("\n-- backoff resets on cursor progress")

with FakeBroker() as broker:
    # A gateway that accepts the connection and drops it immediately. Resetting
    # on "we connected" would pin the ladder at its first rung forever.
    def instant_drop(request, write):
        raise StreamAborted()

    broker.sse(EVENTS_STREAM_PATH, instant_drop)
    ladder = Backoff(start=0.01, cap=0.32)
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), Sink(),
                     backoff=ladder)
    try:
        check(until(lambda: ladder.seconds >= 0.04, 4.0),
              "connect-and-drop climbs the ladder — reconnection is not progress")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # Same shape, but every connection delivers one event before it drops.
    counter = []
    lock = threading.Lock()

    def one_then_drop(request, write):
        with lock:
            turn = len(counter)
            counter.append(1)
        write(event_frame(turn + 1, kind="progress"))
        broker.closing.wait(0.05)

    broker.sse(EVENTS_STREAM_PATH, one_then_drop)
    ladder = Backoff(start=0.01, cap=1.0)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=ladder)
    try:
        check(until(lambda: sink.count() >= 4, 6.0), "several connections each deliver")
        check(ladder.seconds <= 0.02,
              f"and the ladder stays at its first rung ({ladder.seconds}s) — progress resets it")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # Poison is not progress: a stream serving nothing but unusable frames must
    # not reset the ladder and become a hot reconnect loop.
    def poison_only(request, write):
        write(frame(1, "{ truncated"))

    broker.sse(EVENTS_STREAM_PATH, poison_only)
    ladder = Backoff(start=0.01, cap=0.32)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=ladder)
    try:
        check(until(lambda: ladder.seconds >= 0.04, 4.0),
              "a poison-only stream climbs the ladder rather than spinning")
        check(sink.count() == 0, "and commits nothing")
    finally:
        channel.stop()

# =============================================================================
# Isolation — the whole reason the module is built this way
# =============================================================================
print("\n-- isolation: killing the channel costs delivery nothing")

with FakeBroker() as broker:
    broker.on("GET", "/v1/tasks", json={"tasks": [{"id": "task-1"}]})

    live = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            turn = len(live)
            live.append(request)
        write(event_frame(turn + 1, kind="tick"))
        if turn == 0:
            return                         # a clean end
        if turn == 1:
            raise StreamAborted()          # and then one that dies mid-stream
        # Every later connection keeps ticking, so there is always another
        # event on its way for whatever the test breaks next.
        cursor = 100 * turn
        while not broker.closing.wait(0.02):
            cursor += 1
            write(event_frame(cursor, kind="tick"))

    broker.sse(EVENTS_STREAM_PATH, script)
    creds = TokenSource(token=f"{broker.url}|SECRET")
    http = RelayHTTP(creds)

    delivery = Delivery(http)
    delivery.start()
    check(until(lambda: delivery.rounds >= 3), "delivery is running before the channel")
    before = delivery.rounds

    sink = Sink()
    channel = EventChannel(http, sink, backoff=fast_backoff())
    channel.start()
    try:
        check(until(lambda: sink.count() >= 1), "the channel is streaming alongside it")

        # 1. the stream is cut mid-body by the far end
        check(until(lambda: len(live) >= 3, 5.0), "an aborted stream reconnects")

        # 2. the sink starts raising on every event
        with sink._lock:
            sink.rejects = 3
        check(until(lambda: sink.failures >= 1, 5.0), "a sink that raises does not escape")
        check(channel.running, "the channel thread survives it")

        # 3. the live connection is killed from outside, the way an OS-level
        #    socket death would kill it — the closest a test can get to
        #    "the channel thread was killed mid-stream".
        # `_live` is None while the channel is between connections, which after
        # three sink failures it well may be — wait for one to exist first.
        check(until(lambda: channel._live is not None, 5.0),
              "the channel has a live connection to kill")
        killed = channel._live  # deliberately reaching in: this is the incident
        connections, committed = len(live), sink.count()
        if killed is not None:
            close_stream(killed)
        check(until(lambda: len(live) > connections, 5.0),
              "a socket killed under it brings the channel back on a new connection")
        check(until(lambda: sink.count() > committed, 5.0), "and events flow again")
        check(channel.running, "the thread itself never died")

        # 4. and finally the ordinary stop
        channel.stop()
        check(not channel.running, "the channel is gone")
    finally:
        channel.stop()

    check(until(lambda: delivery.rounds >= before + 5, 5.0),
          "delivery kept polling through every one of those")
    check(delivery.errors == [], f"and saw no error at all ({delivery.errors[:2]})")
    check(delivery.delivered.qsize() > 0, "tasks kept arriving")
    check(creds.secret == "SECRET", "the channel changed nothing about the credential")
    delivery.stop()
    check(not delivery.is_alive(), "the stand-in poller stops cleanly")

with FakeBroker() as broker:
    # Isolation, the other direction: a channel whose gateway is simply gone.
    # Nothing is programmed, the port is closed under it — and delivery, which
    # has its own connection, is untouched.
    broker.on("GET", "/v1/tasks", json={"tasks": []})
    http = RelayHTTP(TokenSource(token=f"{broker.url}|SECRET"))
    delivery = Delivery(http)
    delivery.start()

    dead = RelayHTTP(TokenSource(token="http://127.0.0.1:1|SECRET"))
    channel = events(dead, Sink(), backoff=fast_backoff())
    try:
        check(until(lambda: channel.health()["status"] == "reconnecting", 4.0),
              "a channel pointed at nothing reconnects quietly")
        check(channel.health()["error"].startswith("connect:"),
              "recording a connect failure, not raising one")
        rounds = delivery.rounds
        check(until(lambda: delivery.rounds >= rounds + 5, 5.0),
              "while delivery carries on at full cadence")
        check(delivery.errors == [], "with no error of its own")
    finally:
        channel.stop()
        delivery.stop()

# =============================================================================
# Progress is the cursor MOVING — not a reconnection, and not a commit
# =============================================================================
print("\n-- a commit that moves no cursor is not progress")

with FakeBroker() as broker:
    # A producer that omits `id:` entirely. Every frame commits, so the old rule
    # ("a committed frame is progress") reset the ladder on every connection —
    # and because the marker never moved, no `Last-Event-ID` was ever sent, the
    # server replayed from the start, and the same events were committed again.
    # With production's ladder that settles at exactly one second: one reconnect
    # and one duplicate commit per second, forever.
    resumes = []
    lock = threading.Lock()

    def no_id(request, write):
        with lock:
            resumes.append(request.header("Last-Event-Id"))
        write('data: {"kind":"noid"}\n\n')  # committed, and unresumable

    broker.sse(EVENTS_STREAM_PATH, no_id)
    ladder = Backoff(start=0.01, cap=0.32)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=ladder)
    try:
        check(until(lambda: sink.count() >= 3, 5.0),
              "every connection commits its event, and the sink keeps them")
        check(until(lambda: ladder.seconds >= 0.04, 5.0),
              f"and the ladder climbs anyway ({ladder.seconds}s) — a commit that "
              "moves no cursor is not progress")
        check(channel.health()["last_cursor"] is None,
              "because the resume marker never moved at all")
        check(all(r == "" for r in resumes),
              "which is exactly why no reconnect can ask for anything newer")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # The same shape with an id that never advances: a producer stuck re-serving
    # cursor 7. It commits, so it looks like progress; it resumes from 7, so it
    # is the same event forever.
    def same_id(request, write):
        write(event_frame(7, kind="stuck"))

    broker.sse(EVENTS_STREAM_PATH, same_id)
    ladder = Backoff(start=0.01, cap=0.32)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=ladder)
    try:
        check(until(lambda: sink.count() >= 3, 5.0), "the same cursor is re-served")
        check(until(lambda: ladder.seconds >= 0.04, 5.0),
              "and a cursor that does not move does not reset the ladder either")
    finally:
        channel.stop()

# =============================================================================
# A skip may only step a little way past ground the channel has reached
# =============================================================================
print("\n-- a poison frame's id does not get to blind the channel")

with FakeBroker() as broker:
    # One unparseable frame carrying `id: 5000` used to set the resume marker to
    # 5000 — monotonic, never pulled back — so every reconnect asked for
    # everything after 5000, the broker served nothing, and the five real events
    # below it were never delivered for the life of the process. `health()` said
    # nothing: cursor `None`, status cycling as though all were well.
    calls = []
    lock = threading.Lock()

    def wait_after(request, write):
        resumed = int(request.header("Last-Event-Id") or 0)
        with lock:
            turn = len(calls)
            calls.append(resumed)
        if turn == 0:
            write(frame(5000, "{ not json at all"))  # the poison, wildly ahead
            return
        for cursor in range(1, 6):
            if cursor > resumed:  # the broker's own rule: strictly after
                write(event_frame(cursor, kind=f"real-{cursor}"))
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, wait_after)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: sink.count() >= 5, 5.0),
              "all five real events below the bad id are delivered")
        check(until(lambda: len(calls) >= 2, 5.0) and calls[1] == 0,
              "because the reconnect did not ask for everything after 5000")
        check(channel.health()["skipped"] == 1, "the skip is counted in health()")
        check("5000" in (channel.health()["last_skip"] or ""),
              f"and named there ({channel.health()['last_skip']})")
        check(until(lambda: channel.health()["last_cursor"] == 5, 5.0),
              "and the cursor the channel really reached is the one it reports")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # The bound is a bound, not a ban: a bad frame within reach of where the
    # channel already is still gets stepped over, which is the whole point of
    # skipping — one skip per process, not one per reconnect.
    calls = []
    lock = threading.Lock()

    def near(request, write):
        with lock:
            turn = len(calls)
            calls.append(request)
        if turn == 0:
            write(event_frame(10, kind="good"))
            write(frame(10 + SKIP_LOOKAHEAD, "{ truncated"))  # just inside
            return
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, near)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: len(calls) >= 2, 5.0), "the stream ends after the bad frame")
        check(calls[1].header("Last-Event-ID") == str(10 + SKIP_LOOKAHEAD),
              "a skip within the lookahead still advances past the frame")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # The mirror image, and the case the ticket's "one skip per process" never
    # covered: a poison frame with no id at all. There is nothing to advance
    # past, so it arrives again on every reconnect — bounded by the ladder
    # rather than by the cursor, which is worth saying out loud in health()
    # rather than leaving as a silence.
    def no_id_poison(request, write):
        write("data: { truncated\n\n")

    broker.sse(EVENTS_STREAM_PATH, no_id_poison)
    ladder = Backoff(start=0.01, cap=0.32)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=ladder)
    try:
        check(until(lambda: channel.health()["skipped"] >= 2, 5.0),
              "an id-less poison frame is re-served on every reconnect")
        check("no id" in (channel.health()["last_skip"] or ""),
              f"and health() says why it cannot be resumed past "
              f"({channel.health()['last_skip']})")
        check(channel.health()["unresumable"] >= 2,
              "counted as unresumable — the number a supervisor can alert on")
        check(until(lambda: ladder.seconds >= 0.04, 5.0),
              "while the ladder climbs against it instead of spinning")
        check(sink.count() == 0, "and nothing unusable ever reaches the sink")
    finally:
        channel.stop()

# =============================================================================
# Bounded reads — the one confirmed route from this channel to delivery's memory
# =============================================================================
print("\n-- an unbounded body is poison, not a memory leak")

with FakeBroker() as broker:
    calls = []
    lock = threading.Lock()

    def flood(request, write):
        with lock:
            turn = len(calls)
            calls.append(request)
        if turn == 0:
            write("id: 1\ndata: ")
            for _ in range(48):            # 384 KiB with no newline in it
                write("x" * 8192)
            write("\n\n")                  # ...and only now a terminator
            write(event_frame(2, kind="after-the-flood"))
            write("id: 3\n")
            for _ in range(24):            # data lines that sum past the frame bound
                write("data: " + "y" * 60000 + "\n")
            write("\n")
            write(event_frame(4, kind="after-the-frame"))
            return
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, flood)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: sink.kinds() == ["after-the-flood", "after-the-frame"], 10.0),
              f"the channel reads past both over-sized frames and keeps "
              f"delivering ({sink.kinds()})")
        check(channel.health()["skipped"] == 2,
              "each of them is a skipped frame, not a buffer")
        check("longer than" in (channel.health()["last_skip"] or ""),
              f"named by the bound it broke ({channel.health()['last_skip']})")
        check(channel.running, "and the channel is still up")
    finally:
        channel.stop()

with FakeBroker() as broker:
    # The pure form: a body with no newline anywhere, and then the far end goes.
    def newline_free(request, write):
        for _ in range(32):
            write("x" * 8192)
        raise StreamAborted()

    broker.sse(EVENTS_STREAM_PATH, newline_free)
    sink = Sink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: channel.health()["retries"] >= 2, 8.0),
              "a body with no newline ends as a reconnect, not as a buffer")
        check(sink.count() == 0, "nothing was committed")
        check(channel.running, "and the channel survives it")
    finally:
        channel.stop()

# =============================================================================
# stop() before the first byte, and the start() that follows it
# =============================================================================
print("\n-- stop() during a connect that will never answer")

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(8)
accepted = []


def accept_forever():
    """Accept, and then say nothing at all — the black-holed connect."""
    while True:
        try:
            accepted.append(listener.accept()[0])
        except OSError:
            return


acceptor = threading.Thread(target=accept_forever, name="never-answers", daemon=True)
acceptor.start()

port = listener.getsockname()[1]
dead_http = RelayHTTP(TokenSource(token=f"http://127.0.0.1:{port}|SECRET"))
# Short, so the leftover thread this test is *about* does not outlive the suite.
channel = EventChannel(dead_http, Sink(), read_timeout=1.5, backoff=fast_backoff())
try:
    channel.start()
    check(until(lambda: len(accepted) >= 1, 5.0),
          "the channel is parked in a connect that has produced no byte")

    started = time.monotonic()
    channel.stop(timeout=0.3)
    check(time.monotonic() - started < 2.0, "stop() returns rather than hanging on it")
    check(not channel.running,
          "and does not report the channel as still running afterwards")
    check(channel.health()["status"] == "stopped", "the snapshot says stopped")
    check("could not be interrupted" in (channel.health()["error"] or ""),
          f"while saying honestly what it could not reach "
          f"({channel.health()['error']})")

    # And the compounding half: start() used to return early on the live thread
    # *before* clearing the stop flag, so this call handed back a channel that
    # said running, spawned nothing, and died when the old thread saw the flag.
    connects = len(accepted)
    channel.start()
    check(channel.running, "start() after a stop that could not take really starts")
    check(until(lambda: len(accepted) > connects, 5.0),
          "a new attempt of its own, which connects rather than inheriting a stop")
    check(channel.health()["status"] != "stopped",
          "and the channel is no longer describing itself as stopped")
finally:
    channel.stop(timeout=0.3)
    check(until(lambda: not channel_threads(), 6.0),
          "and every thread it left behind exits when its connect gives up")
    listener.close()
    for conn in accepted:
        conn.close()

# =============================================================================
# A clean stop leaves nothing in the snapshot but "stopped"
# =============================================================================
print("\n-- health() after an ordinary stop")

dirty = []
for _ in range(6):
    with FakeBroker() as broker:
        def ticking(request, write):
            cursor = 0
            while not broker.closing.wait(0.002):
                cursor += 1
                write(event_frame(cursor, kind="tick"))

        broker.sse(EVENTS_STREAM_PATH, ticking)
        sink = Sink()
        channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                         backoff=fast_backoff())
        check_ok = until(lambda: sink.count() >= 2, 5.0)
        channel.stop()
        snapshot = channel.health()
        if not check_ok or snapshot["status"] != "stopped" or snapshot["error"] is not None:
            dirty.append(snapshot)

# `close_stream` drops `fp` on the caller's thread while the reader is inside
# http.client about to use it, and the AttributeError that follows used to be
# written straight into the snapshot — 12 times out of 12, a normal shutdown
# reading `error: "stream: 'NoneType' object has no attribute 'close'"`.
check(not dirty,
      f"six clean stops leave no library internals in health() ({dirty[:1]})")

# =============================================================================
# A sink that cannot answer at all
# =============================================================================
print("\n-- a sink that cannot report its durable cursor")


class MuteSink(Sink):
    """A store that is up enough to commit and down enough to be asked."""

    def durable_cursor(self):
        raise RuntimeError("the store cannot be reached")


with FakeBroker() as broker:
    calls = []
    lock = threading.Lock()

    def script(request, write):
        with lock:
            turn = len(calls)
            calls.append(request)
        if turn == 0:
            write(event_frame(11, kind="through"))
            return
        broker.closing.wait(5)

    broker.sse(EVENTS_STREAM_PATH, script)
    sink = MuteSink()
    channel = events(RelayHTTP(TokenSource(token=f"{broker.url}|SECRET")), sink,
                     backoff=fast_backoff())
    try:
        check(until(lambda: len(calls) >= 2, 5.0),
              "a sink that raises on durable_cursor() does not stop the channel")
        check(calls[0].header("Last-Event-ID") == "",
              "the first connect has nothing of its own to fall back on")
        check(calls[1].header("Last-Event-ID") == "11",
              "and the reconnect falls back to the channel's own marker")
        check(sink.count() == 1, "while events still reach it")
        check(channel.running, "and the channel is still up")
    finally:
        channel.stop()

# =============================================================================
# The frame parser, on its own — the SSE grammar the rest depends on
# =============================================================================
print("\n-- the SSE grammar")


def parsed(text, **bounds):
    """Parse `text` the way production parses a stream: through `readline`.

    This used to hand the parser `bytes.splitlines(keepends=True)`, which is a
    *different tokenizer* from the one the module actually reads with — it
    breaks on a lone `\\r` where `HTTPResponse` does not. So the only coverage
    the parser had was coverage of a read path nothing uses, and a CR-only frame
    passed here while yielding nothing at all on the wire. A file object with a
    `readline(limit)` is what the channel really holds.
    """
    return list(parse_frames(io.BytesIO(text.encode()), **bounds))


check(parsed("data: {}\n\n") == [("event", None, "{}")],
      "a data line with no id yields an event with no cursor")
check(parsed("id: 4\ndata: a\ndata: b\n\n") == [("event", "4", "a\nb")],
      "data lines accumulate until the blank line dispatches them")
check(parsed(": keepalive\n\n") == [("comment", None, "keepalive")],
      "a leading colon is a comment, not an event")
check(parsed("id: 1\ndata: x\n\ndata: y\n\n")
      == [("event", "1", "x"), ("event", "1", "y")],
      "the id is sticky: it stays until another id replaces it")
check(parsed("data:no-space\n\n") == [("event", None, "no-space")],
      "exactly one space after the colon is separator, and only one")
check(parsed("data:  two\n\n") == [("event", None, " two")],
      "the second space is payload")
check(parsed("retry: 3000\ndata: x\n\n") == [("event", None, "x")],
      "fields this client does not use are ignored, not fatal")
check(parsed("data: x\r\n\r\n") == [("event", None, "x")], "CRLF line endings work")
check(parsed("data: x\n") == [], "an undispatched frame is not delivered")
check(parsed("id: 3\rdata: z\r\r\n") == [("event", "3", "z")],
      "and a CR-only frame is a frame — the grammar allows it, and the old read "
      "path silently yielded nothing for it")
check(parsed("data: a\rdata: b\r\n\r\n") == [("event", None, "a\nb")],
      "CR-terminated lines inside one read accumulate like any others")

# The bound, at the level where it is decided: a line that never ends must not
# be read into memory, because this channel shares an address space with task
# delivery and 64 MiB of newline-free body once grew RSS by ~139 MB.
class Counted:
    """A response that remembers the largest read the parser ever asked for."""

    def __init__(self, payload):
        self._buf = io.BytesIO(payload)
        self.biggest = 0

    def readline(self, limit=-1):
        self.biggest = max(self.biggest, limit)
        return self._buf.readline(limit)


endless = Counted(b"id: 9\ndata: " + b"x" * (512 * 1024) + b"\n\n" + b"data: {}\n\n")
frames = list(parse_frames(endless))
check(0 < endless.biggest <= MAX_LINE_BYTES + 1,
      f"the parser never asks for an unbounded line ({endless.biggest} bytes at most)")
check(frames[0][0] == "poison" and frames[0][1] == "9",
      "a line past the bound is poison, carrying the id it arrived under")
check("longer than" in frames[0][2], "and says which bound it broke")
check(frames[1] == ("event", "9", "{}"),
      "and the parser resynchronises: the next frame is read normally")

huge = Counted(b"id: 4\n" + b"".join(b"data: " + b"y" * 4000 + b"\n" for _ in range(64))
               + b"\n" + b"data: {}\n\n")
frames = list(parse_frames(huge, max_frame=100_000))
check(frames[0][0] == "poison" and "frame longer than" in frames[0][2],
      "data lines that accumulate past the frame bound are poison too")
check(frames[1] == ("event", "4", "{}"), "and that frame is dropped, not the stream")

nothing = Counted(b"z" * (256 * 1024))  # a body with no newline anywhere, then EOF
check(list(parse_frames(nothing)) == [],
      "a body with no newline at all yields nothing rather than buffering it")
check(0 < nothing.biggest <= MAX_LINE_BYTES + 1,
      "and is read a bounded piece at a time to the end")

# =============================================================================
print("\n-- threads")
check(not channel_threads(), f"no channel thread outlives the suite ({channel_threads()})")

print("\n" + ("PASS — events green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
