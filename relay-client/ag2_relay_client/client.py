"""The wire loop: one bearer's whole conversation with the broker.

The seam is two things wide. Tasks come out of an in-memory queue; answers go
back through `complete` / `reject`. Everything else — the long poll, the lease,
the journal, the ack, the results, the heartbeat, the backoff, the auth
recovery, the status file — is on this side of it, and a consumer that never
learns any of the ids below still gets all of them.

**Cadence is a correctness property, not a tuning knob (F1).** The broker
extends a lease only while the worker keeps polling; a worker that stops polling
has its in-flight tasks re-served with `attempt` bumped. So nothing in this loop
may block unboundedly: the long poll has a socket timeout of `wait + 10`, every
side call has its own bounded timeout, the queue the tasks land in is unbounded
so a slow consumer can never stall the poll thread, and the status write, the
hook and the log are all best-effort by contract (D4). A stall here does not
look like a stall — it looks like duplicate delivery.

**The ordering that makes at-least-once safe.** Journal first, then the ack
(F2): an ack that goes out before local durability leaves the broker showing
"received" for work no surviving process knows about. A result is retained until
its POST succeeds (F5) — success is the only thing that retires it, because
`POST /v1/results` is what completes the lease. And every id the broker re-serves
is checked against the journal before anything executes (F3): already answered
means re-complete, never re-execute. The reconnect replays of 2026-06-30 and
2026-07-01 were 500 historical tasks each, and without that check every one of
them ran again.

**The ack is not informational, whatever the protocol doc says.** `WP.md` states
that the ack "never touches the lease". The broker's source says otherwise, and
the source is what runs: liveness alone extends an un-acked lease exactly
`UNACKED_EXTEND_GRACE` times (`taskqueue.py:68` and the extend gate at `:327`),
and only `acknowledged_ts` extends it indefinitely — while `take()` re-leases
with no `acknowledged_ts` carried over (`:499`), so every re-serve starts
un-acked again. An un-acked lease therefore dies about three visibility windows
after it was served, however hard its consumer is still working, and the attempt
cap dead-letters it at five. Two things follow, and both are load-bearing here:
a re-served id the consumer still holds is **re-acked** (never re-delivered),
and a pause on acking is a pause on delivery, so it is said out loud and it is
never allowed to swallow a re-ack.

**Both halves of the media move are below the seam.** Inbound, a marker becomes
a local path before the Task is delivered (below). Outbound, `complete` runs the
answer through `Outbound` — the one marker parser, the egress allowlist built
from `egress_roots`, the uploads — so a consumer hands over the text its agent
wrote and never a path this library has not judged, never a URL, never bytes.
The room ops the ladder needs are on `client.room_ops`, and a Room Op failure
degrades to `/v1/results` rather than reaching the consumer (I1).

**Media is resolved before delivery, off this thread.** A task body can carry an
`[ag2space-media:]` marker, which is a URL and not a file; the stage in
`media.py` turns it into a local path between the poll and the queue, so a
consumer reads `task.attachments` and never a marker. The fetch has its own
thread for one reason: a 25 MiB download on this one is a stalled loop, and a
stalled loop is duplicate delivery.
**One poller per bearer, enforced here (J1).** The wire cannot tell you that a
second client is polling the same queue — it just splits the lease stream and
delivers everything twice. So every turn of this loop starts by asking the
singleton guard in the state dir whether this client is still the poller of
record; a client that loses the guard stops, and a client that cannot read the
guard keeps polling, because a lock bug must never be what silences delivery.

**Never health-probe with `GET /v1/tasks`** — it leases tasks. `healthz()` is
here so the question has an answer that costs nothing.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.parse
from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .backoff import Backoff
from .credentials import TokenSource
from .egress import DEFAULT_MAX_BYTES, EgressAllowlist
from .envelope import Task, parse_task
from .journal import Journal, Reconciler
from .media import MediaIngress, MediaStore
from .outbound import MAX_FILES, Outbound, PreparedResult
from .resolver import BoundedResolver
from .roomops import COOLDOWN_S as ROOM_OPS_COOLDOWN_S
from .roomops import RoomOps
from .singleton import LOST, STALE_AFTER_S, PollerGuard
from .state import StateLayout, valid_wire_id
from .status import (
    AUTH_WAIT,
    CONNECTED,
    DISPLACED,
    FATAL,
    RECONNECTING,
    STANDBY,
    STOPPED,
    StatusReporter,
)
from .transport import AuthRejected, RelayHTTP, RelayHTTPError

log = logging.getLogger(__name__)

#: The long-poll window. The server caps `wait` at 30; 25 leaves room for the
#: round trip so a healthy idle loop turns continuously rather than racing the
#: cap. The socket timeout is this plus `SOCKET_MARGIN_S` — a timeout at or
#: below the window would turn every idle poll into an error.
POLL_WAIT_S = 25
SOCKET_MARGIN_S = 10

#: Best-effort calls: ack, heartbeat, healthz. Short, because none of them is
#: allowed to hold up the poll behind it.
SIDE_TIMEOUT_S = 10.0
#: The result POST. Longer, because it is the one call that matters.
RESULT_TIMEOUT_S = 20.0

#: E1's interval. "At most every 60 s" — the gate is checked once per poll, so
#: the real spacing is the first poll boundary at or past 60 s.
HEARTBEAT_INTERVAL_S = 60.0

#: F4's cooldown. A broker with no `/ack` route at all gets a rest — and then
#: gets asked again, because a permanent latch means a broker that *gains* the
#: endpoint in a deploy is never picked up until the worker restarts.
#:
#: It is a pause on *delivery*, not on bookkeeping — see the module docstring on
#: what an un-acked lease actually costs — so it is written into the status file
#: while it runs, it says what it costs in the log, and a re-serve is re-acked
#: straight through it.
ACK_COOLDOWN_S = 300.0

#: The broker's own number, mirrored here so the log line above can be honest
#: about what the pause costs: `UNACKED_EXTEND_GRACE` in
#: `services/shared/queue/taskqueue.py:68`.
UNACKED_EXTEND_GRACE = 2

#: What one turn may spend POSTing owed results before the rest wait for the
#: next one — and what it may spend on the side calls an intake makes (the
#: per-task acks and the dead-letter rejects).
#:
#: Both exist because the poll iteration is what keeps every in-flight lease
#: alive (F1) and what the singleton guard measures freshness against (J1), and
#: neither the owed-result queue nor a poll answer has any bound on its length:
#: `GET /v1/tasks` drains the broker's whole queue into one answer, and the
#: reconnect replays were 500 tasks each. Unbounded, one pass cost a timeout per
#: item — 10 tasks at the ack's own timeout is 100 s of poll iteration, which is
#: a lost lease per task still in flight.
#:
#: The result budget is deliberately larger than `RESULT_TIMEOUT_S`, so a turn
#: always gets at least one whole attempt at the head of the queue and the drain
#: can never stall on its own bound.
RESULT_DRAIN_BUDGET_S = 30.0
INTAKE_BUDGET_S = 10.0

#: How long a drain waits for the drain lock before leaving it to whoever has
#: it. The consumer thread drains too (`complete()` posts immediately), and this
#: used to be an unbounded wait *inside the poll iteration* — the poll thread
#: parked for the length of the consumer's whole pass, which is F1's failure
#: mode wearing a mutex.
DRAIN_LOCK_WAIT_S = 1.0

#: The longest slice of the between-turns wait the loop takes in one call. The
#: wait is chunked so the singleton guard can be re-stamped through it: a client
#: idling out a 60 s backoff is deliberately quiet, not hung, and waiting it out
#: in one call was the largest single term in J1's freshness arithmetic.
GUARD_TOUCH_SLICE_S = 15.0

#: How long `stop()` waits for the poll thread by default. Short, because this
#: is a daemon thread that leaves at its next check and a consumer calling
#: `stop()` should not be parked for the length of a long poll to find that out.
#: The 40 s it replaces put every consumer on the obvious path behind a 35 s
#: socket timeout.
STOP_JOIN_S = 5.0

#: How many un-sent acks to remember across turns. Bounded because it is a
#: backlog and not a queue of work; ids fall off the old end, and an id whose
#: ack is lost that way is re-acked the next time the broker re-serves it.
ACK_BACKLOG_MAX = 1024

#: How long a client that lost the singleton race waits before asking for the
#: bearer again (J1). It is not backoff — nothing failed — and it is short
#: because the thing it is waiting for is a holder that died without releasing:
#: delivery resumes one staleness window plus one of these after the death.
STANDBY_RECHECK_S = 15.0

#: C2's re-check cadence. Slow on purpose: the client is waiting for a human or
#: a connect flow to rotate a token, and hammering the gateway while it waits is
#: the crash-loop this replaced.
AUTH_RECHECK_S = 30.0

#: The heartbeat's protocol version — the one versioned thing on this wire. The
#: task envelope is frozen additive-only with no version field.
PROTOCOL_VERSION = 1

#: What this client tells the broker it can do. A list, not a bitfield, because
#: the broker's answer to an unknown entry is to ignore it.
CAPABILITIES = ("task-ack", "heartbeat", "result-skip-markers", "reject")

#: The one marker the wire loop itself writes: "recorded, nothing to post". It
#: completes the lease without a user-facing reply, which is what makes
#: re-completing a redelivery possible at all — a skipped `/v1/results` would
#: leave the lease to expire and the task to come back again. The rest of the
#: marker grammar (skip, redirect, dm-only, attach) is read — in the one place
#: it is written down, `markers.py` — by `Outbound`, which `complete` runs.
NO_SEND = "[no-send]"

#: The dead-letter code for a task this client cannot even name.
INVALID_TASK_SCHEMA = "INVALID_TASK_SCHEMA"


class RelayClient:
    """The relay wire, running. Start it, read `tasks`, answer with `complete`.

        client = RelayClient(TokenSource(token_file=...), state_dir="~/.ag2/state")
        client.start()
        task = client.next_task(timeout=30)
        client.complete(task.id, "the answer")

    The queue is a handoff and not a durability boundary: what survives a
    restart is the journal under the state dir, and the consumer's contract is
    that every Task it takes is eventually answered with `complete` or
    `reject`.

    `media_dir` is where inbound attachments are fetched to (the state dir by
    default), and `media_retention_s` opts out of deleting them when the task is
    answered — for a consumer whose own archives point at those paths.

    **`egress_roots` is the whole of this client's outgoing-file policy**, and
    it is fixed here. A body answered through `complete` is read for the marker
    grammar and any file it names is uploaded from an allowlisted path — so the
    directories in this one list are the only places on this machine a file can
    leave from, a consumer that passes none sends nothing, and a reviewer
    asking "what may this program upload?" has exactly one line to read. The
    media directory is deliberately *not* added: a consumer that wants to send
    back what arrived says so with an explicit root.

    **The authenticated session is private and sealed, and the object has no
    `__dict__`.** `RelayHTTP` is a bearer token with a `.post` on it, and a
    public attribute holding one is the raw-wire escape hatch this library
    refuses to have: `client.http.post("/v1/rooms/X/media", {"content_b64": …})`
    is every egress rule in `egress.py` bypassed in one line. Ticket 04 sealed
    the identical hatch on `RoomOps`; this is the same seal, for the same
    reason. A caller with its own transport passes it as `http=` at
    construction, which is the supported and only route.
    """

    #: Slots, so the seal below has no `__dict__` to be walked around.
    __slots__ = (
        "credentials", "layout", "_http", "journal", "status", "backoff",
        "poll_wait", "socket_margin", "heartbeat_interval", "ack_cooldown",
        "auth_recheck_interval", "guard", "standby_recheck", "idle_gap",
        "result_budget", "intake_budget", "tier", "client_name", "provider",
        "capabilities", "tasks", "media", "_room_ops", "outbound",
        "_reconciler", "_live", "_presence",
        "_ack_disabled_until", "_ack_owed", "_intake_deadline",
        "_result_retry_at", "_heartbeat_disabled", "_last_heartbeat",
        "_deferred_auth", "_drain_lock", "_stop", "_thread",
    )

    #: Written once, in `__init__`, and never again. `_room_ops` is sealed for
    #: the reason `RoomOps` seals its own two: it holds the allowlist, and an
    #: allowlist that refuses to be widened is worth nothing if the object
    #: carrying it can be replaced by one built with wider roots.
    #:
    #: `outbound` is sealed for exactly the same reason, one layer out, and was
    #: missed at first (review 2026-08-20): it *holds* the `RoomOps`, so leaving
    #: it writable left the whole seal one assignment wide —
    #: `client.outbound = Outbound(RoomOps(http, EgressAllowlist(["/"])))` and a
    #: `[file:]` marker uploads anything on the machine. A seal with a door in
    #: it is not a seal.
    _SEALED = frozenset({"_http", "_room_ops", "outbound"})

    def __init__(
        self,
        credentials: TokenSource,
        state_dir,
        instance: str = "default",
        media_dir=None,
        media_retention_s: Optional[float] = None,
        egress_roots: Sequence[object] = (),
        egress_max_bytes: int = DEFAULT_MAX_BYTES,
        max_files: int = MAX_FILES,
        room_ops_cooldown: float = ROOM_OPS_COOLDOWN_S,
        http: Optional[RelayHTTP] = None,
        resolver: Optional[BoundedResolver] = None,
        poll_wait: int = POLL_WAIT_S,
        socket_margin: float = SOCKET_MARGIN_S,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_S,
        ack_cooldown: float = ACK_COOLDOWN_S,
        auth_recheck_interval: float = AUTH_RECHECK_S,
        singleton: bool = True,
        singleton_stale_after: float = STALE_AFTER_S,
        standby_recheck: float = STANDBY_RECHECK_S,
        result_budget: float = RESULT_DRAIN_BUDGET_S,
        intake_budget: float = INTAKE_BUDGET_S,
        idle_gap: float = 0.0,
        tier: str = "owner",
        client_name: str = "ag2-relay-client",
        provider: str = "",
        capabilities: Sequence[str] = CAPABILITIES,
    ):
        self.credentials = credentials
        self.layout = StateLayout(state_dir, instance)
        self._http = http or RelayHTTP(credentials, resolver=resolver)
        self.journal = Journal(self.layout.journal_path)
        self.status = StatusReporter(
            self.layout.status_path, gateway=credentials.base_url, instance=instance)
        self.backoff = Backoff()

        self.poll_wait = int(poll_wait)
        self.socket_margin = float(socket_margin)
        self.heartbeat_interval = float(heartbeat_interval)
        self.ack_cooldown = float(ack_cooldown)
        self.auth_recheck_interval = float(auth_recheck_interval)
        #: The J1 guard: exactly one poller per bearer, arbitrated through the
        #: state dir this client was pointed at. It lives here rather than in
        #: the consumer so that every consumer inherits it — and it is on by
        #: default for the same reason. `singleton=False` is for a caller who
        #: has *another* singleton mechanism, and it is a loud choice: nothing
        #: else in this library stops a second poller on the same bearer.
        self.guard: Optional[PollerGuard] = (
            PollerGuard(self.layout.singleton_path,
                        stale_after=singleton_stale_after) if singleton else None)
        self.standby_recheck = float(standby_recheck)
        #: The two wall-clock bounds one turn puts on itself. See the constants.
        self.result_budget = float(result_budget)
        self.intake_budget = float(intake_budget)
        #: What the loop waits after a *healthy* poll. Zero: the long poll is
        #: the pacing. A test with an instant broker sets it to keep the loop
        #: from spinning.
        self.idle_gap = float(idle_gap)
        #: A node self-description for the heartbeat, never an authorization
        #: input — the broker attests each task's sender itself.
        self.tier = tier
        self.client_name = client_name
        #: Which surface this node answers for, said on the heartbeat and
        #: nowhere else. Empty means the field is omitted rather than sent
        #: blank, for the reason presence is omitted: a node that says nothing
        #: must not overwrite what the broker last knew about it.
        self.provider = str(provider or "")
        self.capabilities = tuple(capabilities)

        #: Where Tasks come out. Unbounded on purpose: a bounded queue would
        #: make a slow consumer able to block the poll thread, and a blocked
        #: poll thread is lost leases and duplicate delivery (F1).
        self.tasks: "queue.Queue[Task]" = queue.Queue()

        #: The stage between the poll and that queue: it strips media markers
        #: from every body and, for the few tasks that carry one, fetches the
        #: bytes on its own thread before delivering. Nothing a consumer takes
        #: out of `tasks` has ever carried a marker or a URL. The media
        #: directory is **not** added to any egress allowlist here — a consumer
        #: that wants to re-upload what arrived says so with an explicit root,
        #: so egress policy stays in one visible place.
        self.media = MediaIngress(
            self._http,
            MediaStore(media_dir or self.layout.media_path, media_retention_s),
            deliver=self.tasks.put,
            on_auth_rejected=self._defer_auth,
        )

        #: The outbound half, built here so that a consumer gets it by
        #: consuming the client rather than by remembering to wire it up. The
        #: allowlist is constructed from `egress_roots` and from nothing else —
        #: no environment variable, no default root, no directory this library
        #: chose on the consumer's behalf — because the point of a list fixed
        #: at construction is that it is the only thing a reviewer has to read.
        self._room_ops = RoomOps(
            self._http,
            EgressAllowlist(egress_roots, max_bytes=egress_max_bytes),
            cooldown_s=room_ops_cooldown,
            on_auth_rejected=self._defer_auth,
        )
        #: What `complete` runs the answer through: the marker grammar, the
        #: uploads, the F6 ledger that keeps a retried result POST from putting
        #: the same file in the room twice.
        self.outbound = Outbound(self._room_ops, max_files=max_files)

        self._reconciler = Reconciler(self.journal)
        #: Ids accepted by *this* run. The journal knows which ids are owed an
        #: answer; only this set knows which of them a consumer could still be
        #: working on, which is what keeps E3 from dropping a live one.
        self._live: set = set()
        self._presence: Tuple[Optional[str], Optional[str]] = (None, None)
        #: Monotonic, not wall clock (A8): an NTP step backwards used to extend
        #: this pause by the size of the step, and a pause on acking is a pause
        #: on delivery. `None` is "not paused" rather than `0.0`, because a
        #: monotonic clock's zero point is undefined — on CPython 3.9 for macOS
        #: it is process start, so `0.0` is a *live* deadline for the first
        #: milliseconds of a run.
        self._ack_disabled_until: Optional[float] = None
        #: Acks a turn's budget had no room for, oldest first, mapped to whether
        #: they are urgent — an urgent one is a re-serve, and it goes out even
        #: while acking is otherwise paused. See `_ack_phase`.
        self._ack_owed: "OrderedDict[str, bool]" = OrderedDict()
        self._intake_deadline = 0.0
        #: Per-result retry gates, monotonic. A result that failed waits before
        #: it is tried again, which is what lets the drain move on to the answer
        #: behind it instead of stalling the whole queue on one id.
        self._result_retry_at: Dict[str, Tuple[Backoff, float]] = {}
        self._heartbeat_disabled = False
        #: `None` is "never", for the same reason as above: with a monotonic
        #: clock whose origin is process start, a `0.0` sentinel makes the
        #: interval gate read "sent 0 seconds ago" on the very first turn and
        #: the client never announces itself.
        self._last_heartbeat: Optional[float] = None
        self._deferred_auth: Optional[AuthRejected] = None
        self._drain_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<RelayClient {self.layout.instance} {self._http.base_url}>"

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._SEALED and hasattr(self, name):
            raise AttributeError(
                f"{name} is fixed at construction; build another RelayClient "
                f"rather than repointing this one")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if name in self._SEALED:
            raise AttributeError(f"{name} is fixed at construction")
        object.__delattr__(self, name)

    @property
    def room_ops(self) -> RoomOps:
        """Speaking in a room as this identity — post, edit, react, upload.

        Read-only, and safe to hand around: `RoomOps` seals its own transport
        and its own allowlist, so what a caller can reach through this is four
        ops and no bearer. It is *not* a second way out for a file — `upload`
        goes through the same allowlist `complete` does, built from the same
        `egress_roots`.

        Consumers need it for the placeholder→edit ladder: post a message, keep
        the event id, edit it as the work proceeds, and then complete the lease
        with `[REPLIED]`.
        """
        return self._room_ops

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> "RelayClient":
        """Open the state dir, replay what a previous run left, and poll.

        Startable again after the loop has stopped by itself, which it does on
        a displacement and on an unrecoverable auth rejection. It used not to
        be: `_thread` was left pointing at a thread that had already exited, so
        the documented way back — "coming back is the consumer's decision" —
        raised `RuntimeError: this client is already started`, and only
        `stop()` cleared it, which neither the README nor `PollerGuard` says
        you have to call first.
        """
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("this client is already started")
        self._thread = None
        if self.guard is not None and self.guard.displaced:
            # The loop never re-acquires a guard it was displaced from — that
            # is the reaped-process incident with extra steps, plus flapping.
            # `start()` is not the loop: it is the consumer deciding. Releasing
            # here clears nothing on disk (the record names the poller that
            # took it) and only lets this client re-enter the arbitration
            # honestly — it will stand by behind a live holder, not poll
            # alongside one.
            log.warning("this client was displaced from the bearer's guard; "
                        "start() re-enters the arbitration as a standby (J1)")
            self.guard.release()
        self.prepare()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"ag2-relay-{self.layout.instance}", daemon=True)
        self._thread.start()
        return self

    def prepare(self) -> None:
        """Everything `start` does except starting the thread.

        Separate because a caller driving `poll_once` itself — a test, or a
        consumer with its own scheduler — needs the same state loaded, and
        because the log line below is the trailhead after an incident: a
        client that writes its status somewhere nobody thinks to look has, in
        practice, written nothing (D5).
        """
        self.layout.ensure()
        self.journal.load()
        # Before the first poll: the media directory exists, and whatever an
        # earlier run left in it is swept. Those files belonged to Tasks that
        # were sitting in an in-memory queue when their process ended, so no
        # live task can ever claim them (the age-based opt-out sweeps only what
        # has aged out — a consumer whose archives point at those paths said so
        # at construction).
        self.media.start()
        if self.guard is None:
            log.warning("this client runs with no singleton guard — nothing "
                        "here prevents a second poller on the same bearer, and "
                        "the wire does not detect one (J1)")
        log.info("relay client %r on %s — connection status in %s",
                 self.layout.instance, self.status.snapshot().get("gateway"),
                 self.layout.status_path)
        owed = self.journal.pending_results()
        if owed:
            log.info("%d result(s) from an earlier run are still owed to the "
                     "broker; they are re-POSTed before the first poll", len(owed))
        stale = self.journal.accepted_ids()
        if stale:
            log.info("%d id(s) were accepted by an earlier run and never "
                     "answered; the broker will re-serve them (F3 decides "
                     "whether they re-execute)", len(stale))
        # One status write before the first turn, so there is never a window
        # where the only thing on disk is the constructor's default with no
        # guard verdict in it (D2). A first turn can legitimately last most of a
        # minute — a long poll on top of a drain — and a supervisor reading a
        # bare `reconnecting` in that window cannot tell "starting" from
        # "wedged", which is exactly the 2026-07-25 shape.
        self._update_status(RECONNECTING, error=None, backoff_s=0.0)

    def stop(self, timeout: float = STOP_JOIN_S) -> None:
        """Stop polling. Anything already answered stays on disk to be re-POSTed.

        The default join is short on purpose: this is a daemon poll thread that
        leaves at its next check, and a consumer should not be parked for the
        length of a long poll to learn that.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        left = True
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            left = not thread.is_alive()
        # After the poll thread, so nothing is still being handed to it. A
        # download in flight is not waited out — `stop()` returning promptly
        # matters more, and a late fetch can only put a Task on a queue nobody
        # is reading and leave a file for the next run's sweep.
        self.media.stop()
        if self.guard is not None:
            if left:
                # After the join, not before: a guard released while this loop
                # is still turning is a bearer two clients may poll at once.
                # Released at all — rather than left to go stale — because a
                # restart that has to wait out a freshness window it does not
                # need is minutes of a user's messages going nowhere.
                self.guard.release()
            else:
                # The invariant above only holds if the join actually joined,
                # and it did not: the loop is still inside a bounded call. A
                # release here empties the record while this client may yet
                # poll once more, and another client then polls alongside it —
                # precisely the thing the ordering exists to prevent. Left
                # held, it goes stale by itself.
                log.warning(
                    "the poll thread had not stopped %ss after stop() — leaving "
                    "the singleton guard held rather than releasing a bearer "
                    "this client may still poll. A replacement waits out the "
                    "%.0fs freshness window instead (J1).",
                    timeout, self.guard.stale_after)
        self._update_status(STOPPED)

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    delay = self.poll_once()
                except Exception:  # noqa: BLE001 — D1: an unexpected error
                    # costs a delay, never the bearer's only poller.
                    # `poll_once` already catches broadly; this is the second
                    # belt, for a bug in the backoff or the status write itself.
                    log.exception("unexpected error in the poll loop")
                    delay = self.backoff.after_error()
                self._wait(delay)
        finally:
            # A displacement and an unrecoverable auth rejection both stop the
            # loop from in here, and a `_thread` left pointing at a dead thread
            # made `start()` raise "already started". Cleared by whoever is
            # actually leaving, and only if `stop()` has not already replaced
            # it with a newer one.
            if self._thread is threading.current_thread():
                self._thread = None

    def _wait(self, delay: float) -> None:
        """Wait between two turns without letting the guard go stale (J1).

        In slices, because the longest of these is the 60 s backoff cap and a
        client idling out a backoff is deliberately quiet, not hung: it is
        still the poller of record and it is still the one that will answer.
        Waiting it out in a single call was the largest single term in the
        freshness arithmetic and it bought nothing — a loop that is genuinely
        hung never reaches this method at all.
        """
        while delay > 0 and not self._stop.is_set():
            slice_s = min(delay, GUARD_TOUCH_SLICE_S)
            self._stop.wait(slice_s)
            delay -= slice_s
            self._keep_guard_fresh()

    # --- the loop ----------------------------------------------------------

    def poll_once(self) -> float:
        """One turn of the loop. Returns how long to wait before the next one.

        Every outcome — healthy, failed, auth-rejected — writes status before
        returning (D2). The 2026-07-25 wedge was invisible because the stall
        happened with no status write at all.
        """
        not_ours = self._guard_turn()
        if not_ours is not None:
            # Standing by, or displaced. Either way nothing reaches the wire on
            # this turn: the bearer is somebody else's to poll (J1).
            return not_ours
        try:
            self._before_the_wire()
            self._raise_deferred_auth()
            answer = self._http.get(
                "/v1/tasks", params={"wait": self.poll_wait},
                timeout=self.poll_wait + self.socket_margin)
            self._keep_guard_fresh()
            self._intake_all(answer)
            self._keep_guard_fresh()
            if self.journal.pending_results():
                # Either a redelivery was just re-completed above, or the
                # consumer answered while the poll was in flight. Either way it
                # goes now rather than one long-poll window later: the thing at
                # stake is a lease, and a lease is on a clock.
                self._drain_results()
                self._keep_guard_fresh()
                self._raise_deferred_auth()
        except AuthRejected as exc:
            return self._on_auth_rejected(exc)
        except Exception as exc:  # noqa: BLE001 — D1
            delay = self.backoff.after_error()
            log.warning("poll failed (%s) — retrying in %ss",
                        _describe(exc), delay)
            self._update_status(RECONNECTING, error=_describe(exc), backoff_s=delay)
            return delay

        self.backoff.after_success()
        self._update_status(CONNECTED)
        return self.idle_gap

    def _before_the_wire(self) -> None:
        """The two side channels that run ahead of `GET /v1/tasks` — and D4.

        D4 is verbatim about this spot: "any code that runs inside the poll
        iteration before the `GET /v1/tasks` MUST NOT be able to raise". Both
        steps here reach `journal._save`, which can raise on a full disk, a
        read-only mount or a state dir that went away under a running client.
        Reproduced: a result POST that *succeeded* and a retire that did not,
        so every later pass re-POSTed and re-raised at the same line — the loop
        backed off to its 60 s cap and never polled again, every in-flight lease
        expired unextended, and the status file still said `reconnecting`. A
        side channel had become the delivery blocker, which is the whole of what
        D4 forbids.

        A rejected bearer is the one thing that does not stop here: it is
        deferred by the steps themselves and raised by the caller (C8).
        """
        for step in (self._drain_results, self._heartbeat):
            try:
                step()
            except Exception:  # noqa: BLE001 — D4, see above
                log.exception("a side channel before the poll raised; the poll "
                              "goes ahead anyway — nothing in here is allowed "
                              "to be what stops delivery (D4)")
            self._keep_guard_fresh()

    def _keep_guard_fresh(self) -> None:
        """Say "the loop is still turning", between two of its bounded calls.

        The guard's liveness test is the freshness of a stamp, and the stamp
        used to happen once per turn — which quietly made the freshness window
        a bound on the *whole* turn, the sum of every call in it. It was never
        that: measured, a holder with one owed result and its backoff at the cap
        took 125 s against a 120 s window, and one with no failure at all took
        120.9 s, and in both cases a standby took the bearer from a client whose
        thread was alive throughout — which then went DISPLACED permanently.

        So the stamp happens wherever the loop demonstrably got to. This is not
        a second liveness channel and must never become one: there is no timer
        and no thread behind it, so a loop hung inside any one call reaches no
        stamp and ages out exactly as J1 wants. Only a holder stamps — a
        standby touching the file here would take the bearer between two turns
        without ever writing the status that says it did.
        """
        if self.guard is not None and self.guard.held:
            self.guard.touch()

    # --- the singleton guard (J1) -----------------------------------------

    def _guard_turn(self) -> Optional[float]:
        """May this client poll at all? `None` means yes.

        Asked once per turn, before anything reaches the wire, because the
        thing being prevented is *this* turn leasing tasks a second poller is
        also leasing. Two clients on one bearer split the lease stream and
        double-deliver every task, and the broker neither detects nor rejects
        the second one.

        Three answers, and the third is the one that matters:

        - **held** — poll.
        - **lost** — do not. A client that never held the guard stands by and
          keeps asking, so that a holder which died without releasing is taken
          over from with no operator in the loop. A client that *held* it and
          was displaced stops for good: the incident is a reaped process and
          its replacement both pulling, and a loop that re-acquired by itself
          would be that incident with extra steps.
        - **degraded** — the guard could not be evaluated, so poll. A lock bug
          must never silence task delivery: the worst case on this side is the
          dual poller the guard was already there to catch, and the worst case
          on the other side is a user whose agent has simply gone quiet.
        """
        if self.guard is None:
            return None
        was_standing_by = self.guard.state == LOST and not self.guard.displaced
        if self.guard.claim() != LOST:
            if was_standing_by:
                self._took_the_bearer_over()
            return None
        if self.guard.displaced:
            log.error("another poller took this bearer's guard — polling stops "
                      "now. Whatever this client had in flight will be "
                      "re-served to the holder; answering alongside it is the "
                      "double delivery the guard exists to prevent (J1)")
            self._update_status(
                DISPLACED,
                error="another poller holds this bearer; this client stopped")
            self._stop.set()
            return 0.0
        self._update_status(
            STANDBY,
            error="another poller holds this bearer's guard",
            # Not `backoff_s`: nothing failed, and a supervisor reading a
            # non-zero backoff cannot tell a client that is retrying from one
            # that is deliberately waiting its turn. The constant's own comment
            # said "it is not backoff" and then it was written into the backoff
            # field anyway.
            backoff_s=0.0,
            recheck_s=self.standby_recheck)
        return self.standby_recheck

    def _took_the_bearer_over(self) -> None:
        """A standby just became the holder: re-read the journal (J1, F3).

        The journal is read once, in `prepare()`, and a standby can stand by
        for hours. Everything the previous holder answered in that time is on
        disk and absent from this client's memory, and nothing on the
        STANDBY→HELD edge used to reload it. Reproduced: the holder answered
        `task-1` and retired it, the standby (booted earlier, journal empty)
        took the stale guard, the broker redelivered `task-1` — and the standby
        handed it to its consumer a second time, because `is_done` asked a
        memory that had never seen it. Then its first `journal.accept()` rewrote
        the whole file from that stale memory and erased the `done` entry. The
        longer it had stood by, the more history it reverted.

        Best-effort by construction: this runs before the poll, and D4 says
        nothing in there may be what stops delivery. A reload that fails leaves
        this client with the dedup memory it had, which is the pre-existing
        situation and not a worse one.
        """
        try:
            self.journal.load()
        except Exception:  # noqa: BLE001 — D4
            log.exception("could not re-read the journal on taking this bearer "
                          "over; the dedup memory may be behind the file until "
                          "the next restart (F3)")
            return
        log.info("took the bearer over from another poller — the journal was "
                 "re-read: %d id(s) still owe an answer, %d already answered "
                 "and remembered for dedup (J1, F3)",
                 self.journal.inflight(), len(self.journal.done_ids()))

    def _intake_all(self, answer: Any) -> int:
        """Every task in one poll answer, accepted or explained."""
        served = answer.get("tasks") if isinstance(answer, Mapping) else None
        if not isinstance(served, list):
            # An answer whose shape this client does not understand is no work
            # (G3). It is not an error: the envelope is additive-only, and a
            # shape change is the broker's to announce, not this loop's to
            # crash on.
            if served is not None:
                log.warning("poll answered with a `tasks` field that is not a list")
            served = []
        # `GET /v1/tasks` has no bound on the batch: the broker drains its whole
        # queue into one answer, and the reconnect replays were 500 tasks each.
        # Every side call the intake makes is a blocking POST, so they share one
        # bounded slice of this turn and the rest of the batch is journalled and
        # delivered without waiting for them.
        self._intake_deadline = time.monotonic() + self.intake_budget
        accepted = 0
        for raw in served:
            try:
                accepted += self._intake(raw)
            except AuthRejected:
                raise
            except Exception:  # noqa: BLE001 — one task, not the batch
                # A task that could not be written down is a task this client
                # will not deliver: the broker re-serves it and F3 absorbs the
                # duplicate. Losing the rest of a batch the broker has already
                # leased to us would not be absorbed by anything.
                log.exception("a leased task could not be taken in; the broker "
                              "will re-serve it. The rest of the batch is "
                              "unaffected")
        # After the whole batch, and only then: the acks are the part that can
        # cost seconds each, and F2 says the ack must not gate the handoff.
        self._ack_phase()
        # Raised after the whole batch, never in the middle of it: a rejected
        # bearer seen by an ack must reach recovery (C8), but not by dropping
        # tasks the broker has already leased to us.
        self._raise_deferred_auth()
        return accepted

    def _intake(self, raw: Any) -> int:
        task = parse_task(raw) if isinstance(raw, Mapping) else None
        if task is None:
            self._dead_letter_unusable(raw)
            return 0

        wire_id = task.id
        # I2: the broker put 🫡 on this message when the task was acked, so
        # nothing here may react to it again. Registered before any of the
        # journal checks below and not after them, because a *re-serve* names
        # the same event and reacting to it twice is the same doubled emoji —
        # and because the whole guard is a bounded dict write that cannot fail.
        self._room_ops.note_intake_event(task.source_message_id)
        if self.journal.is_done(wire_id):
            # F3: the answer already landed. Re-complete the lease upstream
            # with a skip marker — the broker dedups the result and delivers
            # nothing — and never hand the task to the consumer again. Silently
            # dropping it instead would leave the lease to expire and the task
            # to come back for a third time.
            self.journal.record_result(
                wire_id, {"id": wire_id, "body": NO_SEND, "no_send": True})
            log.info("task %s was already answered — re-completing the lease, "
                     "not re-executing (attempt %s)", wire_id, task.attempt)
            return 0
        # A re-serve of an id this client still owes an answer for. F3 says do
        # not hand it over again — and the ack, which is the part that was
        # missing entirely, has to go again.
        #
        # `WORKER-PROTOCOL.md` says the ack "never touches the lease". The
        # broker's source says otherwise, and the source is what runs: `take()`
        # re-leases with no `acknowledged_ts` carried over
        # (`taskqueue.py:499`), so every re-serve starts un-acked, and an
        # un-acked lease is extended on liveness alone at most
        # `UNACKED_EXTEND_GRACE` times (`:68`, gate at `:327`) before it is
        # requeued with `attempt` bumped. So a ten-minute Turn burned all five
        # attempts and was dead-lettered around minute fifteen while its
        # consumer was still typing — and the eventual `complete()` then POSTed
        # into a broker with no lease and no room for it, which answers 200 and
        # counts a `results_undelivered`. This client retired the id as
        # delivered. The user's answer was gone, with no error at either end.
        #
        # The re-ack is marked urgent, which is what carries it through F4's
        # cooldown: a re-serve is proof the lease is real and about to die
        # without it, and it happens at most once per visibility window per
        # task, so it can never become the hammering the cooldown is for.
        if self.journal.is_pending(wire_id):
            self._owe_ack(wire_id, urgent=True)
            log.info("task %s already has an answer waiting to be POSTed — the "
                     "re-serve is re-acked so the new lease survives, and the "
                     "drain below completes it", wire_id)
            return 0
        if wire_id in self._live:
            log.info("task %s is queued or in the consumer's hands — the "
                     "re-serve is re-acked so its new lease is not requeued "
                     "out from under a running Turn, and nothing is handed "
                     "over twice (F3, attempt %s)", wire_id, task.attempt)
            self._owe_ack(wire_id, urgent=True)
            return 0

        # Durable first, then the handoff, and the ack after both (F2). The
        # room sidecar is captured here because this is the only moment it is
        # known and a media answer produced after a restart still has to find
        # its room (F7).
        self.journal.accept(wire_id, room=task.room_id)
        self._live.add(wire_id)
        if task.metadata_stripped:
            log.info("stripped unsigned room-ops metadata from task %s", wire_id)
        # Delivery goes through the media stage, which strips any
        # `[ag2space-media:]` marker here and now — a regex, on this thread —
        # and hands the task to its own thread only when there are bytes to
        # fetch. Nothing that reaches `self.tasks` has ever carried a marker,
        # and nothing on this thread ever waits for a download (F1).
        self.media.accept(task)
        self._owe_ack(wire_id)
        return 1

    def _dead_letter_unusable(self, raw: Any) -> None:
        """A task this client cannot name: reject it, do not drop it.

        Dropping is what sparrow does, and the protocol is explicit that a skip
        "just re-serves it until the attempt cap trips" — five deliveries and a
        dead-letter anyway, minutes later. The reject is best-effort and is
        never retained: retention needs a journal entry, and a journal entry
        needs an id this client is willing to write to disk, which is exactly
        what it does not have here (F8).
        """
        wire_id = raw.get("id") if isinstance(raw, Mapping) else None
        log.error("refusing a task with an unusable id (%.80r) — rejecting it to "
                  "the broker's dead-letter park rather than letting it re-serve",
                  wire_id)
        if not isinstance(wire_id, str) or not 0 < len(wire_id) <= 256:
            return
        if not self._intake_budget_left():
            # The batch has used its slice of this turn. This reject is the
            # best-effort half of an already best-effort path: skipping it costs
            # a re-serve of a task nobody can act on, and taking it costs the
            # poll iteration a timeout that every live lease pays for (F1).
            log.warning("no room left in this turn's intake budget for the "
                        "dead-letter reject of %.80r; the broker re-serves it "
                        "until its attempt cap", wire_id)
            return
        try:
            self._http.post(
                "/v1/results",
                {"id": wire_id, "status": "rejected", "error_code": INVALID_TASK_SCHEMA},
                # A side call's timeout, not a result's: this is not a user's
                # answer, and it used to be able to hold the poll for 20 s per
                # malformed task in a batch with no bound on its length.
                timeout=SIDE_TIMEOUT_S)
        except AuthRejected as exc:
            self._defer_auth(exc)
        except Exception as exc:  # noqa: BLE001 — best-effort by construction
            log.warning("dead-letter reject failed (%s); the broker will "
                        "re-serve until its attempt cap", _describe(exc))

    # --- ack (F2, F4) ------------------------------------------------------

    def _intake_budget_left(self) -> bool:
        """Has this turn's slice for intake side calls run out? (F1)"""
        return time.monotonic() < self._intake_deadline

    def _owe_ack(self, wire_id: str, urgent: bool = False) -> None:
        """Remember that an id needs acking. Sent by `_ack_phase`, not here."""
        self._ack_owed[wire_id] = urgent or self._ack_owed.get(wire_id, False)
        self._ack_owed.move_to_end(wire_id)
        while len(self._ack_owed) > ACK_BACKLOG_MAX:
            self._ack_owed.popitem(last=False)

    def _acks_paused(self) -> bool:
        return (self._ack_disabled_until is not None
                and time.monotonic() < self._ack_disabled_until)

    def _ack_phase(self) -> None:
        """Ack what this batch journalled, oldest first, inside one slice.

        Off the delivery path on purpose. The ack used to be a blocking POST
        between `journal.accept` and the handoff, once per task, on a batch with
        no bound on its length. Measured at an ordinary 300 ms round trip that
        is 3.1 s of pure delivery latency for ten tasks; at the ack's own
        timeout a ten-task batch is 100 s of poll iteration, and the poll
        iteration is what keeps every *other* lease alive (F1). F2 says the ack
        must not gate anything, and there it gated both delivery and cadence.

        So the tasks reach the consumer first and the acks follow, for as long
        as this turn can spare. What does not fit is owed to the next turn
        rather than dropped, because "we will ack it eventually" is a real
        deadline: an un-acked lease is extended on liveness alone twice and
        then requeued (see the module docstring).

        A cooldown (F4) holds back the ordinary acks and leaves them owed — they
        go out when the route comes back. It never holds back an urgent one: a
        re-serve is a live lease about to be requeued, and it doubles as the
        probe that notices the route is back.
        """
        for wire_id, urgent in list(self._ack_owed.items()):
            if not self._intake_budget_left():
                log.info("this turn's intake budget is spent with %d ack(s) "
                         "still owed; they go out on the next turn",
                         len(self._ack_owed))
                break
            if not urgent and self._acks_paused():
                continue  # still owed; the pause is time-gated, not a latch
            self._ack_owed.pop(wire_id, None)
            if not self.journal.knows(wire_id) or self.journal.is_done(wire_id):
                continue  # answered and retired while the ack waited its turn
            self._ack(wire_id, urgent=urgent)

    def _ack(self, wire_id: str, urgent: bool = False) -> bool:
        """Tell the broker the task was received.

        Allowed to fail, and never a gate on delivery (F2) — but not
        informational, whatever `WORKER-PROTOCOL.md` says: against this broker
        the ack is what buys a long Turn its lease. See the module docstring.

        The 404 is the interesting part. It means two different things and only
        the body tells them apart: `not leased to you` is a routine per-task
        negative under lease churn, and treating it as "this broker has no ack
        route" let one stale lease blind the whole host's `received` state. A
        bare no-route 404/405 really is an unsupported endpoint — and that gets
        a cooldown rather than a latch, so a deploy that adds the route is
        picked up without a restart (F4).
        """
        now = time.monotonic()
        if self._acks_paused() and not urgent:
            return False
        safe_id = urllib.parse.quote(wire_id, safe="")
        try:
            self._http.post(f"/v1/tasks/{safe_id}/ack", {"id": wire_id},
                            timeout=SIDE_TIMEOUT_S)
        except AuthRejected as exc:
            self._defer_auth(exc)  # C8
            return False
        except RelayHTTPError as exc:
            if exc.status in (404, 405):
                if exc.status == 404 and _says_not_leased(exc.body):
                    log.info("ack for %s: the lease is gone (re-served or not "
                             "ours) — every other task keeps acking", wire_id)
                    return False
                self._pause_acking(now, exc.status)
                return False
            log.info("ack for %s failed: HTTP %s — the broker may redeliver",
                     wire_id, exc.status)
            return False
        except Exception as exc:  # noqa: BLE001 — ack is never a gate
            log.info("ack for %s failed (%s) — the broker may redeliver",
                     wire_id, _describe(exc))
            return False
        # A success re-enables acking: the cooldown is a pause, not a state.
        self._ack_disabled_until = None
        return True

    def _pause_acking(self, now: float, status: int) -> None:
        """F4's cooldown — said out loud, because of what it costs.

        Implemented as specified and correct as far as it goes: a bare 404/405
        is a route that is not there, and hammering it once per accepted task
        helps nobody. What was missing is that against this broker the pause is
        a pause on *delivery*. Everything accepted inside it goes un-acked, and
        an un-acked lease is extended on liveness alone twice and then requeued
        with `attempt` bumped, so a busy window ends in re-serves and eventually
        in dead-letters for work that was never in trouble. It cannot be
        allowed to be silent, and it is not allowed to swallow a re-ack.
        """
        self._ack_disabled_until = now + self.ack_cooldown
        log.warning(
            "this broker has no task-ack route (HTTP %s) — acking pauses for "
            "%ss, then asks again. That pause is not free: an un-acked lease is "
            "extended on liveness alone only %d times before the broker requeues "
            "it with `attempt` bumped, so tasks accepted in this window may be "
            "re-served under a running Turn. Re-serves are re-acked straight "
            "through the pause, and `acks_paused_s` in the status file says how "
            "much of it is left (F4).",
            status, self.ack_cooldown, UNACKED_EXTEND_GRACE)

    # --- results (F5) ------------------------------------------------------

    def complete(self, broker_id: str, body: str,
                 base_dir: Optional[object] = None) -> PreparedResult:
        """Answer a task. The answer is durable before this returns.

        Delivery is this library's problem from here: the result is retained
        until `POST /v1/results` succeeds, across retries and across a restart,
        because that POST is what completes the lease. Dropping it on a
        transient failure loses the user's answer *and* re-serves the task.

        An empty body is refused rather than sent (H5). A blank answer is "not
        ready", not "an empty answer" — a deliberate silence is `[no-send]`,
        which completes the lease and posts nothing.

        **The body is read for the marker grammar before it goes anywhere**
        (H2, through `Outbound`), and that is not a formatting pass: `[file:]`,
        `[send:]` and `[attach:]` are the entrance to egress, so a file the
        body names is uploaded from an allowlisted path *here*, in this
        process, before the POST — and one that may not leave is refused with a
        sentence appended to the answer, so the room learns it did not arrive.
        A consumer that parsed these markers itself would be a second parser of
        a grammar whose every copy has drifted, and a consumer that could hand
        over bytes would make the allowlist decorative. Neither surface exists.

        `base_dir` is what a *relative* path in a marker is read against — the
        consumer's notion of "where this turn ran". It widens nothing: the
        destination is still judged against `egress_roots`.

        Returns what was prepared — the body actually POSTed, what was
        uploaded, what was refused — so a consumer can log or display it.
        Nothing in it has to be read: the answer is on its way either way.
        """
        wire_id = self._require_wire_id(broker_id)
        if not isinstance(body, str):
            raise TypeError(f"a result body is text; got {type(body).__name__}")
        if not body.strip():
            raise ValueError(
                f"refusing an empty result for {wire_id}: a blank answer is "
                f"'not ready', not an answer. To complete the lease with no "
                f"user-facing reply, send {NO_SEND!r}")
        # The room the task came from, out of the journal's F7 sidecar. It is
        # captured at accept because that is the only moment it is known, and
        # media goes to a room-scoped route — so an answer produced after a
        # restart still has somewhere to put its file.
        prepared = self.outbound.prepare(
            wire_id, self.journal.room_for(wire_id), body, base_dir=base_dir)
        # A body that was *only* markers strips to nothing, and posting nothing
        # is H5's failure at one remove: an empty message in the room. The lease
        # still has to be completed, so it is completed the way a deliberate
        # silence is — recorded, delivered to nobody.
        payload: Dict[str, Any] = {"id": wire_id, "body": prepared.body or NO_SEND}
        if prepared.skip or not prepared.body:
            # The flag *and* the marker in the body. Two clients have been
            # completing skipped leases against this broker for a year: sutando
            # sends `no_send: true` and trusts the flag, this one sent the
            # marker and trusted the broker to read it, and neither belief has
            # ever been tested against the other. Sending both is the only
            # variant that is correct whichever one the broker actually honors,
            # and it costs a boolean.
            payload["no_send"] = True
        self._record_result(wire_id, payload)
        return prepared

    def reject(self, broker_id: str, reason: str = "INVALID_TASK") -> None:
        """Dead-letter a task: terminal, never re-served, nothing posted.

        This is the documented answer to a permanently malformed task, and the
        reason it is worth having is that the alternative — staying silent — is
        not "nothing happens" but five re-serves and a dead-letter anyway.

        The worker does *not* post its own give-up message alongside a reject:
        the broker owns the terminal-failure notice, and a second one doubles
        it in the room.
        """
        wire_id = self._require_wire_id(broker_id)
        self._record_result(wire_id, {
            "id": wire_id, "status": "rejected", "error_code": _error_code(reason)})

    def _record_result(self, wire_id: str, payload: Dict[str, Any]) -> None:
        if not self.journal.knows(wire_id):
            # Not refused: the consumer may legitimately answer under an id
            # this run never accepted (an alias for a re-ask, a result carried
            # across a restart). Said out loud, because the other way to reach
            # this line is answering an id the broker is not waiting on.
            log.warning("answering %s, which this run never accepted — the "
                        "broker will ignore it if it is not waiting on it", wire_id)
        self.journal.record_result(wire_id, payload)
        self._live.discard(wire_id)
        # The consumer is done with this Task, so its fetched attachments are
        # done too — under the default retention they are deleted now, and the
        # answer is on disk before they go. A consumer that opted out keeps
        # them, which is what "my archives point at those paths" means.
        self.media.release(wire_id)
        self._drain_results()

    def _drain_results(self) -> int:
        """POST what is owed, oldest first, bounded, and never head-of-line.

        Two rules, and each one was a way this method lost answers.

        **A failure belongs to its result, not to the queue.** It used to
        `break` on the first one, and `pending_results()` is oldest-first, so a
        result the broker would not take sat at the head and every answer behind
        it was durable, owed, and never sent — unreachable by `Reconciler`,
        which only inspects `accepted_ids()`, and never re-offered, because
        `is_pending` makes a redelivery a no-op. `inflight` then only grew, with
        no path back down: E3's scar arriving through the front door. The
        trigger is not exotic — `add_result` does the room's outbound send
        inside the request, so one reply large enough to outlast
        `RESULT_TIMEOUT_S` blocks the queue permanently. So a failure now costs
        *that* id a backoff and costs the answers behind it nothing.

        **The pass is bounded.** Continuing rather than breaking means a broker
        that is simply down would otherwise cost one timeout per owed result,
        inside the poll iteration — and the poll iteration is what keeps every
        in-flight lease alive (F1) and what the singleton guard measures
        freshness against (J1). So it gets a slice of the turn and the rest wait
        for the next one.

        Never raises (D4), including on the journal write that retires an id.
        """
        if not self._drain_lock.acquire(timeout=DRAIN_LOCK_WAIT_S):
            # The consumer thread is inside its own drain, POSTing this very
            # queue. Waiting for it here would add its whole pass to this turn,
            # and F1 pays for that in lost leases; the answers are durable and
            # this pass has nothing to add.
            log.info("another thread is draining results — this pass leaves it "
                     "to them rather than holding the poll behind a lock")
            return 0
        try:
            return self._drain_locked()
        finally:
            self._drain_lock.release()

    def _drain_locked(self) -> int:
        deadline = time.monotonic() + self.result_budget
        owed = self.journal.pending_results()
        self._forget_stale_result_gates({wire_id for wire_id, _ in owed})
        delivered = 0
        for index, (wire_id, payload) in enumerate(owed):
            if time.monotonic() >= deadline:
                log.info("the result drain used its %.0fs of this turn; %d "
                         "answer(s) go on the next pass — they are on disk and "
                         "nothing is lost (F5)", self.result_budget,
                         len(owed) - index)
                break
            gate = self._result_retry_at.get(wire_id)
            if gate is not None and time.monotonic() < gate[1]:
                continue  # this one is waiting out its own failure
            try:
                answer = self._http.post("/v1/results", payload,
                                         timeout=RESULT_TIMEOUT_S)
            except AuthRejected as exc:
                # The one broker-wide verdict there is: this bearer is rejected
                # for every id, so trying the next one is a wasted round trip.
                self._defer_auth(exc)  # C8
                break
            except Exception as exc:  # noqa: BLE001 — F5: keep and retry
                self._retry_result_later(wire_id, _describe(exc))
                continue
            if isinstance(answer, Mapping) and answer.get("ok") is False:
                self._retry_result_later(wire_id, "the broker refused it")
                continue
            duplicate = isinstance(answer, Mapping) and answer.get("duplicate")
            self.journal.retire(wire_id)
            self._live.discard(wire_id)
            self._result_retry_at.pop(wire_id, None)
            # F6: only a POST that succeeded may retire what this id uploaded.
            # Forgetting any earlier is the bug — the retry would re-derive the
            # same body, miss the ledger, and put the same chart in the room
            # again.
            self.outbound.forget(wire_id)
            delivered += 1
            log.info("result delivered for %s%s", wire_id,
                     " (the broker had it already)" if duplicate else "")
        return delivered

    def _retry_result_later(self, wire_id: str, why: str) -> None:
        """Hold one result back for a while. The rest of the queue goes now."""
        ladder = (self._result_retry_at.get(wire_id) or (Backoff(), 0.0))[0]
        delay = ladder.after_error()
        self._result_retry_at[wire_id] = (ladder, time.monotonic() + delay)
        log.warning("result POST for %s failed (%s) — the answer is retained "
                    "and retried in %ss, and every answer behind it goes now. "
                    "Nothing is lost (F5).", wire_id, why, delay)

    def _forget_stale_result_gates(self, owed_ids: set) -> None:
        for wire_id in [k for k in self._result_retry_at if k not in owed_ids]:
            self._result_retry_at.pop(wire_id, None)

    # --- heartbeat (E1, E2, E3) --------------------------------------------

    def heartbeat(self) -> bool:
        """Send a heartbeat now, if the broker still has the endpoint.

        Refused while another poller holds this bearer's guard (J1). Of the four
        calls a consumer can make straight onto the wire, this is the one that
        has to be: a heartbeat is this client announcing itself as the bearer's
        live worker, carrying an `inflight` the broker's presence sweep
        schedules against, and two clients announcing that about one bearer is
        the split brain the guard exists to prevent.

        The other three stay open, deliberately. `complete` and `reject` deliver
        an answer for a lease this client genuinely holds, and F5 plus J1's
        fail-open rule agree that losing a user's answer to a lock is the worse
        outcome — a standby posting a result it owes is the *right* thing.
        `healthz` leases nothing, claims nothing and marks no presence.
        """
        if self.guard is not None and self.guard.state == LOST:
            log.info("not heartbeating: another poller holds this bearer's "
                     "guard, and two clients claiming one bearer's presence is "
                     "the split brain the guard is for (J1)")
            return False
        return self._heartbeat(force=True)

    def _heartbeat(self, force: bool = False) -> bool:
        """Liveness, `inflight`, and whatever presence is known (E1).

        `inflight` is read from the journal (E2) — the count of ids accepted
        from the broker whose results have not been POSTed — and reconciled
        first (E3), because it is a broker-visible signal with scheduling
        consequences: a ledger that only grows ends with the presence sweep
        marking the agent unassignable (2026-07-09: 175 stranded ids, none with
        any work behind them).

        Presence fields ride along only when known. A status-less client that
        sent nulls would clobber the broker's last-known presence, and the
        broker only records on presence.
        """
        if self._heartbeat_disabled:
            return False
        # Monotonic (A8): on the wall clock an NTP step backwards silently
        # suspended the heartbeat for the size of the step, and `inflight` is a
        # signal the broker's presence sweep schedules against.
        now = time.monotonic()
        if (not force and self._last_heartbeat is not None
                and now - self._last_heartbeat < self.heartbeat_interval):
            return False
        self._last_heartbeat = now
        self._reconciler.reconcile(self._live)

        payload: Dict[str, Any] = {
            "client": self.client_name,
            "protocol_version": PROTOCOL_VERSION,
            "tier": self.tier,
            "inflight": self.journal.inflight(),
            "capabilities": list(self.capabilities),
        }
        if self.provider:
            payload["provider"] = self.provider
        presence_status, presence_step = self._presence
        if presence_status is not None:
            payload["status"] = presence_status
        if presence_step is not None:
            payload["step"] = presence_step

        try:
            self._http.post("/v1/heartbeat", payload, timeout=SIDE_TIMEOUT_S)
        except AuthRejected as exc:
            self._defer_auth(exc)  # C8
            return False
        except RelayHTTPError as exc:
            if exc.status in (404, 405):
                # This broker predates the endpoint. Hard-failing here would
                # strand every pre-heartbeat deployment.
                self._heartbeat_disabled = True
                log.info("this broker has no heartbeat endpoint (HTTP %s) — "
                         "continuing without one", exc.status)
            else:
                log.info("heartbeat failed: HTTP %s — continuing", exc.status)
            return False
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            log.info("heartbeat failed (%s) — continuing", _describe(exc))
            return False
        return True

    def set_presence(self, status: Any = None, step: Any = None) -> None:
        """What the agent is doing, for the broker's presence sweep.

        A side channel, and side channels never block delivery (D4): anything
        that is not usable text degrades to absent rather than raising. Absent
        means the field is omitted, which means the broker keeps what it knew.
        """
        self._presence = (_presence_text(status), _presence_text(step))

    # --- auth recovery (C2-C4, C8) ----------------------------------------

    def _defer_auth(self, exc: AuthRejected) -> None:
        """Hold a rejection a best-effort path met, for the loop to act on.

        Best-effort endpoints swallow most failures; they must not swallow a
        revoked bearer (C8). Deferring rather than raising in place keeps the
        rejection from tearing through the middle of a batch of leased tasks.
        """
        if self._deferred_auth is None:
            self._deferred_auth = exc

    def _raise_deferred_auth(self) -> None:
        exc, self._deferred_auth = self._deferred_auth, None
        if exc is not None:
            raise exc

    def _on_auth_rejected(self, exc: AuthRejected) -> float:
        """401/403: re-read the token source, and hold rather than die (C2).

        The immediate re-read catches a rotation that already happened while
        this client lagged behind a re-onboard. Otherwise the loop holds at a
        slow cadence, writing status every pass, until the rotation lands — and
        resumes live, with no restart. The historical behaviour was an
        immediate fatal exit, which under a supervisor that blindly relaunches
        is a silent crash-loop hammering the gateway until a human notices.

        A rotation is re-read through the same parse as startup (C4) and lands
        in the one place every request path reads (C3), so it reaches the poll,
        the acks and the results together.
        """
        rotation = self.credentials.reload()
        if rotation.rotated:
            log.warning("auth rejected (HTTP %s), but the token source had "
                        "already rotated — resuming with the new bearer: %s",
                        exc.status, rotation.reason)
            self._update_status(RECONNECTING, error=None, backoff_s=0.0)
            return 0.0
        if self.credentials.token_file is None:
            # Nothing to re-read, so nothing can change: the historical fatal
            # contract survives exactly here, and nowhere else. A library does
            # not kill its host process — it stops polling and says why, in the
            # status file and the hook, and the consumer decides.
            log.error("auth rejected (HTTP %s) and no durable token source is "
                      "configured — this client cannot recover; polling stops",
                      exc.status)
            self._update_status(FATAL, error=f"auth rejected HTTP {exc.status}: "
                                             "no durable token source to rotate from")
            self._stop.set()
            return 0.0
        log.warning("auth rejected (HTTP %s) — waiting for a rotation in %s, "
                    "re-checking every %ss (%s)", exc.status,
                    self.credentials.token_file, self.auth_recheck_interval,
                    rotation.reason)
        self._update_status(
            AUTH_WAIT,
            error=f"auth rejected HTTP {exc.status} — waiting for token rotation",
            backoff_s=self.auth_recheck_interval)
        return self.auth_recheck_interval

    # --- what a consumer reads --------------------------------------------

    def next_task(self, timeout: Optional[float] = None) -> Optional[Task]:
        """The next Task, or `None` if none arrived within `timeout`.

        `queue.Queue.get` semantics: no timeout means wait indefinitely. A
        consumer that wants neither reads `client.tasks` directly — the queue
        is public because wrapping every one of its methods would be a worse
        surface than the one the standard library already documents.
        """
        try:
            return self.tasks.get(timeout=timeout)
        except queue.Empty:
            return None

    def snapshot(self) -> Dict[str, Any]:
        """Connection state, thread-safe, as data."""
        return self.status.snapshot()

    def on_status(self, hook) -> None:
        """Called after every status change, with the snapshot. Never on the
        critical path: a hook that raises is logged and forgotten (D4)."""
        self.status.on_change(hook)

    def healthz(self) -> Any:
        """The liveness probe. It exists so that nothing ever probes with
        `GET /v1/tasks`, which leases tasks (F1)."""
        return self._http.get("/v1/healthz", timeout=SIDE_TIMEOUT_S)

    def inflight(self) -> List[str]:
        """The ids this client owes the broker an answer for (E2)."""
        return self.journal.inflight_ids()

    def hold(self, broker_id: str) -> None:
        """Say that an id is in this consumer's hands, and must not be reconciled.

        `prepare()` invites a consumer with a scheduler of its own, and such a
        consumer can be holding work across a restart that *this* run never
        accepted from the wire. E3's reconciler drops exactly those — ids the
        journal calls accepted that no live Task claims — which is right for an
        id whose Task died with its process and wrong for one a consumer
        persisted and is still working on. Getting it wrong costs the room
        sidecar (F7), so an attachment produced afterwards has no room to
        target and is simply not delivered.

        There was no supported way to say so: the live set and the reconciler
        are both private, and reaching into them is not an interface. This is
        that way. `complete`, `reject` and a successful result POST all release
        the hold again, so a consumer that answers never has to.
        """
        wire_id = self._require_wire_id(broker_id)
        self._live.add(wire_id)

    # --- internals ---------------------------------------------------------

    def _update_status(self, state: str, **fields: Any) -> None:
        self.status.update(
            state,
            inflight=self.journal.inflight(),
            pending_results=len(self.journal.pending_results()),
            # The guard's own verdict rides along on every write, because
            # "polling anyway with a guard it could not read" and "polling with
            # the guard held" look identical from outside and are not the same
            # operational situation.
            singleton=self.guard.state if self.guard is not None else "off",
            # How much of F4's ack cooldown is left, because against this broker
            # a pause on acking is a pause on delivery and a supervisor cannot
            # otherwise tell "quiet" from "every lease in this window is being
            # requeued underneath us".
            acks_paused_s=(0.0 if self._ack_disabled_until is None else round(
                max(0.0, self._ack_disabled_until - time.monotonic()), 1)),
            **fields)

    def _require_wire_id(self, broker_id: Any) -> str:
        """Validate at egress too, not only at intake (F8).

        The id goes back onto the wire and into a journal path from here; an
        id that reaches this method from consumer code has not been through
        intake's check.
        """
        if not isinstance(broker_id, str) or not valid_wire_id(broker_id):
            raise ValueError(f"not a broker task id: {broker_id!r:.80}")
        return broker_id


