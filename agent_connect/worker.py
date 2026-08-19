"""agent-connect worker.

Watches a workspace `tasks/` dir (populated by the AG2 Space relay client),
runs the configured agent adapter on each task, and writes `results/`. The relay
client handles all Matrix transport + posting back — this only turns a task into
an agent run.

Tasks are processed **concurrently across rooms** and serialised within one
Session: a ten-minute request in one room no longer silences every other room,
while two Tasks that share a Session still run one at a time, because only one
Turn at a time may be open on a Session.

**Settings are documented in `README.md` § Settings, and only there.** This
docstring used to carry a second list of the same environment variables, which
went out of date the moment an Adapter grew one of its own; there is now one
home for all of them and `test_acp_settings.py` fails if a setting exists in the
package and not in that section.

Task files are the AG2 Space convention: `tasks/task-<id>.txt` with `id:`,
`task:` and `access_tier:` lines. Results go to `results/task-<id>.txt`.
"""
from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .adapters import get as get_adapter
from .attachments import parse as parse_attachments
from .events import TurnContext
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


# Header keys the AG2 Space relay writes (ag2-sparrow's task-file layout).
# The relay deliberately writes `access_tier` as the LAST header — after
# `task:` — as an anti-forgery invariant, so the parser must keep reading
# headers after the task line instead of treating everything to EOF as body.
#
# Any key missing from this set lands in the prompt body instead, so the Local
# Agent reads relay metadata as if a person had typed it. The attachment keys
# are written after `task:`, which is how they came to leak; the parser itself
# is order-independent, so their position is relay trivia, not an invariant.
_HEADER_KEYS = {
    "id", "timestamp", "task", "source", "channel_id", "chat_id", "room_name",
    "sender_name", "user_id", "priority", "interaction_type", "access_tier",
    "collaborator", "reply_to_event", "reply_to_me", "thread_ts",
    # attachments
    "content_modalities", "media_form", "attachments",
    # provenance
    "source_message_id", "platform_card",
}


def parse_task(text: str) -> dict:
    """Parse an AG2 Space task file.

    Headers are `key: value` lines with a known key; `task:` starts the body,
    which may span multiple lines and ends at the next known-header line.
    The relay sanitizes newlines out of wire fields, so a message body cannot
    fabricate a header line of its own. Defense-in-depth on top of that:
    if more than one `access_tier` header appears, fail closed to "other".

    `access_tier`, `task` and `source_message_id` always come back, because
    each has a meaningful default — downstream threading reads the source
    message identifier on every Task, including ones the relay wrote without
    one. Every other known header is returned verbatim under its own key only
    when the relay wrote it, so read those with `.get()`.
    """
    fields: dict = {"access_tier": "other", "task": "", "source_message_id": ""}
    body: list = []
    tiers: list = []
    in_body = False
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        key = k.strip()
        if sep and key in _HEADER_KEYS and not line[:1].isspace():
            in_body = key == "task"
            if key == "task":
                body.append(v.lstrip())
            elif key == "access_tier":
                tiers.append(v.strip())
            else:
                fields[key] = v.strip()
        elif in_body:
            body.append(line)
    fields["task"] = "\n".join(body).strip()
    if len(tiers) == 1:
        fields["access_tier"] = tiers[0]
    # zero headers → default "other"; multiple → forged/ambiguous → "other"
    return fields


