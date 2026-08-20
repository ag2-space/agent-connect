"""agent-connect worker.

Drains the Relay Client's Task queue, runs the configured Adapter on each Task,
and answers it — `complete` with what the Local Agent produced, `reject` for a
Task nothing could ever answer. The transport is a library this repository owns
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
that seam runs on a thread rather than on the event loop: the queue read on a
daemon thread of its own, `complete` and `reject` on the default executor. That
is not tidiness. Cadence is a correctness property on the other side of the seam
— a poll thread that stops polling loses its leases and its work comes back as
duplicate delivery — and a blocked event loop up here is a `complete` that never
happens down there.

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

from . import relay as relay_client
from .adapters import get as get_adapter
from .config import CONFIG_ENV, DEFAULT_PATH, ConfigError
from .config import export as export_config
from .config import load as load_config
from .events import Attachment, TurnContext
from .outgoing import Outbox
from .pending import queue_for
from .reporter import LadderSettings, TurnReporter
from .roomops import room_ops_from_env
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
    is no `attachments:` header any more either, because there is no file for a
    header to be written into. The relay used to dual-write a
    `[File attached: <path>]` line into the body as well; whatever of that a
    sender types is left exactly where it is, because a path read out of a body
    would be a path the sender chose.

    Tolerant of a library that has not grown the tuple yet: transport-seam
    ticket 03 is media ingress and ticket 08 is where `agent_connect.attachments`
    stops parsing and starts consuming these directly. Until both land, a Task
    carries none and a Turn sees none.
    """
    return tuple(getattr(task, "attachments", ()) or ())


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
    """
    tier = attested_tier(task.access_tier)
    return TurnContext(
        prompt=(task.body or "").strip(),
        task_id=task.id,
        room=task.room_id,
        room_name=task.room_name,
        access_tier=tier,
        sender_name=task.sender_name,
        user_id=task.user_id,
        source_message_id=task.source_message_id,
        sandbox=tier_to_sandbox(tier),
        cwd=repo,
        attachments=task_attachments(task),
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
    results_dir: Path,
    sessions: dict = None,
    ops=None,
    settings=None,
) -> str:
    """Handle one delivered Task and return the body that answers it.

    Returns `""` for a Task nothing could answer — an empty prompt, which is
    also what a body that was *only* an unsigned metadata block degrades to
    after the library quarantines it. `handle_one` turns that into the reject.
    Everything else comes back as a body: the answer, the terminal marker when
    the answer already went to the room up the Ladder, or the structured
    rejection when the Turn produced nothing to say.

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
    # The results directory is also the *outgoing* directory: it is the one place
    # the transport's send allowlist trusts, so a file the agent produced leaves
    # this machine by being staged there and named in the result body. The Worker
    # uploads nothing itself — see `agent_connect.outgoing`. Transport-seam
    # ticket 09 retires the staging airlock for the library's allowlisted-path
    # egress; until then this route is unchanged.
    reporter = TurnReporter(ops, settings, outbox=Outbox(results_dir))

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
    results_dir: Path,
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
    dead-letter five attempts later. So there is exactly one exit here, and it
    is one of those two calls.

    One Task failing is still one Task's problem: the failure becomes the body,
    so the person who asked hears something back rather than nothing, and the
    drain loop above keeps turning.

    A `reject` is reserved for input nothing could ever answer, which on this
    side of the seam is one thing: a Task with no prompt in it. Re-serving that
    produces the same nothing five times over, and the broker's dead-letter park
    is where the protocol says it goes — this is the flow sparrow never
    implemented and the reason the old code could only write `[no-send] empty
    task` and hope. The broker posts the terminal-failure notice; the Worker
    posts none, exactly as with the `TurnReporter`'s structured rejection.

    `client` may be `None` — a test, or a caller driving one Task by hand — and
    then the answer travels back as the return value and nothing is told to any
    broker. The body is returned either way, and `""` means the Task was
    rejected.
    """
    try:
        body = await process_one(task, adapter, repo, results_dir, sessions, ops, settings)
    except Exception as e:  # noqa: BLE001 — never die on one bad task
        body = f"agent-connect: worker error: {e}"
    if not body.strip():
        # Belt and braces: `process_one` returns "" only for an empty prompt,
        # and the reporter's endings are never blank. A blank body would be
        # refused by `complete` anyway (H5: an empty answer is "not ready", not
        # an answer), so it must not be able to reach it.
        await _answer(client, "reject", task.id, EMPTY_TASK)
        return ""
    await _answer(client, "complete", task.id, body)
    return body


async def _answer(client, how: str, task_id: str, payload: str) -> None:
    """Tell the library how one Task ended, without blocking the event loop.

    The call is sync and does I/O — `complete` writes the journal and then
    POSTs — so it goes to a thread. It is also the last thing standing between
    a finished Turn and the person who asked, so a failure here is said loudly
    on stderr and nowhere else: raising would take down the drain loop, and
    swallowing it silently would lose an answer without a trace. The retry is
    the library's (F5: a result is retained until its POST succeeds), which is
    why there is none here.
    """
    if client is None:
        return
    try:
        await asyncio.to_thread(getattr(client, how), task_id, payload)
    except Exception as exc:  # noqa: BLE001 — one Task's answer, not the loop
        print(f"agent-connect: could not {how} task {task_id}: {exc}",
              file=sys.stderr, flush=True)


async def serve(
    adapter,
    repo: str,
    results_dir: Path,
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

    def pump() -> None:
        while not stop.is_set():
            task = client.next_task(poll)
            if task is None:
                continue                    # nothing arrived in `poll` seconds
            try:
                loop.call_soon_threadsafe(inbound.put_nowait, task)
            except RuntimeError:
                # The loop closed under us — the Worker is going away. The Task
                # stays accepted-and-unanswered in the library's journal, which
                # is what the broker re-serves; dropping it here is the one
                # thing the queue is documented to be allowed to do, because it
                # is a handoff and not the durability boundary.
                return

    threading.Thread(target=pump, name="agent-connect-queue", daemon=True).start()
    try:
        while True:
            task = await inbound.get()
            fut = asyncio.ensure_future(
                handle_one(task, adapter, repo, results_dir, sessions,
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
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
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

        # The transport, constructed before the Adapter's preflight and started
        # after it. Constructing it is what validates the credential, and a bad
        # token should be a refusal in the first second rather than in the
        # forty-sixth, after an ACP bridge has finished proving the Local Agent
        # is fine. Starting it is what leases work, and leasing work the Worker
        # cannot yet run would be a lease burnt on a Worker that may be about to
        # refuse to start.
        try:
            client = relay_client.from_env(ws)
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

        agent = preflight(adapter, status)
        repo = str(_resolve_repo())
        poll = float(os.environ.get("AGENT_CONNECT_POLL", "1.0"))

        results_dir = ws / "results"
        results_dir.mkdir(parents=True, exist_ok=True)

        # The Ladder: the placeholder and its edits are Room Ops. Still asked
        # for over `roomops.py` rather than through the client — transport-seam
        # ticket 09 is where that seam closes too.
        ops = room_ops_from_env()
        settings = LadderSettings.from_env()

        detail = f"adapter={adapter_name} repo={repo} ws={ws}"
        print(f"agent-connect worker: {detail}")
        status.serving(detail=detail, agent=agent, repo=repo)
        client.start()
        try:
            asyncio.run(
                serve(adapter, repo, results_dir, client, poll, ops, settings,
                      status=status)
            )
        finally:
            # Barely waited on, and deliberately. The poll thread is a daemon
            # sitting in a 25-second long poll with a 35-second socket timeout,
            # so joining it properly would turn every SIGTERM into half a minute
            # of a service manager waiting — and then killing us before
            # `stopped` could be written, which is the one thing this handler
            # exists to get into the file. Nothing is lost by not waiting:
            # anything already answered is in the library's journal and is
            # re-POSTed by the next run, and anything accepted and unanswered is
            # re-served by the broker and re-completed rather than re-executed.
            client.stop(timeout=2.0)
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
