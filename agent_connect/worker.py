"""agent-connect worker.

Drains the Relay Client's Task queue, runs the configured Adapter on each Task,
and answers it — `complete` with what the Local Agent produced, `reject` for a
Task nothing could ever answer. The Relay Client is a library this repository owns
and this process runs (workspace `docs/adr/0001`); everything the wire knows is
below `agent_connect.relay`, and everything above it — Sessions, the Ladder, the
Sandbox, ACP — is here.

There used to be a `tasks/` directory between the two, written by a foreign
process, globbed here, and remembered in an in-memory `seen` set. All three are
gone, and with them the thing they never actually provided: the delivery
guarantee was always the broker's lease, and a `seen` set that dies with the
process turned a restart mid-Task into a re-run of the Turn. The journal under
the library re-*completes* a redelivery instead, and a Task never reaches this
module twice.

**The library is sync and threaded; this side is asyncio.** Every call across
that seam runs on a **daemon thread** rather than on the event loop or on the
default executor: the queue read on one of its own, `complete` and `reject` on
one apiece, and every Room Op the Ladder asks for on one of its own too. Cadence
is a correctness property on the other side of the seam — a poll thread that
stops polling loses its leases — and a blocked event loop up here is a
`complete` that never happens down there. The daemon part is the same argument
at shutdown: `asyncio.run` joins every thread of the default executor on its way
out, so one answer in flight would hold the process open past the point a
service manager stops waiting and starts killing. The one function that does
this is `agent_connect.offthread.in_daemon_thread`, in a module under neither
side of the seam, because the Adapter shim needs it too and has no business
importing the relay wiring to borrow a thread.

**A file the agent produced goes out through `complete`, not from here.** The
answer's `[file: …]` markers travel with it, the library reads them in the one
place that grammar is written down, and it uploads from a path inside the
allowlist roots this Worker built the client with (`agent_connect.outgoing`).
There is no staging directory any more, and nothing here reads a marker.

Tasks are processed **concurrently across rooms** and serialised within one
Session: a ten-minute request in one room no longer silences every other room,
while two Tasks that share a Session still run one at a time, because only one
Turn at a time may be open on a Session.

**Settings are documented in `README.md` § Settings, and only there.** This
docstring used to carry a second list of the same environment variables, which
went out of date the moment an Adapter grew one of its own; there is now one
home for all of them and `test_acp_settings.py` fails if a setting exists in the
package and not in that section.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

from ag2_relay_client.status import DISPLACED, FATAL

from . import attachments as att
from . import relay as relay_client
from .adapters import get as get_adapter
from .config import CONFIG_ENV, DEFAULT_PATH, ConfigError
from .config import export as export_config
from .config import load as load_config
from .events import Attachment, TurnContext
from .pending import queue_for
from .offthread import in_daemon_thread  # noqa: F401 — re-exported
from .reporter import LadderSettings, TurnReporter
from .roomops import room_ops_for
from .sandbox import sandbox_preamble, tier_to_sandbox  # noqa: F401 — re-exported
from .sessions import workspace_dir
from .status import StatusFile, from_env as status_from_env


def _ws() -> Path:
    # One definition of "the workspace", shared with the Session map that lives
    # in it — `agent_connect.sessions` owns it because the Adapter reaches for
    # it without going through the Worker.
    return workspace_dir()


# macOS TCC-protected locations: an agent operating out of one of these hits
# permission walls on file access unless the launching process has Full Disk
# Access. The old cwd default silently put the agent in whatever dir the user
# launched from — often ~/Documents / ~/Desktop — which produced opaque TCC
# failures (owner-caught friction). We default to a dedicated ~/agents dir and
# warn loudly if the resolved repo still lands under a protected path.
_TCC_PROTECTED = ("Desktop", "Documents", "Downloads")


def _resolve_repo() -> Path:
    """Resolve the working dir the agent operates in.

    `AGENT_CONNECT_REPO` (if set) wins; otherwise default to ``~/agents`` — a
    dedicated, non-TCC-protected dir — instead of the launch cwd. Creates the
    dir when defaulting, prints where it landed, and warns if the path is under
    a macOS TCC-protected location (agent file ops there need Full Disk Access).
    """
    explicit = os.environ.get("AGENT_CONNECT_REPO")
    repo = Path(explicit).expanduser() if explicit else (Path.home() / "agents")
    if not explicit:
        repo.mkdir(parents=True, exist_ok=True)
        print(f"agent-connect: no AGENT_CONNECT_REPO set — defaulting repo to {repo}")

    home = Path.home()
    try:
        rel = repo.resolve().relative_to(home.resolve())
        top = rel.parts[0] if rel.parts else ""
    except ValueError:
        top = ""
    if top in _TCC_PROTECTED:
        print(
            f"agent-connect: WARNING — repo {repo} is under a macOS TCC-protected "
            f"location (~/{top}); the agent's file operations may fail without "
            f"Full Disk Access. Set AGENT_CONNECT_REPO to a dir like ~/agents."
        )
    return repo

#: The Access Tier vocabulary this side acts on, and the whole of it. The broker
#: attests the tier and the Worker acts on the attestation (`docs/adr/0003`);
#: nothing here restamps a tier of its own. Anything that is not exactly `owner`
#: — a missing field, a near-miss like `OWNER`, or one of the other words
#: sutando still writes (`team`) — is not an attestation of ownership, and a
#: Task without one is a guest's.
OWNER = "owner"
GUEST = "guest"

#: What a Task nothing could ever answer is dead-lettered as. The broker parks
#: it, stops re-serving it, and posts the terminal-failure notice itself.
EMPTY_TASK = "EMPTY_TASK"

#: What the Adapter is asked when a Task carries files and not one word of text.
#: An upload with no caption is a question — "look at this" — and the Local Agent
#: has to be told it is being asked one, because the files themselves arrive
#: beside the prompt rather than in it.
UNCAPTIONED = "(shared with no message of its own: {names})"


def attested_tier(raw: str) -> str:
    """The Tier to act on, from the Tier the broker attested. Fails closed.

    The broker computes the sender's Access Tier and attests it on the Task;
    this is the whole of the Worker's judgement about it — either the broker
    said `owner`, or it did not. A value it does not recognise is not a value it
    may guess at, and that is not a hypothetical: sutando writes
    `access_tier: team` on a task from a negotiated collaborator, and it arrives
    here. It is read as `guest`, like everything else that is not `owner` —
    read-only under codex and refused under ACP. The demotion is deliberate and
    recorded in `docs/adr/0003`; a tier this side cannot verify is a tier this
    side does not get to interpret.
    """
    return OWNER if (raw or "").strip() == OWNER else GUEST


def task_attachments(task) -> Tuple[Attachment, ...]:
    """The files that came with a Task, as the Adapter boundary names them.

    Read off the delivered Task and from nowhere else. The library resolves the
    `[ag2space-media: …]` markers on the wire and hands over local paths, so
    there is no marker and no URL on this side of the seam to parse — and there
    never was an `attachments:` header to parse either. The relay used to
    dual-write a `[File attached: <path>]` line into the body as well; whatever
    of that a sender types is left exactly where it is, because a path read out
    of a body would be a path the sender chose.

    The crossing itself — the library's vocabulary into the Adapter boundary's,
    a failed fetch's reason included — is `attachments.delivered`, which is the
    module that owns both sides of it.
    """
    return att.delivered(task.attachments)


def uncaptioned_prompt(attachments) -> str:
    """What a Task with files and no text asks, or `""` when it carries no files.

    A caption-less upload from Element arrives as a body that was *only* a media
    marker, and the library empties such a body by design — "the attachment
    tuple is where that task's content is". Judged on the body alone that Task
    reads as empty, and empty is terminal: `reject` dead-letters it, the broker
    posts a failure notice, and someone who dropped a screenshot into a room is
    told their message could never be answered while the Adapter never ran.

    So emptiness is judged on the body **and** the files. A Task carrying files
    has content, and this is the sentence that says so in band, naming them, in
    the one place that knows both halves.
    """
    if not attachments:
        return ""
    return UNCAPTIONED.format(
        names=", ".join(att.label(one) for one in attachments))


def turn_context(task, repo: str) -> TurnContext:
    """Build the context one Turn travels with, from a delivered Task.

    The Access Tier is settled here, and everything downstream — the Sandbox,
    the Session key, the ACP Adapter's owner-only check — reads the settled
    value rather than the attestation. It comes from `access_tier` as the broker
    attested it and from nothing else the sender can write; `attested_tier` then
    reduces it to the two values that cross the wire, so no consumer has to
    decide for itself what an unfamiliar tier means. The library delivers the
    attestation verbatim on purpose: mapping it to local privilege is the
    consumer's decision (`docs/adr/0003`), and this line is where this consumer
    makes it.

    The room identifier is the broker's `channel_id`, which the library delivers
    as `room_id` — one name for one concept, the rename I1 asked for.
    `room_name` is the human label and is carried for display only.

    **A Turn's content is the body and the files together.** A prompt is empty
    here only when the Task carried nothing at all: an upload with no caption
    arrives with an empty body and a file, and `uncaptioned_prompt` is what it
    asks. Nothing is ever folded into a body someone typed — this is the case
    where nobody typed one.
    """
    tier = attested_tier(task.access_tier)
    attachments = task_attachments(task)
    return TurnContext(
        prompt=(task.body or "").strip() or uncaptioned_prompt(attachments),
        task_id=task.id,
        room=task.room_id,
        room_name=task.room_name,
        access_tier=tier,
        sender_name=task.sender_name,
        user_id=task.user_id,
        source_message_id=task.source_message_id,
        sandbox=tier_to_sandbox(tier),
        cwd=repo,
        attachments=attachments,
    )


async def run_turn(adapter, ctx: TurnContext, ops=None, settings=None, reporter=None) -> str:
    """Drive one Turn up the Ladder and return the body to write as the result.

    The whole event stream goes to the `TurnReporter`, which owns everything the
    room sees: the placeholder, the throttled progress edits, the final edit and
    the terminal marker — or, for a Turn that produced nothing, the structured
    rejection that leaves the failure notice to the broker. With no `ops` — a
    Worker holding no relay token, or a test — nothing is posted and the answer
    travels as the result body, exactly as it did before the Ladder existed.

    `reporter` is for a caller that already had to speak to the room before the
    Turn began — announcing a queued message, which happens before the
    placeholder exists. Everyone else lets one be built here.
    """
    return await (reporter or TurnReporter(ops, settings)).run(adapter, ctx)


async def _preflight(check, status: Optional[StatusFile]):
    """Run the Adapter's check with the status file beating underneath it."""
    heartbeat = (asyncio.ensure_future(status.beating())
                 if status is not None else None)
    try:
        return await check()
    finally:
        if heartbeat is not None:
            heartbeat.cancel()


