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
- **A 120 s read timeout.** If not even a keepalive comment arrives in that
  window the TCP path is black-holed: the socket is open, the server thinks it
  is streaming, and nothing will ever arrive. Without the bound the channel
  looks connected forever. With it, it reconnects.
- **Resume from the DURABLE cursor, not the received one.** `Last-Event-ID`
  carries the cursor of the last event the sink has *committed*, so nothing
  between "received" and "durable" is lost; a crash in that window replays the
  event, which is why the sink must be idempotent. (The broker lets the header
  win over `?cursor=`.)
- **A poison frame is skipped, and then never asked for again.** A frame that is
  not JSON, or is JSON but not an object, cannot be committed. Handing it to the
  sink would raise, the connection would drop, the resume would come from the
  same cursor, and the same bad frame would arrive again — forever, with the
  channel busy and no event ever making it through. So it is skipped; and
  because a skip is a decision, the resume cursor advances past it too. A
  restart is the only thing that can see it again (in-memory, by design: the
  durable cursor is the sink's, and this channel does not get to write it), and
  it is skipped again just as fast.
- **Fatal vs retryable.** 404 is always fatal — no rotation fixes a missing
  route, and reconnecting forever against one is a busy loop. 401/403 are
  retryable **only while token-rotation recovery is armed**: then they are a
  window (a revoked key, a rotation landing in a moment), and the next attempt
  reads the bearer through the same credential object the poll loop rotates, so
  the new token reaches the stream with no restart. Unarmed, they are fatal —
  because nothing would ever change.
- **The backoff ladder resets on cursor progress, not on reconnection.** A
  gateway that accepts a connection and immediately drops it would otherwise
  hold the ladder at one second forever.

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


class EventSink:
    """Where events go, and what the channel needs back from that place.

    Duck-typed: this class is the contract written down and a base to inherit if
    that is convenient, not a type anything is checked against. Two methods:

    - `durable_cursor()` — the cursor of the last event that is **durable**, or
      `None` if none is. This is what the channel resumes from, so a sink that
      answers optimistically loses events on the next reconnect.
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


def parse_frames(response) -> Iterator[Tuple[str, Optional[str], str]]:
    """Yield `("comment"|"event", last_id, text)` from a live SSE response.

    The SSE rules that matter here: `data:` lines accumulate, a blank line
    dispatches what accumulated, a leading `:` is a comment (the keepalive), one
    optional space after the field colon is separator and not payload, and `id:`
    is **sticky** — it stays the last event id until another `id:` replaces it,
    which is why it is yielded alongside every frame rather than only the ones
    that carried it.
    """
    data = []
    last_id = None
    for raw in response:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data:
                yield ("event", last_id, "\n".join(data))
                data = []
            continue
        if line.startswith(":"):
            yield ("comment", None, line[1:].lstrip())
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
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

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
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
            "error": None,
        }

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<EventChannel {self._path} {self._health['status']}>"

    # --- lifecycle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "EventChannel":
        """Spawn the channel's own thread. Idempotent."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            # Daemon: a channel wedged in a read must never be the reason a
            # process refuses to exit. Delivery outlives telemetry.
            self._thread = threading.Thread(
                target=self.run, name=self._thread_name, daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the channel to stop, and wait for its thread to end.

        Safe from any thread, safe twice, safe before `start()`. The live
        response is shut down rather than merely closed, so a reader parked in
        a blocking `readline()` returns now instead of when the 120 s read
        timeout says so.
        """
        self._stop.set()
        with self._io_lock:
            live = self._live
            self._live = None
        if live is not None:
            close_stream(live)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def health(self) -> Dict[str, Any]:
        """A snapshot of what the channel is doing. Thread-safe, and free of
        secrets: statuses and reasons, never a bearer."""
        with self._lock:
            return dict(self._health)

    # --- the loop

    def run(self) -> None:
        """Connect, consume, reconnect, until stopped or fatally refused.

        The isolation contract lives in this method's `except`: *nothing* gets
        out. Whatever went wrong — the network, the bearer, a frame, the sink —
        it stays a fact about this channel, recorded in `health()`.
        """
        try:
            self._run()
        except BaseException as exc:  # noqa: BLE001 — the isolation contract
            log.exception("events channel stopped on an unexpected error")
            self._note(status="stopped", error=f"channel error: {exc!r}")

    def _run(self) -> None:
        while not self._stop.is_set():
            outcome = self._connect_once()
            if not outcome.retryable:
                return  # fatal — stopping is the correct loud behaviour
            if self._stop.is_set():
                break
            if outcome.progressed:
                # Progress, not mere reconnection (K): a gateway that accepts a
                # connection and drops it immediately would otherwise pin the
                # ladder at one second and hammer it forever.
                self._backoff.after_success()
            delay = self._backoff.after_error()
            with self._lock:
                self._health["retries"] += 1
            # Waiting on the stop event, not sleeping: a `stop()` during a 60 s
            # backoff returns in microseconds instead of a minute.
            self._stop.wait(delay)
        self._note(status="stopped")

    def _connect_once(self) -> _Outcome:
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
            return self._on_auth_rejected(exc)
        except RelayHTTPError as exc:
            if exc.status == FATAL_STATUS:
                self._note(status="fatal", error=f"HTTP {exc.status}")
                log.error("events channel: HTTP %s — no such route; stopping. "
                          "A rotation cannot conjure an endpoint.", exc.status)
                return _Outcome(False, False)
            # Everything else the broker can answer — 503 "too many event
            # streams", a 5xx, an edge interstitial — is a state that passes.
            self._note(status="reconnecting", error=f"HTTP {exc.status}")
            return _Outcome(True, False)
        except Exception as exc:  # noqa: BLE001 — DNS, TCP, TLS, protocol: one thing
            self._note(status="reconnecting", error=f"connect: {exc}")
            return _Outcome(True, False)

        with self._io_lock:
            if self._stop.is_set():
                # `stop()` landed between the open and here, so it found no
                # live response to wake. Closing it is this branch's job.
                close_stream(response)
                return _Outcome(True, False)
            self._live = response

        self._note(status="connected", error=None)
        progressed = False
        try:
            progressed = self._consume(response)
        except Exception as exc:  # noqa: BLE001 — including a sink that raised
            self._note(status="reconnecting", error=f"stream: {exc}")
        finally:
            with self._io_lock:
                self._live = None
            close_stream(response)

        if self.health()["status"] == "connected":
            # A clean EOF is still a disconnection. Without saying so, the
            # backoff wait happens while the status file still advertises the
            # channel as connected.
            self._note(status="reconnecting", error="stream ended")
        return _Outcome(True, progressed)

    def _on_auth_rejected(self, exc: AuthRejected) -> _Outcome:
        """401/403 — a window or a wall, depending on whether anything can
        change (K)."""
        self._note(status="auth_failed", error=f"HTTP {exc.status}")
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

    def _consume(self, response) -> bool:
        """Read frames until the stream ends. Returns whether the durable
        cursor moved — the only kind of progress the ladder counts."""
        progressed = False
        for kind, frame_id, payload in parse_frames(response):
            if self._stop.is_set():
                break
            if kind == "comment":
                continue  # a keepalive; its only job was to arrive
            cursor = _as_cursor(frame_id)
            try:
                event = json.loads(payload)
            except ValueError:
                self._skip(cursor, "not JSON")
                continue
            if not isinstance(event, dict):
                # Valid JSON, but not an event — `data: []` cannot be committed
                # by any sink, and the ones that tried raised.
                self._skip(cursor, "JSON, but not an event object")
                continue
            if cursor is not None and "cursor" not in event:
                # The broker puts the cursor in the body as well as the `id:`;
                # a deployment that only sets the id still hands the sink a
                # complete event.
                event["cursor"] = cursor
            self._sink.commit(event)  # returning IS the durable point
            if cursor is not None:
                self._resume = cursor if self._resume is None else max(self._resume, cursor)
            progressed = True
            self._note(status="connected", error=None,
                       last_cursor=self._resume, last_event_at=time.time())
        return progressed

    def _skip(self, cursor: Optional[int], why: str) -> None:
        """A frame this channel will not commit — and will not ask for again.

        Advancing the resume cursor past it is the second half of the fix, and
        the half sparrow left out: skipping alone keeps *this* connection alive,
        but the next reconnect resumes from the durable cursor, which is still
        behind the bad frame, so it arrives again. Advancing means one skip per
        process rather than one per reconnect.

        It deliberately does not count as progress: a stream serving nothing but
        poison must not reset the backoff ladder and turn into a hot loop.
        """
        where = f"at cursor {cursor}" if cursor is not None else "with no id"
        log.warning("events channel: skipping the frame %s (%s) — it will not "
                    "be committed, and will not be resumed from", where, why)
        if cursor is not None:
            self._resume = cursor if self._resume is None else max(self._resume, cursor)

    def _resume_cursor(self) -> Optional[int]:
        """Where the next connection asks to start.

        The sink's durable cursor is authoritative — it is the one that survives
        a restart — but this channel's own resume marker can be ahead of it by
        the frames it deliberately skipped, so the later of the two is the
        answer.
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

    def _note(self, **fields) -> None:
        with self._lock:
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
