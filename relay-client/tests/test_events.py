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
import json
import queue
import tempfile
import threading
import time
from pathlib import Path

from fake_broker import FakeBroker, StreamAborted

from ag2_relay_client.backoff import Backoff
from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.events import (
    EVENTS_STREAM_PATH,
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
# The frame parser, on its own — the SSE grammar the rest depends on
# =============================================================================
print("\n-- the SSE grammar")


def parsed(text):
    return list(parse_frames(iter(text.encode().splitlines(keepends=True))))


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

# =============================================================================
print("\n-- threads")
check(not channel_threads(), f"no channel thread outlives the suite ({channel_threads()})")

print("\n" + ("PASS — events green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