class _NoLock:
    """Stand-in for a Session lock when the caller is not serialising."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def process_one(
    task,
    adapter,
    repo: str,
    sessions: dict = None,
    ops=None,
    settings=None,
) -> str:
    """Handle one delivered Task and return the body that answers it.

    Returns `""` for a Task nothing could answer — no text *and* no files, which
    is what a body that was only an unsigned metadata block degrades to after
    the library quarantines it. `handle_one` turns that into the reject.
    Everything else comes back as a body: the answer, the terminal marker when
    the answer already went to the room up the Ladder, or the structured
    rejection when the Turn produced nothing to say.

    An upload with no caption is emphatically not one of them: its body is empty
    and its content is in the attachments, so `turn_context` gives it a prompt
    that says so and the Turn runs like any other.

    `sessions` — when given — maps a Session key to the `SessionQueue` that
    keeps one Turn at a time open on it. Tasks with different keys never
    contend, which is what makes two rooms concurrent; Tasks with the same key
    queue, and **the person is told they are queued** rather than left
    wondering whether their message arrived. Without a registry there is no
    serialisation at all, which is what a single-Task caller wants.

    `ops` is the relay to climb the Ladder on, shared across Tasks; `settings`
    is how much of it to climb. Both may be `None`, and then the answer travels
    whole in the body this returns, as it always did.
    """
    ctx = turn_context(task, repo)
    if not ctx.prompt:
        return ""
    reporter = TurnReporter(ops, settings)

    async def announce(turn) -> None:
        # Said before the wait rather than after it, and as its own message:
        # the placeholder for this Task does not exist yet, and editing
        # someone else's would take their answer away.
        await reporter.queued(ctx, turn.ahead)

    if sessions is None:
        held = _NoLock()
    else:
        held = queue_for(sessions, ctx.session_key).arrive(ctx, on_wait=announce)
    async with held:
        return await run_turn(adapter, ctx, ops, settings, reporter)


async def handle_one(
    task,
    adapter,
    repo: str,
    sessions: dict = None,
    ops=None,
    settings=None,
    *,
    client=None,
) -> str:
    """`process_one`, answered to the broker, and unable to take the Worker down.

    **Every Task that comes off the queue leaves through this function, once.**
    That is the contract the library's seam is built on: it hands a Task over
    and waits to be told `complete` or `reject`, and an answer that is merely
    dropped is a lease left to expire, a redelivery, and eventually a
    dead-letter five attempts later. So a Task that reaches the end of this
    function has left through one of those two calls, and never both.

    **Cancellation is the third exit, and it answers neither on purpose.**
    `CancelledError` has been a `BaseException` since 3.8, so the guard below
    does not catch it and must not: on a stop, `asyncio.run` cancels every Turn
    in flight, and an id left accepted-and-unanswered is re-served by the broker
    and re-executed — which is the correct outcome for work that never finished.
    Answering it here would be worse in both directions: a `complete` would post
    an answer nobody produced, and a `reject` is terminal for a Task whose only
    problem was that the Worker was asked to stop.

    One Task failing is still one Task's problem: the failure becomes the body,
    so the person who asked hears something back rather than nothing, and the
    drain loop above keeps turning.

    A `reject` is reserved for input nothing could ever answer, which on this
    side of the seam is one thing: a Task with neither text nor files in it.
    Re-serving that produces the same nothing five times over, and the broker's
    dead-letter park is where the protocol says it goes — this is the flow
    sparrow never implemented and the reason the old code could only write
    `[no-send] empty task` and hope. The broker posts the terminal-failure
    notice; the Worker posts none, exactly as with the `TurnReporter`'s
    structured rejection.

    `client` may be `None` — a test, or a caller driving one Task by hand — and
    then the answer travels back as the return value and nothing is told to any
    broker. The body is returned either way, and `""` means the Task was
    rejected.
    """
    try:
        body = await process_one(task, adapter, repo, sessions, ops, settings)
    except asyncio.CancelledError:
        # Written out rather than left to the `Exception` guard's blind spot, so
        # the third exit is visible where the other two are. Nothing is said to
        # the broker: see above.
        raise
    except Exception as e:  # noqa: BLE001 — never die on one bad task
        body = f"agent-connect: worker error: {e}"
    if not body.strip():
        # Belt and braces: `process_one` returns "" only for a Task with neither
        # text nor files, and the reporter's endings are never blank. A blank
        # body would be refused by `complete` anyway (H5: an empty answer is
        # "not ready", not an answer), so it must not be able to reach it.
        await _answer(client, "reject", task.id, EMPTY_TASK)
        return ""
    # `repo` travels with the answer as the directory a *relative* path in a
    # `[file:]` marker is read against — the Turn's working directory, which is
    # also the root the client was built with. It widens nothing: the library
    # still judges where the file is, never how it was named.
    await _answer(client, "complete", task.id, body, repo)
    return body


async def _answer(client, how: str, task_id: str, payload: str, *rest) -> None:
    """Tell the library how one Task ended, without blocking the event loop.

    The call is sync and does I/O — `complete` reads the answer's markers,
    uploads whatever it named from an allowlisted path, writes the journal and
    then POSTs — so it goes to a thread of its own (see
    `agent_connect.offthread.in_daemon_thread`: not the default executor, which a
    stop would have to join). It is also the last thing standing between a
    finished Turn and the person who asked, so a failure here is said loudly on
    stderr and nowhere else: raising would take down the drain loop, and
    swallowing it silently would lose an answer without a trace. The retry is
    the library's (F5: a result is retained until its POST succeeds), which is
    why there is none here.

    A refused attachment is **not** one of the failures this catches, and must
    not be: the library says so in the answer it POSTs, where the person who
    asked for the file will read it, rather than by raising into this loop (I1).
    """
    if client is None:
        return
    try:
        await in_daemon_thread(getattr(client, how), task_id, payload, *rest)
    except asyncio.CancelledError:
        raise                               # a stop, not a failed answer
    except Exception as exc:  # noqa: BLE001 — one Task's answer, not the loop
        print(f"agent-connect: could not {how} task {task_id}: {exc}",
              file=sys.stderr, flush=True)


class RelayStopped(Exception):
    """The Relay Client's poll loop ended, and only a new client resumes it."""


