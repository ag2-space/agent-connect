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

**The ordering that makes at-least-once safe.** Journal first, then ack, then
deliver (F2): an ack that goes out before local durability leaves the broker
showing "received" for work no surviving process knows about. A result is
retained until its POST succeeds (F5) — success is the only thing that retires
it, because `POST /v1/results` is what completes the lease. And every id the
broker re-serves is checked against the journal before anything executes (F3):
already answered means re-complete, never re-execute. The reconnect replays of
2026-06-30 and 2026-07-01 were 500 historical tasks each, and without that check
every one of them ran again.

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

import logging
import queue
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .backoff import Backoff
from .credentials import TokenSource
from .envelope import Task, parse_task
from .journal import Journal, Reconciler
from .media import MediaIngress, MediaStore
from .resolver import BoundedResolver
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
ACK_COOLDOWN_S = 300.0

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

#: The one marker the wire loop itself needs: "recorded, nothing to post". It
#: completes the lease without a user-facing reply, which is what makes
#: re-completing a redelivery possible at all — a skipped `/v1/results` would
#: leave the lease to expire and the task to come back again. The rest of the
#: marker grammar (redirect, dm-only, attach) belongs to the Room Ops ticket
#: and has exactly one parser when it lands.
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
    """

    def __init__(
        self,
        credentials: TokenSource,
        state_dir,
        instance: str = "default",
        media_dir=None,
        media_retention_s: Optional[float] = None,
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
        idle_gap: float = 0.0,
        tier: str = "owner",
        client_name: str = "ag2-relay-client",
        capabilities: Sequence[str] = CAPABILITIES,
    ):
        self.credentials = credentials
        self.layout = StateLayout(state_dir, instance)
        self.http = http or RelayHTTP(credentials, resolver=resolver)
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
        #: What the loop waits after a *healthy* poll. Zero: the long poll is
        #: the pacing. A test with an instant broker sets it to keep the loop
        #: from spinning.
        self.idle_gap = float(idle_gap)
        #: A node self-description for the heartbeat, never an authorization
        #: input — the broker attests each task's sender itself.
        self.tier = tier
        self.client_name = client_name
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
            self.http,
            MediaStore(media_dir or self.layout.media_path, media_retention_s),
            deliver=self.tasks.put,
            on_auth_rejected=self._defer_auth,
        )

        self._reconciler = Reconciler(self.journal)
        #: Ids accepted by *this* run. The journal knows which ids are owed an
        #: answer; only this set knows which of them a consumer could still be
        #: working on, which is what keeps E3 from dropping a live one.
        self._live: set = set()
        self._presence: Tuple[Optional[str], Optional[str]] = (None, None)
        self._ack_disabled_until = 0.0
        self._heartbeat_disabled = False
        self._last_heartbeat = 0.0
        self._deferred_auth: Optional[AuthRejected] = None
        self._drain_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<RelayClient {self.layout.instance} {self.http.base_url}>"

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> "RelayClient":
        """Open the state dir, replay what a previous run left, and poll."""
        if self._thread is not None:
            raise RuntimeError("this client is already started")
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

    def stop(self, timeout: float = 40.0) -> None:
        """Stop polling. Anything already answered stays on disk to be re-POSTed."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        # After the poll thread, so nothing is still being handed to it. A
        # download in flight is not waited out — `stop()` returning promptly
        # matters more, and a late fetch can only put a Task on a queue nobody
        # is reading and leave a file for the next run's sweep.
        self.media.stop()
        if self.guard is not None:
            # After the join, not before: a guard released while this loop is
            # still turning is a bearer two clients may poll at once. Released
            # at all — rather than left to go stale — because a restart that
            # has to wait out a freshness window it does not need is two
            # minutes of a user's messages going nowhere.
            self.guard.release()
        self._update_status(STOPPED)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.poll_once()
            except Exception:  # noqa: BLE001 — D1: an unexpected error costs
                # a delay, never the bearer's only poller. `poll_once` already
                # catches broadly; this is the second belt, for a bug in the
                # backoff or the status write itself.
                log.exception("unexpected error in the poll loop")
                delay = self.backoff.after_error()
            if delay:
                self._stop.wait(delay)

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
            # Both of these are best-effort and swallow their own failures;
            # what they are not allowed to swallow is a rejected bearer, which
            # they hand to the recovery below (C8).
            self._drain_results()
            self._heartbeat()
            self._raise_deferred_auth()
            answer = self.http.get(
                "/v1/tasks", params={"wait": self.poll_wait},
                timeout=self.poll_wait + self.socket_margin)
            self._intake_all(answer)
            if self.journal.pending_results():
                # Either a redelivery was just re-completed above, or the
                # consumer answered while the poll was in flight. Either way it
                # goes now rather than one long-poll window later: the thing at
                # stake is a lease, and a lease is on a clock.
                self._drain_results()
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
        if self.guard.claim() != LOST:
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
            backoff_s=self.standby_recheck)
        return self.standby_recheck

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
        accepted = 0
        for raw in served:
            accepted += self._intake(raw)
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
        if self.journal.is_done(wire_id):
            # F3: the answer already landed. Re-complete the lease upstream
            # with a skip marker — the broker dedups the result and delivers
            # nothing — and never hand the task to the consumer again. Silently
            # dropping it instead would leave the lease to expire and the task
            # to come back for a third time.
            self.journal.record_result(wire_id, {"id": wire_id, "body": NO_SEND})
            log.info("task %s was already answered — re-completing the lease, "
                     "not re-executing (attempt %s)", wire_id, task.attempt)
            return 0
        if self.journal.is_pending(wire_id):
            log.info("task %s already has an answer waiting to be POSTed — "
                     "the redelivery changes nothing", wire_id)
            return 0
        if wire_id in self._live:
            return 0  # queued or in the consumer's hands; a re-serve is a no-op

        # Durable first, then the ack, then the handoff (F2). The room sidecar
        # is captured here because this is the only moment it is known and a
        # media answer produced after a restart still has to find its room (F7).
        self.journal.accept(wire_id, room=task.room_id)
        self._live.add(wire_id)
        self._ack(wire_id)
        if task.metadata_stripped:
            log.info("stripped unsigned room-ops metadata from task %s", wire_id)
        # Delivery goes through the media stage, which strips any
        # `[ag2space-media:]` marker here and now — a regex, on this thread —
        # and hands the task to its own thread only when there are bytes to
        # fetch. Nothing that reaches `self.tasks` has ever carried a marker,
        # and nothing on this thread ever waits for a download (F1).
        self.media.accept(task)
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
        try:
            self.http.post(
                "/v1/results",
                {"id": wire_id, "status": "rejected", "error_code": INVALID_TASK_SCHEMA},
                timeout=RESULT_TIMEOUT_S)
        except AuthRejected as exc:
            self._defer_auth(exc)
        except Exception as exc:  # noqa: BLE001 — best-effort by construction
            log.warning("dead-letter reject failed (%s); the broker will "
                        "re-serve until its attempt cap", _describe(exc))

    # --- ack (F2, F4) ------------------------------------------------------

    def _ack(self, wire_id: str) -> bool:
        """Tell the broker the task was received. Informational, and it is
        allowed to fail — the ack never touches the lease.

        The 404 is the interesting part. It means two different things and only
        the body tells them apart: `not leased to you` is a routine per-task
        negative under lease churn, and treating it as "this broker has no ack
        route" let one stale lease blind the whole host's `received` state. A
        bare no-route 404/405 really is an unsupported endpoint — and that gets
        a cooldown rather than a latch, so a deploy that adds the route is
        picked up without a restart (F4).
        """
        now = time.time()
        if now < self._ack_disabled_until:
            return False
        safe_id = urllib.parse.quote(wire_id, safe="")
        try:
            self.http.post(f"/v1/tasks/{safe_id}/ack", {"id": wire_id},
                           timeout=SIDE_TIMEOUT_S)
        except AuthRejected as exc:
            self._defer_auth(exc)  # C8
            return False
        except RelayHTTPError as exc:
            if exc.status in (404, 405):
                if exc.status == 404 and "not leased" in (exc.body or "").lower():
                    log.info("ack for %s: the lease is gone (re-served or not "
                             "ours) — every other task keeps acking", wire_id)
                    return False
                self._ack_disabled_until = now + self.ack_cooldown
                log.warning("this broker has no task-ack route (HTTP %s) — "
                            "acking pauses for %ss, then asks again",
                            exc.status, self.ack_cooldown)
                return False
            log.info("ack for %s failed: HTTP %s — the broker may redeliver",
                     wire_id, exc.status)
            return False
        except Exception as exc:  # noqa: BLE001 — ack is never a gate
            log.info("ack for %s failed (%s) — the broker may redeliver",
                     wire_id, _describe(exc))
            return False
        # A success re-enables acking: the cooldown is a pause, not a state.
        self._ack_disabled_until = 0.0
        return True

    # --- results (F5) ------------------------------------------------------

    def complete(self, broker_id: str, body: str) -> None:
        """Answer a task. The answer is durable before this returns.

        Delivery is this library's problem from here: the result is retained
        until `POST /v1/results` succeeds, across retries and across a restart,
        because that POST is what completes the lease. Dropping it on a
        transient failure loses the user's answer *and* re-serves the task.

        An empty body is refused rather than sent (H5). A blank answer is "not
        ready", not "an empty answer" — a deliberate silence is `[no-send]`,
        which completes the lease and posts nothing.
        """
        wire_id = self._require_wire_id(broker_id)
        if not isinstance(body, str):
            raise TypeError(f"a result body is text; got {type(body).__name__}")
        if not body.strip():
            raise ValueError(
                f"refusing an empty result for {wire_id}: a blank answer is "
                f"'not ready', not an answer. To complete the lease with no "
                f"user-facing reply, send {NO_SEND!r}")
        self._record_result(wire_id, {"id": wire_id, "body": body})

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
        """POST everything still owed, oldest first. Never raises (D4)."""
        delivered = 0
        with self._drain_lock:
            for wire_id, payload in self.journal.pending_results():
                try:
                    answer = self.http.post("/v1/results", payload,
                                            timeout=RESULT_TIMEOUT_S)
                except AuthRejected as exc:
                    self._defer_auth(exc)  # C8
                    break
                except Exception as exc:  # noqa: BLE001 — F5: keep and retry
                    log.warning("result POST for %s failed (%s) — the result is "
                                "retained and retried; nothing is lost",
                                wire_id, _describe(exc))
                    break
                if isinstance(answer, Mapping) and answer.get("ok") is False:
                    log.warning("the broker refused the result for %s — retained "
                                "for the next pass", wire_id)
                    break
                duplicate = isinstance(answer, Mapping) and answer.get("duplicate")
                self.journal.retire(wire_id)
                self._live.discard(wire_id)
                delivered += 1
                log.info("result delivered for %s%s", wire_id,
                         " (the broker had it already)" if duplicate else "")
        return delivered

    # --- heartbeat (E1, E2, E3) --------------------------------------------

    def heartbeat(self) -> bool:
        """Send a heartbeat now, if the broker still has the endpoint."""
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
        now = time.time()
        if not force and now - self._last_heartbeat < self.heartbeat_interval:
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
        presence_status, presence_step = self._presence
        if presence_status is not None:
            payload["status"] = presence_status
        if presence_step is not None:
            payload["step"] = presence_step

        try:
            self.http.post("/v1/heartbeat", payload, timeout=SIDE_TIMEOUT_S)
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
        return self.http.get("/v1/healthz", timeout=SIDE_TIMEOUT_S)

    def inflight(self) -> List[str]:
        """The ids this client owes the broker an answer for (E2)."""
        return self.journal.inflight_ids()

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