#: The names an HTTP error message is written under. The deployed broker uses
#: the first one (`api/v1.py:461` answers `{"error": "not leased to you"}`);
#: the rest are what the same message would be called by anything else.
_ERROR_KEYS = ("error", "detail", "message", "reason", "title")


def _says_not_leased(body: Any) -> bool:
    """Is this 404 the per-task negative, or a route that is not there? (F4)

    Only the body tells them apart, and reading the per-task one as "no route"
    is what let a single stale lease blind a whole host's `received` state.

    Both readings, on purpose. The raw text first, because that is what the wire
    carried and it is what a substring match has always seen; then the parsed
    body's error message, under any of the names one goes by. A sniff that only
    knew the raw text breaks the day the transport hands this a decoded object
    instead of a string, and one that only knew `detail` would already be broken
    against a broker that says `error` — which this one does.
    """
    text = body if isinstance(body, str) else ""
    if "not leased" in text.lower():
        return True
    parsed: Any = body
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            return False
    if not isinstance(parsed, Mapping):
        return False
    return any("not leased" in parsed[key].lower()
               for key in _ERROR_KEYS
               if isinstance(parsed.get(key), str))


def _describe(exc: BaseException) -> str:
    """An exception as one status-file-safe line."""
    text = f"{type(exc).__name__}: {exc}"
    return text.replace("\n", " ")[:300]


def _error_code(reason: Any) -> str:
    """A reject reason as an error code the broker can log and an operator can
    grep. Free text degrades to the schema code rather than travelling as one."""
    if not isinstance(reason, str) or not reason.strip():
        return INVALID_TASK_SCHEMA
    code = "".join(c if c.isalnum() else "_" for c in reason.strip().upper())
    return code[:64].strip("_") or INVALID_TASK_SCHEMA


def _presence_text(value: Any) -> Optional[str]:
    """A presence field as bounded text, or `None` for "not known".

    Never raises and never coerces: a non-string is absent, because the broker
    calls string methods on what it is given and a forwarded dict is its
    problem, arriving from here.
    """
    if not isinstance(value, str):
        return None
    text = value.replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or None