#: The two connection states the library never comes back from by itself.
#:
#: Everything else it can report is the library *waiting*, and waiting is not
#: dying: `reconnecting` retries by itself, `auth-wait` holds at a slow cadence
#: until a rotation lands, and `standby` keeps asking for a bearer another
#: poller holds so that a holder which died is taken over from with no operator
#: in the loop. These two are different in kind — the loop has stopped, and the
#: library's own contract is that coming back is the consumer's decision:
#:
#: * `fatal` — the bearer was rejected and there is nothing to re-read (this
#:   Worker passes its token by value, so that is every rejection it can meet).
#: * `displaced` — another poller took this bearer's guard. Re-acquiring by
#:   itself would be the reaped-process incident with extra steps, so the loop
#:   stops for good (J1).
RELAY_ENDED = (FATAL, DISPLACED)


def relay_stopped_for_good(client) -> Optional[RelayStopped]:
    """Why this Worker can never be given work again, or `None`.

    A Worker whose Relay Client has stopped is the failure this module's `serve`
    docstring already names about the queue reader — "alive by every measure
    anybody has, and no longer an agent" — reached from the other side. The
    reader is fine; what it is reading from has ended. Nothing used to notice:
    the connection state went into the status file's `relay` block, where it was
    true and unread, while the document's own `state` went on saying `serving`
    and the process went on beating it, for ever, receiving nothing.
    """
    snapshot = getattr(client, "snapshot", None)
    if snapshot is None:
        return None                         # a caller driving Tasks by hand
    try:
        state = (snapshot() or {}).get("state")
    except Exception:  # noqa: BLE001 — an observer is never a delivery blocker
        return None
    if state not in RELAY_ENDED:
        return None
    return RelayStopped(
        f"the Relay Client stopped for good ({state}) — it holds no bearer it "
        f"can poll, and only a restart re-enters the arbitration. See the "
        f"`relay` block of the status file for what it last said"
    )