def turn_context(fields: dict, task_id: str, repo: str) -> TurnContext:
    """Build the context one Turn travels with, from a parsed Task.

    The Sandbox is derived here, from the Access Tier the parser fought to
    establish — never from anything else in the file, all of which the sender
    can write. The room identifier is the relay's `channel_id` (a Matrix room
    id); `room_name` is the human label and is carried for display only.

    Attachments come from the `attachments:` **header** and from nowhere else.
    The relay also dual-writes a `[File attached: <path>]` line into the body,
    and that line is left exactly where it is: it is part of what the person
    sees themselves as having sent, and a path read out of a body would be a
    path the sender chose. See `agent_connect.attachments`.
    """
    tier = fields.get("access_tier", "other")
    return TurnContext(
        prompt=fields.get("task", "").strip(),
        task_id=task_id,
        room=fields.get("channel_id") or fields.get("chat_id") or "",
        room_name=fields.get("room_name", ""),
        access_tier=tier,
        sender_name=fields.get("sender_name", ""),
        user_id=fields.get("user_id", ""),
        source_message_id=fields.get("source_message_id", ""),
        sandbox=tier_to_sandbox(tier),
        cwd=repo,
        attachments=parse_attachments(fields.get("attachments", "")),
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
    task_path: Path,
    adapter,
    repo: str,
    results_dir: Path,
    sessions: dict = None,
    ops=None,
    settings=None,
) -> None:
    """Handle one Task file: parse it, run a Turn, write the result once.

    `sessions` — when given — maps a Session key to the `SessionQueue` that
    keeps one Turn at a time open on it. Tasks with different keys never
    contend, which is what makes two rooms concurrent; Tasks with the same key
    queue, and **the person is told they are queued** rather than left
    wondering whether their message arrived. Without a registry there is no
    serialisation at all, which is what a single-Task caller wants.

    `ops` is the relay to climb the Ladder on, shared across Tasks; `settings`
    is how much of it to climb. Both may be `None`, and then the result body is
    the answer and the relay client posts it, as before.
    """
    task_id = task_path.stem  # "task-<id>"
    result_path = results_dir / f"{task_id}.txt"
    if result_path.exists():
        return
    fields = parse_task(task_path.read_text(errors="replace"))
    ctx = turn_context(fields, task_id, repo)
    if not ctx.prompt:
        result_path.write_text("[no-send] empty task\n")
        return
    # The results directory is also the *outgoing* directory: it is the one place
    # the transport's send allowlist trusts, so a file the agent produced leaves
    # this machine by being staged there and named in the result body. The Worker
    # uploads nothing itself — see `agent_connect.outgoing`.
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
        output = await run_turn(adapter, ctx, ops, settings, reporter)
    result_path.write_text(output + "\n")


async def handle_one(
    task_path: Path,
    adapter,
    repo: str,
    results_dir: Path,
    sessions: dict = None,
    ops=None,
    settings=None,
) -> None:
    """`process_one`, with the guarantee that it cannot take the Worker down.

    One Task failing is one Task's problem. The failure is written where the
    relay looks for the answer, so the person who asked hears something back
    rather than nothing.
    """
    try:
        await process_one(task_path, adapter, repo, results_dir, sessions, ops, settings)
    except Exception as e:  # noqa: BLE001 — never die on one bad task
        result_path = results_dir / f"{task_path.stem}.txt"
        if not result_path.exists():
            result_path.write_text(f"agent-connect: worker error: {e}\n")


async def serve(
    adapter,
    repo: str,
    results_dir: Path,
    tasks_dir: Path,
    poll: float,
    ops=None,
    settings=None,
    *,
    status: Optional[StatusFile] = None,
) -> None:
    """Scan for Tasks forever, starting each one without waiting for the last.

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
    seen: set = set()
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
    try:
        while True:
            for task_path in sorted(tasks_dir.glob("task-*.txt")):
                if task_path.name in seen:
                    continue
                seen.add(task_path.name)
                fut = asyncio.ensure_future(
                    handle_one(task_path, adapter, repo, results_dir, sessions,
                               ops, settings)
                )
                running.add(fut)
                started[fut] = time.monotonic()
                fut.add_done_callback(running.discard)
                fut.add_done_callback(started.pop)
            await asyncio.sleep(poll)
    finally:
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


def main() -> None:
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
        # Settings first (a config file may name the workspace — that is
        # byo/03's half and it inserts above this line), the workspace second,
        # the status file third because it lives in the workspace, and only
        # then anything that can fail. A config file that cannot be read is
        # therefore reported on stderr with no status file to its name, which
        # is right: the config is what says where the status file goes.
        ws = _ws()
        status = status_from_env()
        status.starting(workspace=str(ws))
        adapter_name = os.environ.get("AGENT_CONNECT_ADAPTER")
        if not adapter_name:
            raise SystemExit("set AGENT_CONNECT_ADAPTER (e.g. codex)")
        try:
            adapter = get_adapter(adapter_name)
        except KeyError as exc:
            raise SystemExit(f"agent-connect: {exc}")
        status.starting(adapter=adapter_name)
        agent = preflight(adapter, status)
        repo = os.environ.get("AGENT_CONNECT_REPO") or os.getcwd()
        poll = float(os.environ.get("AGENT_CONNECT_POLL", "1.0"))

        tasks_dir = ws / "tasks"
        results_dir = ws / "results"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        # The Ladder, if this Worker holds a relay token: the placeholder and its
        # edits are Room Ops, and without a token there is nobody to ask for one.
        ops = room_ops_from_env()
        settings = LadderSettings.from_env()

        detail = f"adapter={adapter_name} repo={repo} ws={ws}"
        print(f"agent-connect worker: {detail}")
        status.serving(detail=detail, agent=agent, repo=repo)
        asyncio.run(
            serve(adapter, repo, results_dir, tasks_dir, poll, ops, settings,
                  status=status)
        )
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
