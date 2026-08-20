"""The events channel: a stream that is never allowed to cost a task.

`GET /v1/events/stream` is Server-Sent Events — a connection held open for as
long as the broker will hold it, carrying workspace events the consumer wants
durably. It is **additive and optional**: nothing in this library starts it, no
module here imports this one, and the only way one exists is a consumer calling
`events(...)`. agent-connect does not; the future sutando shim does.

Every rule below is a scar (asset section K), and all of them are one shape of
the same lesson — *this channel must never be able to hurt task delivery*:

- **Isolation is the contract.** The channel gets its own thread, its own
  connection, its own backoff and its own cursor, shares no mutable state with
  the poll loop, and `run()` swallows everything: a dead network, a revoked
  bearer, a garbled frame, a sink that raises. A channel failure that reached
  the task loop would trade delivery — the product — for telemetry.
- **Every read is bounded.** Isolation is not only about exceptions and threads:
  this channel shares an address space with delivery, so a body with no newline
  read into an unbounded buffer is a route from the telemetry channel to the
  poll loop's memory. It exists — 64 MiB of newline-free body cost ~139 MB of
  RSS and nothing stopped it at any size. So the line read is bounded, the frame
  accumulator is bounded, and a frame that breaks either is poison.
- **A 120 s read timeout.** If not even a keepalive comment arrives in that
  window the TCP path is black-holed: the socket is open, the server thinks it
  is streaming, and nothing will ever arrive. Without the bound the channel
  looks connected forever. With it, it reconnects.
- **Resume from the DURABLE cursor, not the received one.** `Last-Event-ID`
  carries the cursor of the last event the sink has *committed*, so nothing
  between "received" and "durable" is lost; a crash in that window replays the
  event, which is why the sink must be idempotent. (The broker lets the header
  win over `?cursor=`.)
- **A poison frame is skipped, and then never asked for again — as far as it is
  safe to.** A frame that is not JSON, or is JSON but not an object, cannot be
  committed. Handing it to the sink would raise, the connection would drop, the
  resume would come from the same cursor, and the same bad frame would arrive
  again — forever, with the channel busy and no event ever making it through. So
  it is skipped; and because a skip is a decision, the resume cursor advances
  past it too. But the id on an unusable frame is unusable data, and the marker
  it moves never comes back: one frame carrying `id: 5000` was enough to blind
  the channel to every real event below it for the life of the process. So a
  skip may step only a little way past ground the channel has actually reached,
  and when it cannot it says so in `health()` rather than guessing.
- **Fatal vs retryable.** 404 is always fatal — no rotation fixes a missing
  route, and reconnecting forever against one is a busy loop. 401/403 are
  retryable **only while token-rotation recovery is armed**: then they are a
  window (a revoked key, a rotation landing in a moment), and the next attempt
  reads the bearer through the same credential object the poll loop rotates, so
  the new token reaches the stream with no restart. Unarmed, they are fatal —
  because nothing would ever change.
- **The backoff ladder resets on cursor movement, not on reconnection and not
  on a commit.** A gateway that accepts a connection and drops it would
  otherwise hold the ladder at one second forever — and so would a producer that
  omits `id:` entirely, whose every frame commits, moves no cursor, and gets
  replayed from zero on the next connect. Progress is the marker moving.

No bearer leaves this module: the credential is read through `RelayHTTP`, and
the health snapshot carries statuses and reasons, never secrets.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, Iterator, NamedTuple, Optional, Tuple

from .backoff import Backoff
from .transport import AuthRejected, RelayHTTP, RelayHTTPError, close_stream

log = logging.getLogger(__name__)

#: The stream, relative to the provisioned base URL (I3 — no gateway is
#: compiled in anywhere in this package).
EVENTS_STREAM_PATH = "/v1/events/stream"

#: The socket timeout on the open stream. The broker sends a `: keepalive`
#: comment on its own idle cycle, so silence for two minutes is not a quiet
#: workspace — it is a path that will never deliver anything again.
STREAM_READ_TIMEOUT_S = 120.0

#: A missing route cannot be rotated into existence.
FATAL_STATUS = 404

#: The most bytes one SSE line may occupy. The broker's frames are an integer id
#: and a small JSON object; 64 KiB is room for an event a hundred times larger
#: than any it writes, and a bound on the one thing that has no natural end — a
#: body that never sends a newline at all.
MAX_LINE_BYTES = 64 * 1024

#: The most a single frame's `data:` lines may accumulate to before the frame is
#: called poison. `data:` lines join without limit in the grammar; a stream that
#: never sends its blank line would otherwise buffer for as long as it liked.
MAX_FRAME_BYTES = 1024 * 1024

#: How far past ground the channel has actually reached a *skipped* frame's id
#: may claim to be and still be believed. Small on purpose: this is untrusted
#: data moving a marker that nothing pulls back, so the question is not "is this
#: plausible" but "how much of the stream am I willing to lose if it is a lie".
SKIP_LOOKAHEAD = 64


class EventSink:
    """Where events go, and what the channel needs back from that place.

    Duck-typed: this class is the contract written down and a base to inherit if
    that is convenient, not a type anything is checked against. Two methods:

    - `durable_cursor()` — the cursor of the last event that is **durable**, or
      `None` if none is. This is what the channel resumes from, so a sink that
      answers optimistically loses events on the next reconnect. It is also
      expected to be **monotonic** within a process: the channel resumes from
      the later of this cursor and its own marker, so a sink that reports a
      *lower* cursor than it did a moment ago — a rolled-back transaction, a
      restore from backup — is not followed back down, and the events between
      the two markers are not replayed until the process restarts. The
      asymmetry is deliberate: the channel's own marker is ahead precisely
      because of frames it decided to skip, and honouring a regression would
      walk it straight back into the poison it stepped over. A restart is the
      escape hatch, and it is the honest one, because this marker is in memory
      and the durable cursor is the only thing that survives it.
    - `commit(event)` — returning normally means "durable". Raising ends the
      connection; the channel reconnects from `durable_cursor()`, so the event
      arrives again. At-least-once, and idempotence is the sink's job.

    The sink is the consumer's, deliberately: this library owns the wire, and an
    events store is a consumer's schema, retention and lifecycle.
    """

    def durable_cursor(self) -> Optional[int]:
        raise NotImplementedError

    def commit(self, event: Dict[str, Any]) -> None:
        raise NotImplementedError


class _Outcome(NamedTuple):
    """What one connection did, in the only two terms the loop cares about."""

    retryable: bool   # False = fatal; stop, do not spin
    progressed: bool  # the durable cursor moved; the ladder may reset


class _Attempt:
    """One `start()`-to-`stop()` life of the channel.

    The stop flag lives here rather than on the channel because a `stop()` that
    could not reach its thread must not leave a set flag behind for the *next*
    `start()` to inherit. That is a failure that happened: `stop(); start()`
    while the thread was parked in a connect returned a channel that reported
    itself running, spawned nothing, and then died quietly when the old thread
    noticed a flag someone else had set. Per-attempt flags make the next start
    unpoisonable, and make a thread left over from a previous one something that
    can be recognised and ignored rather than something that writes into the
    live channel's health.
    """

    __slots__ = ("stop", "thread")

    def __init__(self) -> None:
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None


def _read_lines(response, max_line: int = MAX_LINE_BYTES) -> Iterator[Tuple[str, bool]]:
    """Yield `(line, over_long)` from a live response, holding no more than
    `max_line` bytes of it at a time.

    Two things this does that iterating the response does not:

    - **It bounds the read.** `for line in response` is `readline()` with no
      limit, and a body that never sends a newline — a broken proxy, a binary
      interstitial, a hostile upstream — is then read into memory until there is
      none. The channel shares its address space with task delivery, so that is
      not a telemetry bug, it is the one class of coupling this module exists to
      forbid. A line past the bound is dropped unread, up to the next newline,
      and reported as `over_long` so its frame can be treated as poison.
    - **It honours the SSE line grammar.** The transport's reader breaks on
      `\\n` alone; SSE allows `\\r` alone as well, and a CR-only frame arriving
      through the old path yielded nothing at all. Each chunk is split the way
      the grammar says, so CR-terminated lines inside it are seen. The *bound*
      is still measured in bytes-to-the-next-`\\n`, because that is the only
      thing the socket layer can stop at: a producer that uses CR alone and
      never a newline is bounded like any other body without one — as poison,
      visibly, rather than by running the process out of memory.
    """
    while True:
        chunk = response.readline(max_line + 1)
        if not chunk:
            return
        if len(chunk) > max_line and not chunk.endswith(b"\n"):
            # `readline` stopped on the byte bound rather than on a terminator.
            # Drop what is in hand and everything up to the next newline without
            # ever holding more than one bound's worth of it.
            yield ("", True)
            while True:
                more = response.readline(max_line + 1)
                if not more or more.endswith(b"\n"):
                    break
            continue
        for raw in chunk.splitlines():
            yield (raw.decode("utf-8", "replace"), False)


def parse_frames(
    response,
    max_line: int = MAX_LINE_BYTES,
    max_frame: int = MAX_FRAME_BYTES,
) -> Iterator[Tuple[str, Optional[str], str]]:
    """Yield `("comment"|"event"|"poison", last_id, text)` from a live SSE
    response.

    The SSE rules that matter here: `data:` lines accumulate, a blank line
    dispatches what accumulated, a leading `:` is a comment (the keepalive), one
    optional space after the field colon is separator and not payload, and `id:`
    is **sticky** — it stays the last event id until another `id:` replaces it,
    which is why it is yielded alongside every frame rather than only the ones
    that carried it.

    `"poison"` is a frame that broke a bound rather than a rule — an over-long
    line, or `data:` that accumulated past `max_frame`. Its text is the reason,
    and the caller skips it exactly as it skips a frame that is not JSON: the
    bytes are already gone, so there is nothing else it could do with it.

    `response` is read through `readline(limit)` — an `HTTPResponse`, or
    anything else with that method, which is what makes a test of this parser a
    test of the read path production uses.
    """
    data = []
    size = 0
    last_id = None
    spoiled = None  # why the frame in progress is already unusable, if it is
    for line, over_long in _read_lines(response, max_line):
        if over_long:
            if spoiled is None:
                spoiled = f"a line longer than {max_line} bytes"
                data = []  # let it go now rather than at the dispatch
                size = 0
            continue
        if line == "":
            if spoiled is not None:
                yield ("poison", last_id, spoiled)
            elif data:
                yield ("event", last_id, "\n".join(data))
            data = []
            size = 0
            spoiled = None
            continue
        if line.startswith(":"):
            yield ("comment", None, line[1:].lstrip())
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            if spoiled is not None:
                continue  # the frame is already lost; do not buffer more of it
            size += len(value) + 1
            if size > max_frame:
                spoiled = f"a frame longer than {max_frame} bytes"
                data = []
            else:
                data.append(value)
        elif field == "id":
            last_id = value


def _as_cursor(value: Optional[str]) -> Optional[int]:
    """The broker's event ids are integer cursors. Anything else is not one."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class EventChannel:
    """One SSE connection to one gateway, reconnecting for as long as it is up.

    Constructing one does **nothing**: no thread, no socket, no request. That is
    the "off by default" half of the contract expressed where it cannot be
    forgotten — a channel that only exists after someone asked for it, and only
    runs after `start()`.
    """

    def __init__(
        self,
        http: RelayHTTP,
        sink: EventSink,
        path: str = EVENTS_STREAM_PATH,
        read_timeout: float = STREAM_READ_TIMEOUT_S,
        auth_retry: Optional[bool] = None,
        backoff: Optional[Backoff] = None,
        thread_name: str = "ag2-relay-events",
    ):
        self._http = http
        self._sink = sink
        self._path = path
        self._read_timeout = float(read_timeout)
        # Armed by default exactly when rotation recovery is possible at all: a
        # durable token source is the thing a rotated key can arrive in. Without
        # one, retrying a 401 waits for a change that cannot happen. A consumer
        # whose recovery is armed some other way says so explicitly.
        self._auth_retry = (
            bool(getattr(http.credentials, "token_file", None) is not None)
            if auth_retry is None else bool(auth_retry)
        )
        # Its own ladder. Sharing the poll loop's would let a broken stream
        # slow down task delivery, which is the one thing this channel may not
        # do — the isolation contract is about state as much as about threads.
        self._backoff = backoff or Backoff()
        self._thread_name = thread_name

        self._attempt: Optional[_Attempt] = None
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._live = None  # the open response, for `stop()` to wake

        # The in-memory half of the cursor: how far this channel is *done*,
        # which is at or ahead of the sink's durable cursor because a skipped
        # frame counts as done. Deliberately not persisted — see the module
        # docstring: the durable cursor belongs to the sink.
        self._resume: Optional[int] = None

        self._health = {
            "status": "init",
            "last_cursor": None,
            "last_event_at": None,
            "retries": 0,
            "skipped": 0,
            "unresumable": 0,
            "last_skip": None,
            "error": None,
        }

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<EventChannel {self._path} {self._health['status']}>"

    # --- lifecycle

    @property
    def running(self) -> bool:
        """Whether *this* channel is up: a live thread nobody has asked to stop.

        A thread left over from a `stop()` that could not interrupt its connect
        is deliberately not counted. It is a daemon holding a socket until the
        connect gives up, not the channel — and `stop()` says so out loud rather
        than letting this property imply a shutdown that has not finished.
        """
        attempt = self._attempt
        return (attempt is not None and attempt.thread is not None
                and attempt.thread.is_alive() and not attempt.stop.is_set())

    def start(self) -> "EventChannel":
        """Spawn the channel's own thread. Idempotent.

        The ordering here is a fix, not a detail. The old version returned early
        whenever a thread was alive — *before* clearing the stop flag — so a
        consumer doing `stop(); start()` inside the connect window got back a
        channel that said `running`, had spawned nothing, and whose old thread
        then saw the flag the `stop()` had set and exited. It reported itself up
        and died silently. A start now either finds a channel genuinely running
        and does nothing, or begins a new attempt with a flag of its own, which
        no earlier `stop()` can have set.
        """
        with self._lock:
            current = self._attempt
            alive = (current is not None and current.thread is not None
                     and current.thread.is_alive())
            if alive and not current.stop.is_set():  # type: ignore[union-attr]
                return self
            if alive:
                log.warning(
                    "events channel: starting a fresh attempt while the previous "
                    "thread is still winding down — it was asked to stop while "
                    "parked in a connect that could not be interrupted, and holds "
                    "its socket until that connect gives up. It cannot affect this "
                    "one: it has its own stop flag and writes into no health but "
                    "its own.",
                )
            attempt = _Attempt()
            # Whatever the last life of this channel ended as, it is not what
            # this one is doing. Leaving `stopped` and its reason in place would
            # have `health()` describe a channel that is already connecting.
            self._health.update(status="starting", error=None)
            # Daemon: a channel wedged in a read must never be the reason a
            # process refuses to exit. Delivery outlives telemetry.
            attempt.thread = threading.Thread(
                target=self.run, args=(attempt,), name=self._thread_name, daemon=True,
            )
            self._attempt = attempt
            attempt.thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the channel to stop, and wait for its thread to end.

        Safe from any thread, safe twice, safe before `start()`. The live
        response is shut down rather than merely closed, so a reader parked in
        a blocking `readline()` returns now instead of when the 120 s read
        timeout says so.

        What it cannot do is interrupt a connect that has not yet produced a
        response: until `open_stream()` returns there is nothing here to shut
        down, and the socket belongs to the opener. Against a server that
        accepts TCP and then never answers, this used to be a `stop()` that
        blocked its whole join, returned, and left a thread reporting itself
        running and holding a socket for as long as the connect timeout allowed.
        It still cannot reach that thread — but it retires it, so `running` and
        `health()` describe the channel and not the leftover, and it says in
        both the log and `health()['error']` that a socket is still held, since
        a supervisor that cannot tell "stopped" from "still holding an fd" finds
        out later and worse.
        """
        with self._lock:
            attempt = self._attempt
        if attempt is not None:
            attempt.stop.set()
        with self._io_lock:
            live = self._live
            self._live = None
        if live is not None:
            close_stream(live)
        if attempt is None:
            return
        thread = attempt.thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout)
        if not thread.is_alive():
            return
        with self._lock:
            if self._attempt is attempt:
                self._attempt = None
        log.warning(
            "events channel: stop() gave up waiting after %ss — the thread is in "
            "a connect that had not answered yet, which nothing here can "
            "interrupt. It is a daemon and will exit when the connect does; its "
            "socket is held until then.", timeout,
        )
        self._note(
            None, status="stopped",
            error=(f"stop(): a connect that had not answered yet could not be "
                   f"interrupted within {timeout}s; its socket is held until it "
                   f"gives up"),
        )

    def health(self) -> Dict[str, Any]:
        """A snapshot of what the channel is doing. Thread-safe, and free of
        secrets: statuses and reasons, never a bearer."""
        with self._lock:
            return dict(self._health)

    # --- the loop

    def run(self, attempt: Optional[_Attempt] = None) -> None:
        """Connect, consume, reconnect, until stopped or fatally refused.

        The isolation contract lives in this method's `except`: *nothing* gets
        out. Whatever went wrong — the network, the bearer, a frame, the sink —
        it stays a fact about this channel, recorded in `health()`.

        `attempt` is the life this run belongs to, supplied by `start()`. A
        consumer driving the loop on a thread of its own passes nothing and gets
        one made for it, so `stop()` still has something to set.
        """
        if attempt is None:
            attempt = _Attempt()
            attempt.thread = threading.current_thread()
            with self._lock:
                self._attempt = attempt
        try:
            self._run(attempt)
        except BaseException as exc:  # noqa: BLE001 — the isolation contract
            log.exception("events channel stopped on an unexpected error")
            self._note(attempt, status="stopped", error=f"channel error: {exc!r}")

    def _run(self, attempt: _Attempt) -> None:
        while not attempt.stop.is_set():
            outcome = self._connect_once(attempt)
            if not outcome.retryable:
                return  # fatal — stopping is the correct loud behaviour
            if attempt.stop.is_set():
                break
            if outcome.progressed:
                # Progress, not mere reconnection and not a mere commit (K): a
                # gateway that accepts a connection and drops it, or a producer
                # whose frames carry no id to resume from, would otherwise pin
                # the ladder at one second and hammer it forever.
                self._backoff.after_success()
            delay = self._backoff.after_error()
            with self._lock:
                if self._attempt is attempt:
                    self._health["retries"] += 1
            # Waiting on the stop event, not sleeping: a `stop()` during a 60 s
            # backoff returns in microseconds instead of a minute.
            attempt.stop.wait(delay)
        # A clean stop, so the error that was in force when it landed goes with
        # it: `health()` is this channel's only observability surface, and a
        # supervisor has to be able to tell an ordinary shutdown from a fault.
        self._note(attempt, status="stopped", error=None)

    def _connect_once(self, attempt: _Attempt) -> _Outcome:
        """One connection, from open to end. Never raises."""
        headers = {}
        resume = self._resume_cursor()
        if resume is not None:
            # The header wins over `?cursor=` server-side, so it is the only
            # place the cursor is said — two ways to say it is two ways to
            # disagree.
            headers["Last-Event-ID"] = str(resume)

        try:
            response = self._http.open_stream(
                self._path, headers=headers, timeout=self._read_timeout,
            )
        except AuthRejected as exc:
            return self._on_auth_rejected(attempt, exc)
        except RelayHTTPError as exc:
            if exc.status == FATAL_STATUS:
                self._note(attempt, status="fatal", error=f"HTTP {exc.status}")
                log.error("events channel: HTTP %s — no such route; stopping. "
                          "A rotation cannot conjure an endpoint.", exc.status)
                return _Outcome(False, False)
            # Everything else the broker can answer — 503 "too many event
            # streams", a 5xx, an edge interstitial — is a state that passes.
            self._note(attempt, status="reconnecting", error=f"HTTP {exc.status}")
            return _Outcome(True, False)
        except Exception as exc:  # noqa: BLE001 — DNS, TCP, TLS, protocol: one thing
            self._note(attempt, status="reconnecting", error=f"connect: {exc}")
            return _Outcome(True, False)

        with self._io_lock:
            if attempt.stop.is_set():
                # `stop()` landed between the open and here, so it found no
                # live response to wake. Closing it is this branch's job — and
                # for a thread this attempt has already been retired from, it is
                # the only place the socket gets closed at all.
                close_stream(response)
                return _Outcome(True, False)
            self._live = response

        self._note(attempt, status="connected", error=None)
        progressed = False
        try:
            progressed = self._consume(response, attempt, resume)
        except Exception as exc:  # noqa: BLE001 — including a sink that raised
            if attempt.stop.is_set():
                # A `stop()` shut the socket down under a reader parked in it,
                # and the two teardowns race: `close_stream` drops `fp` on the
                # caller's thread while the reader is inside `http.client` about
                # to use it, which surfaces as an `AttributeError` on `None`.
                # It is the sound of an ordinary shutdown, and recording it made
                # every clean stop — twelve out of twelve — leave a library's
                # internals in the one field a supervisor reads.
                log.debug("events channel: the reader unwound during stop(): %r", exc)
            else:
                self._note(attempt, status="reconnecting", error=f"stream: {exc}")
        finally:
            with self._io_lock:
                if self._live is response:
                    self._live = None
            close_stream(response)

        if not attempt.stop.is_set() and self.health()["status"] == "connected":
            # A clean EOF is still a disconnection. Without saying so, the
            # backoff wait happens while the status file still advertises the
            # channel as connected.
            self._note(attempt, status="reconnecting", error="stream ended")
        return _Outcome(True, progressed)

    def _on_auth_rejected(self, attempt: _Attempt, exc: AuthRejected) -> _Outcome:
        """401/403 — a window or a wall, depending on whether anything can
        change (K)."""
        self._note(attempt, status="auth_failed", error=f"HTTP {exc.status}")
        if self._auth_retry:
            log.warning(
                "events channel: HTTP %s — retrying; token-rotation recovery is "
                "armed, so the next connect reads whatever bearer has landed.",
                exc.status,
            )
            return _Outcome(True, False)
        log.error(
            "events channel: HTTP %s — stopping. No durable token source is "
            "configured, so no later attempt could present a different bearer.",
            exc.status,
        )
        return _Outcome(False, False)

    # --- the frames

    def _consume(self, response, attempt: _Attempt, floor: Optional[int]) -> bool:
        """Read frames until the stream ends. Returns whether the durable
        cursor moved — the only kind of progress the ladder counts.

        "Moved" and not "committed": a frame the sink accepts but whose `id:` is
        absent or not an integer leaves the resume marker exactly where it was,
        so the next connect asks from the same place and is served the same
        events. Counting that as progress reset the ladder on every connection
        and produced the pinned-at-one-second reconnect loop this rule exists to
        prevent — one reconnect and one duplicate commit per second, forever.
        """
        progressed = False
        for kind, frame_id, payload in parse_frames(response):
            if attempt.stop.is_set():
                break
            if kind == "comment":
                continue  # a keepalive; its only job was to arrive
            cursor = _as_cursor(frame_id)
            if kind == "poison":
                # A frame that broke a bound rather than a rule; the bytes are
                # already dropped, so it is skipped like any other bad frame.
                self._skip(cursor, payload, floor)
                continue
            try:
                event = json.loads(payload)
            except ValueError:
                self._skip(cursor, "not JSON", floor)
                continue
            if not isinstance(event, dict):
                # Valid JSON, but not an event — `data: []` cannot be committed
                # by any sink, and the ones that tried raised.
                self._skip(cursor, "JSON, but not an event object", floor)
                continue
            if cursor is not None and "cursor" not in event:
                # The broker puts the cursor in the body as well as the `id:`;
                # a deployment that only sets the id still hands the sink a
                # complete event.
                event["cursor"] = cursor
            self._sink.commit(event)  # returning IS the durable point
            if cursor is not None and (self._resume is None or cursor > self._resume):
                self._resume = cursor
                progressed = True
            self._note(attempt, status="connected", error=None,
                       last_cursor=self._resume, last_event_at=time.time())
        return progressed

    def _skip(self, cursor: Optional[int], why: str, floor: Optional[int]) -> None:
        """A frame this channel will not commit — and, where it safely can, will
        not ask for again.

        Advancing the resume cursor past a skipped frame is the second half of
        the fix, and the half sparrow left out: skipping alone keeps *this*
        connection alive, but the next reconnect resumes from the durable
        cursor, which is still behind the bad frame, so it arrives again.
        Advancing means one skip per process rather than one per reconnect.

        The other half of *that* is knowing when not to. The id on an unusable
        frame is unusable data too, and the marker it moves is monotonic —
        nothing pulls it back. One frame carrying `id: 5000` was enough to make
        every reconnect ask for everything after 5000: five real events below it
        were never delivered for the life of the process, and `health()` gave no
        signal at all, showing a cursor of `None` and a status cycling as though
        all were well. So a skip may step only a little way past ground the
        channel has actually reached — the cursor it resumed this connection
        from, or a later one it has committed — and where it cannot, it says so
        in `health()` instead of guessing.

        Refusing costs the frame being served again on each reconnect, which the
        ladder climbs against rather than spins on. That is the same bound that
        has always applied to a poison frame carrying no id at all, which there
        was never anything to advance past.

        A skip deliberately does not count as progress either way: a stream
        serving nothing but poison must not reset the backoff ladder and turn
        into a hot loop.
        """
        if self._resume is None:
            base = floor
        elif floor is None:
            base = self._resume
        else:
            base = max(self._resume, floor)

        blind = None
        advance = False
        if cursor is None:
            blind = "it carries no id to resume past"
        elif base is None:
            blind = ("this channel has reached no cursor yet to measure it "
                     "against")
        elif cursor <= base:
            pass  # already behind the marker; there is nothing to advance
        elif cursor <= base + SKIP_LOOKAHEAD:
            advance = True
        else:
            blind = (f"id {cursor} is more than {SKIP_LOOKAHEAD} past cursor "
                     f"{base}, which is as far as an unusable frame's own id is "
                     f"allowed to move the marker")

        where = f"at cursor {cursor}" if cursor is not None else "with no id"
        if advance:
            self._resume = cursor
            log.warning("events channel: skipping the frame %s (%s) — it will "
                        "not be committed, and will not be resumed from",
                        where, why)
        elif blind is None:
            log.warning("events channel: skipping the frame %s (%s) — it will "
                        "not be committed; the resume marker is already past it",
                        where, why)
        else:
            log.warning("events channel: skipping the frame %s (%s) — it will "
                        "not be committed, and it is NOT resumed past because "
                        "%s, so it arrives again on every reconnect until a "
                        "committed event moves the cursor", where, why, blind)

        note = f"{where}: {why}"
        if blind is not None:
            note = f"{note} — not resumed past ({blind})"
        # The failure this makes visible was silent: a channel blinded by one
        # bad id looked healthy from the outside — cursor `None`, status
        # cycling, no count of anything. `unresumable` is the number a
        # supervisor can alert on, and it is a counter rather than an `error`
        # because the next disconnection would overwrite an error and the
        # condition outlives the connection that produced it.
        with self._lock:
            self._health["skipped"] = self._health.get("skipped", 0) + 1
            self._health["last_skip"] = note
            if blind is not None:
                self._health["unresumable"] = self._health.get("unresumable", 0) + 1

    def _resume_cursor(self) -> Optional[int]:
        """Where the next connection asks to start.

        The sink's durable cursor is authoritative — it is the one that survives
        a restart — but this channel's own resume marker can be ahead of it by
        the frames it deliberately skipped, so the later of the two is the
        answer. It is deliberately never the *earlier* of the two: see
        `EventSink.durable_cursor` for why a sink that reports a regressed
        cursor is not followed back down.
        """
        durable = None
        try:
            durable = self._sink.durable_cursor()
        except Exception as exc:  # noqa: BLE001 — a sink is allowed to be down
            log.warning("events channel: the sink could not report its durable "
                        "cursor (%s); resuming from this channel's own marker", exc)
        if not isinstance(durable, int):
            durable = None
        if durable is None:
            return self._resume
        if self._resume is None:
            return durable
        return max(durable, self._resume)

    def _note(self, attempt: Optional[_Attempt], **fields) -> None:
        """Record into the health snapshot — on behalf of `attempt`, or, with
        `None`, on the channel's own behalf.

        A thread a `stop()` could not interrupt is still running somewhere, and
        it will have something to say when its connect finally fails. It must
        not say it here: the snapshot describes the channel, and a retired
        attempt writing "reconnecting" over a `stop()`'s "stopped" is how a
        shutdown comes to look like a channel that is still trying.
        """
        with self._lock:
            if attempt is not None and self._attempt is not attempt:
                return
            self._health.update(fields)


def events(
    http: RelayHTTP,
    sink: EventSink,
    path: str = EVENTS_STREAM_PATH,
    read_timeout: float = STREAM_READ_TIMEOUT_S,
    auth_retry: Optional[bool] = None,
    backoff: Optional[Backoff] = None,
) -> EventChannel:
    """Ask for an events channel, and get one running.

    This function is the whole "opt in" — there is no configuration flag, no
    environment variable and no other caller: a channel exists because a
    consumer wrote this line, and does not otherwise. Nothing else in this
    package imports this module, which is what makes the default provable rather
    than merely intended.

    The returned channel is the consumer's to `stop()`.
    """
    return EventChannel(
        http, sink, path=path, read_timeout=read_timeout,
        auth_retry=auth_retry, backoff=backoff,
    ).start()