async def serve(
    adapter,
    repo: str,
    client,
    poll: float,
    ops=None,
    settings=None,
    *,
    status: Optional[StatusFile] = None,
) -> None:
    """Drain the Task queue forever, starting each Task without waiting for the last.

    The queue read is the one blocking call in this Worker, and it is made on a
    thread of its own — the library is sync and threaded, this side is asyncio,
    and a `get` with a timeout on the event loop would stop everything else for
    the length of the timeout. The thread is a **daemon**: a Worker being
    stopped must not be kept alive by a queue read that is still inside its own
    wait, and a service manager that asked for a stop and got half a minute of
    silence kills rather than waits.

    `poll` is what one queue read waits for before looking around, and it paces
    nothing else: the wire's cadence belongs to the library (F1 — a poll thread
    that stops polling loses its leases) and no setting on this side can reach
    it. A Task that arrives wakes the read immediately, whatever `poll` says.

    `status` — when given — gets a heartbeat task of its own, paced by the
    status file and by nothing here. It used to be beaten once per scan, which
    quietly made `AGENT_CONNECT_POLL` able to defeat staleness: a poll longer
    than three heartbeats left a healthy Worker permanently stale. One setting
    must not be able to switch another one's promise off.

    What the heartbeat reports is what only this function knows: how many Turns
    are in flight and how long the oldest has been running. A beat proves the
    loop is turning; those two numbers are what an observer needs to tell that
    apart from work actually moving. Keyword-only because it is an observer, not
    a parameter of the job.

    **A queue reader that stops reading takes the Worker with it.** It is the
    Worker's only inlet, and a Worker that has lost it goes on beating
    `serving` with nothing running and receives nothing, for ever — alive by
    every measure anybody has, and no longer an agent. So the reader hands
    whatever ended it back to this loop, which raises it: the status file gets
    an `error` with the reason in it, the process exits, and a service manager
    restarts something that can be given work.

    **And so does a Relay Client that has stopped for good.** The same failure
    from the other side: the reader is fine, the thing it reads from has ended
    — a bearer rejected with nothing to re-read, or a singleton guard another
    poller took. The library says so in its snapshot and stops its poll thread;
    nothing above it used to ask, so the state was written into the status
    file's `relay` block and the document's own `state` went on saying
    `serving`. `relay_stopped_for_good` is the asking, on the idle path of the
    queue read, and it ends the Worker the same way.
    """
    sessions: dict = {}
    running: set = set()
    started: dict = {}                      # future → when its Turn began

    def liveness() -> dict:
        now = time.monotonic()
        return {
            "tasks_running": len(running),
            "oldest_task_seconds": round(max((now - t for t in started.values()),
                                             default=0.0), 1),
        }

    heartbeat = (asyncio.ensure_future(status.beating(liveness))
                 if status is not None else None)
    loop = asyncio.get_running_loop()
    inbound: asyncio.Queue = asyncio.Queue()
    stop = threading.Event()

    def hand_over(item) -> bool:
        """Put one thing on the loop's queue. False once the loop has closed."""
        try:
            loop.call_soon_threadsafe(inbound.put_nowait, item)
            return True
        except RuntimeError:
            # The loop closed under us — the Worker is going away. A Task
            # dropped here stays accepted-and-unanswered in the library's
            # journal, which is what the broker re-serves; dropping it is the
            # one thing this queue is documented to be allowed to do, because
            # it is a handoff and not the durability boundary.
            return False

    def pump() -> None:
        try:
            while not stop.is_set():
                task = client.next_task(poll)
                if task is None:
                    # Nothing arrived in `poll` seconds — which is the ordinary
                    # case, and also the only moment this Worker can notice that
                    # the thing feeding it has stopped for good.
                    ended = relay_stopped_for_good(client)
                    if ended is not None and not stop.is_set():
                        hand_over(ended)
                        return
                    continue
                if not hand_over(task):
                    return
        except BaseException as exc:  # noqa: BLE001 — reported, never silent
            # Only `RuntimeError` from the handover used to be caught, so
            # anything the queue read itself raised ended this thread without a
            # word and left a Worker that reported `serving` for ever and
            # received nothing. Whatever it was travels back to the loop, which
            # raises it where the status file and the exit code can see it.
            if not stop.is_set():
                hand_over(exc)

    threading.Thread(target=pump, name="agent-connect-queue", daemon=True).start()
    try:
        while True:
            task = await inbound.get()
            if isinstance(task, RelayStopped):
                raise RuntimeError(
                    f"this Worker can no longer be given work: {task}") from task
            if isinstance(task, BaseException):
                raise RuntimeError(
                    "the queue reader stopped: this Worker can no longer be "
                    f"given work ({type(task).__name__}: {task})") from task
            fut = asyncio.ensure_future(
                handle_one(task, adapter, repo, sessions,
                           ops, settings, client=client)
            )
            running.add(fut)
            started[fut] = time.monotonic()
            fut.add_done_callback(running.discard)
            fut.add_done_callback(started.pop)
    finally:
        stop.set()
        if heartbeat is not None:
            heartbeat.cancel()


def preflight(adapter, status: Optional[StatusFile] = None) -> str:
    """Stop here, not in a room, if the Adapter cannot serve.

    An Adapter may offer `preflight()` — a coroutine returning `None` when it is
    able to work, or the sentence the operator has to act on. The Worker refuses
    to start on anything but `None`, because the alternative is that the first
    person to ask a question in a room receives an installation or login notice
    where they expected an answer, hours after the operator walked away.

    Adapters without one start as they always did.

    Returns the Adapter's own one-line description of what it found, for the
    startup log and for the status file — "what is actually running behind this
    identity" is the first question anyone watching a Worker asks.

    `status` keeps beating while the check runs. An ACP bridge can take the
    better part of a minute to answer `initialize`, and a Worker that is
    starting must not read as one that has died.
    """
    check = getattr(adapter, "preflight", None)
    if check is None:
        return ""
    problem = asyncio.run(_preflight(check, status))
    if problem:
        raise SystemExit(f"agent-connect: {problem}")
    describe = getattr(adapter, "describe", None)
    if describe is None:
        return ""
    found = describe()
    print(f"agent-connect: {found}")
    return found


#: What a stop may spend waiting for the Relay Client's poll thread to leave.
#:
#: The arithmetic, because the number is a trade and not a taste. launchd sends
#: SIGKILL 20 s after its SIGTERM (`ExitTimeOut`'s default; the plist in
#: `install.sh` sets none), and everything after the wait — the `stopped` write
#: that the whole handler exists for — has to happen before that. Twelve leaves
#: eight for the loop to tear down and the file to be written, which measures in
#: milliseconds once no answer is on the default executor (`in_daemon_thread`).
#:
#: **What the wait buys is the singleton guard's release.** The library releases
#: it only when the join actually joined — a guard released while the loop may
#: still poll is two pollers on one bearer, which is the incident J1 exists for
#: — so a stop that gives up leaves the record to go stale and the replacement
#: Worker stands by for `STALE_AFTER_S` + `STANDBY_RECHECK_S`, near three
#: minutes of a person's messages going nowhere. The 2.0 s this replaces could
#: never join at all: the poll thread is inside a 25 s long poll almost all of
#: the time, so `guard.release()` was unreachable code and every restart paid
#: the standby.
#:
#: **And it does not buy it away.** A poll that began a moment before the signal
#: outlives any budget that fits in launchd's window, and then the guard is left
#: held exactly as before. That is the cost accepted here, in exchange for a
#: stop a service manager does not have to kill.
STOP_BUDGET_S = 12.0


class RelayStop:
    """Begins the Relay Client's stop at the first sign the Worker is leaving.

    `client.stop()` does two things and only the second one is expensive: it
    signals the poll thread, then waits for it. Called where the old code called
    it — after `asyncio.run` has returned — the waiting starts only once
    everything else is already over, and the seconds the teardown took are
    seconds the long poll could have spent unwinding. So the signal goes out
    from the SIGTERM handler (`begin`) and the waiting happens at the end
    (`finish`), and the two overlap by however long the Worker took to put
    itself away.

    `begin` is called from a signal handler, so it does the least it can: start
    one daemon thread. `finish` is idempotent and safe to call on a Worker that
    never got as far as having a client.
    """

    def __init__(self, budget: float = STOP_BUDGET_S):
        self.budget = float(budget)
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._begun = False
        self._began = 0.0
        #: Reentrant, because the signal handler runs *on the main thread*, at
        #: whatever bytecode it had reached: a plain lock held by `finish` when
        #: the signal lands would be a Worker deadlocked by its own stop.
        self._lock = threading.RLock()

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<RelayStop budget={self.budget} begun={self._begun}>"

    def watch(self, client) -> None:
        """The client to stop. Called once, as soon as there is one."""
        with self._lock:
            self._client = client

    def begin(self) -> None:
        """Tell the client to stop, and do not wait for it here. Once only."""
        with self._lock:
            if self._client is None or self._begun:
                return
            client = self._client
            self._begun = True
            self._began = time.monotonic()
            self._thread = threading.Thread(
                target=lambda: client.stop(timeout=self.budget),
                name="agent-connect-relay-stop", daemon=True)
            self._thread.start()

    def finish(self) -> None:
        """Wait out what is left of the budget, so the guard can be released."""
        self.begin()
        thread = self._thread
        if thread is None:
            return                          # there was never a client to stop
        thread.join(max(0.0, self.budget - (time.monotonic() - self._began)) + 0.5)


USAGE = f"""usage: agent-connect [--config PATH] [--export-config]

Runs your local agent against the Tasks the relay client pulls for one Agent
Identity. Everything it reads is a setting, documented in README.md § Settings,
and every setting can be given in the environment or in a config file.

  --config PATH    the config file to read. Default: {CONFIG_ENV} if it is set,
                   otherwise {DEFAULT_PATH} if it exists. Environment variables
                   win over the file.
  --export-config  print that same config file as shell `export` lines and exit,
                   for a launcher that has to start something else with the same
                   settings. This is why there is no second parser anywhere.
"""


def parse_args(argv: List[str]) -> Tuple[Optional[str], bool]:
    """`(config path, export-only)` from a command line.

    Two flags, and no more: a flag per setting would be a third place for a
    setting to live, and there are already two too many. `--help` earns its
    place by being the answer to `--export-config` existing at all — a program
    with flags that treats `--help` as an unknown argument is worse than one
    with no flags.
    """
    args = list(argv)
    path, export = None, False
    while args:
        arg = args.pop(0)
        if arg in ("-h", "--help"):
            print(USAGE)
            raise SystemExit(0)
        if arg == "--export-config":
            export = True
        elif arg == "--config":
            if not args:
                raise SystemExit("agent-connect: --config needs a path")
            path = args.pop(0)
        elif arg.startswith("--config="):
            path = arg.partition("=")[2]
        else:
            raise SystemExit(f"agent-connect: unknown argument {arg!r}\n\n{USAGE}")
    return path, export


def main(argv: Optional[List[str]] = None) -> None:
    # A service manager stops a Worker with SIGTERM, and the default disposition
    # is death without a word — which an observer could only read as staleness,
    # a minute later. Turning it into an ordinary exit lets the handlers below
    # write "stopped" while the observer is still watching. The cost is real and
    # small: `sys.exit` is raised at whatever bytecode the interpreter is on, so
    # a Task result being written at that instant can be left truncated, where
    # an unhandled SIGTERM would have killed the process between syscalls. One
    # Task's result file against every observer's ability to tell a stop from a
    # crash; the archived result is also not the answer, which has already gone
    # to the room up the Ladder.
    #
    # The handler also tells the Relay Client to stop *here*, at the signal,
    # rather than at the `finally` below: the poll thread it has to wait for is
    # inside a long poll, and the seconds this process spends putting itself
    # away are seconds that wait can overlap with. See `RelayStop`.
    stopping = RelayStop()

    def terminated(*_):
        stopping.begin()
        sys.exit(0)

    signal.signal(signal.SIGTERM, terminated)
    status: Optional[StatusFile] = None
    try:
        # ---- the startup order is a contract ------------------------------
        # Settings first: a config file may name the workspace, so nothing that
        # reads a setting may run before it — and every setting an Adapter reads
        # for itself sees the same environment whether the operator exported it
        # or wrote it down. What the environment already says is never
        # overwritten; see `agent_connect.config`. The workspace is second
        # because everything the Worker owns on disk hangs off it, the status
        # file third because it lives in the workspace, and only then anything
        # that can fail. A config file that cannot be read is therefore reported
        # on stderr with no status file to its name, which is right: the config
        # is what says where the status file goes.
        config_path, export_only = parse_args(
            sys.argv[1:] if argv is None else list(argv))
        try:
            if export_only:
                export_config(config_path)
                return
            load_config(config_path)
        except ConfigError as exc:
            raise SystemExit(f"agent-connect: {exc}")
        ws = _ws()
        status = status_from_env()
        status.starting(workspace=str(ws))
        # Checked here, before anything that could refuse for another reason: it
        # is the name of the directory this Worker's durable state lives in, and
        # a Worker with a mistyped name and no token used to be told about the
        # token, fix that, and only then be told about the name. A setting that
        # is *wrong* outranks one that is *missing*.
        try:
            relay_client.instance()
        except ValueError as exc:
            raise SystemExit(f"agent-connect: {exc}")
        adapter_name = os.environ.get("AGENT_CONNECT_ADAPTER")
        if not adapter_name:
            raise SystemExit(
                "set AGENT_CONNECT_ADAPTER (e.g. codex) — in the environment, or in "
                "a config file (--config PATH; see README.md § Settings)"
            )
        try:
            adapter = get_adapter(adapter_name)
        except KeyError as exc:
            raise SystemExit(f"agent-connect: {exc}")
        status.starting(adapter=adapter_name)

        # The Relay Client, constructed before the Adapter's preflight and started
        # after it. Constructing it is what validates the credential, and a bad
        # token should be a refusal in the first second rather than in the
        # forty-sixth, after an ACP bridge has finished proving the Local Agent
        # is fine. Starting it is what leases work, and leasing work the Worker
        # cannot yet run would be a lease burnt on a Worker that may be about to
        # refuse to start.
        #
        # The working directory is resolved *first*, because it is one of the
        # construction facts: it is the egress allowlist's root, and the roots
        # are fixed when the client is built and never afterwards. A Worker
        # that discovered where its agent works later would have to widen an
        # allowlist to use it, which is the one thing an allowlist may not do.
        repo = str(_resolve_repo())
        try:
            client = relay_client.from_env(ws, repo=repo)
        except Exception as exc:  # noqa: BLE001 — a refusal, said in words
            raise SystemExit(f"agent-connect: {exc}")
        if client is None:
            raise SystemExit(
                "set AGENT_CONNECT_TOKEN to your agent identity's relay token "
                "from the Agent Portal — in the environment, or in a config "
                "file (--config PATH; see README.md § Settings). Without it "
                "this Worker has no way to be given any work at all"
            )
        # The connection's state, in this Worker's own file beside its own
        # facts — the layered status the spec asks a consumer to compose. Hooked
        # up and primed before the first poll, so a Worker that is still
        # starting already says what it is pointed at.
        client.on_status(status.relay)
        status.relay(client.snapshot())
        # From here on a SIGTERM has something to stop, and the sooner it says
        # so the more of the wait it can spend on work that is already happening.
        stopping.watch(client)

        agent = preflight(adapter, status)
        poll = float(os.environ.get("AGENT_CONNECT_POLL", "1.0"))

        # The Ladder: the placeholder and its edits are Room Ops, asked for
        # through the client the Worker already built — one bearer, one gateway,
        # one speaker. There is no second credential read anywhere above this
        # line, and nothing here knows a URL.
        ops = room_ops_for(client)
        settings = LadderSettings.from_env()

        detail = f"adapter={adapter_name} repo={repo} ws={ws}"
        print(f"agent-connect worker: {detail}")
        status.serving(detail=detail, agent=agent, repo=repo)
        client.start()
        try:
            asyncio.run(
                serve(adapter, repo, client, poll, ops, settings,
                      status=status)
            )
        finally:
            # Waited on, for as long as `STOP_BUDGET_S` says and no longer,
            # because the wait is what releases the singleton guard: a stop that
            # gives up costs the *next* Worker three minutes of standby before
            # it may poll at all. Anything the wait does abandon is safe —
            # anything already answered is in the library's journal and is
            # re-POSTed by the next run, and anything accepted and unanswered is
            # re-served by the broker and re-completed rather than re-executed.
            stopping.finish()
    except KeyboardInterrupt:
        _record(status, StatusFile.stopped, "interrupted")
        raise
    except SystemExit as exc:
        # A falsy code is the SIGTERM above, or an ordinary `--help`-shaped
        # exit. Anything else is a refusal to start — and `SystemExit` carries
        # two different things there: a string, which is the sentence the
        # operator has to act on and which an observer needs verbatim, or an
        # integer, which is a status and explains nothing on its own.
        if not exc.code:
            _record(status, StatusFile.stopped, "terminated")
        elif isinstance(exc.code, int):
            _record(status, StatusFile.error, f"exited with status {exc.code}")
        else:
            _record(status, StatusFile.error, str(exc.code))
        raise
    except BaseException as exc:  # noqa: BLE001 — report, then die as before
        _record(status, StatusFile.error, f"{type(exc).__name__}: {exc}")
        raise


def _record(status: Optional[StatusFile], method, detail: str) -> None:
    """Write an ending, if there is a status file yet to write it to.

    There may not be: everything before `status_from_env()` — a config file
    that cannot be read, most of all — fails with nowhere to say so, and
    inventing a path to report it at would be reporting it in the wrong place.
    """
    if status is not None:
        method(status, detail)


if __name__ == "__main__":
    main()
