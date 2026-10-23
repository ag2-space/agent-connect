"""A queued outcome survives the Worker's own startup — against the real AG2 Assistant.

The Worker's first connection to a dialled agent is `preflight`: dial, `initialize`,
hang up — with no notification handler and no Room Ops yet. The agent
(ag2ai/ag2-assistant#171) flushes its outbox to a newly bound client, and the first cut
flushed it to *every* client: the probe took what had queued while the Worker was
down and dropped it, and the standing link that followed found nothing. The queue's one
reason to exist — the Worker restarting — was the case it lost.

This suite drives the Worker's actual startup sequence, `worker.preflight` and then
`worker.serve` (which installs the Room Ops notifier and starts the listener link),
against the actual Assistant listener (`ProfileManager.start_acp_listener`,
`serve_listener`, `Outbound`), and checks that an outcome queued before startup reaches
Room Ops — after preflight, not into it — and that one pushed while the Worker sits idle
between Turns arrives too. The fake-door suite (`test_acp_notify`) pins each side's
behaviour; this one pins the two together.

Cross-repository: it needs the Assistant importable. Run it with the Assistant's own
interpreter, which has `acp[http]` and `ag2` installed, pointing at its checkout:

    AG2_ASSISTANT_REPO=../ag2-assistant ../ag2-assistant/.venv/bin/python \\
        tests/test_cross_assistant_startup.py

Without a reachable checkout it skips (and says so) rather than failing, like the wire
half of `test_acp_notify`. The Assistant's suite runs this same file from its side.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root (and the relay client) on sys.path
import asyncio
import contextlib
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def _skip(why: str) -> None:
    print(f"  --   skipped ({why})")
    print("PASS — cross-repo assistant startup (skipped)")
    sys.exit(0)


def _assistant_repo() -> Path | None:
    raw = os.environ.get("AG2_ASSISTANT_REPO")
    candidates = [Path(raw)] if raw else [_bootstrap.ROOT.parent / "ag2-assistant"]
    for candidate in candidates:
        if (candidate / "src" / "assistant").is_dir():
            return candidate.resolve()
    return None


repo = _assistant_repo()
if repo is None:
    _skip("no AG2 Assistant checkout: set AG2_ASSISTANT_REPO")
sys.path.insert(0, str(repo / "src"))
sys.path.insert(1, str(repo))
try:
    from assistant.acp.auth import KEY_ENV
    from assistant.profiles import ProfileRegistry
    from tests.support.apps import make_manager, make_paths  # the Assistant's own helpers
    from tests.support.fakes import ScriptedModels, fake_summary_factory
except ImportError as exc:
    raise RuntimeError("run the cross-repository check with the Assistant's interpreter") from exc

from _taskqueue import FakeClient  # noqa: E402

from agent_connect import worker  # noqa: E402
from agent_connect.adapters.acp import AcpAdapter  # noqa: E402
from agent_connect.sessions import SessionStore  # noqa: E402

ROOM = "!room:example"
QUEUED = "✅ nightly: done"
LIVE = "✅ weekly: done"


class FakeOps:
    """Room Ops that record instead of posting."""

    def __init__(self):
        self.sent = []

    async def message(self, room, body, mentions=None):
        self.sent.append((room, body))
        return "$event"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_listening(port: int, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.05)
        else:
            writer.close()
            return


async def _until(cond, timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.02)
    return True


async def finish_task(manager, profile, connection, name):
    """Run a persisted task through the profile's actual notifier wiring."""
    tasks = manager.get(profile).tasks
    task = await tasks.create_task(
        name=name, prompt="report", origin_channel=connection, origin_chat=ROOM
    )
    run = await tasks.start_run(task["id"])
    assert run is not None
    await asyncio.wait_for(tasks._jobs_done(), 5)
    assert (await tasks.get_run(run.id))["status"] == "completed"


def _worker_startup(adapter, relay, ops, preflighted, go, done, outcome) -> None:
    """The Worker's startup as `main` runs it: `preflight` first, `serve` after.

    On its own thread with its own loop, because the Assistant listener owns the
    main one — exactly the two-process shape the real deployment has."""
    try:
        outcome["agent"] = worker.preflight(adapter)
    except SystemExit as exc:
        outcome["problem"] = str(exc)
        preflighted.set()
        return
    preflighted.set()
    go.wait(15)

    async def serve_until_done():
        task = asyncio.ensure_future(
            worker.serve(adapter, "/repo", relay, 0.05, ops, None)
        )
        while not done.is_set():
            await asyncio.sleep(0.02)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(serve_until_done())


async def scenario() -> dict:
    results: dict = {}
    with tempfile.TemporaryDirectory() as tmp:
        paths = make_paths(Path(tmp))
        pid = ProfileRegistry(paths).create_profile("Work", "#109e91").id
        # A credentialed profile (a key for whichever provider the default profile
        # resolves to), so the listener advertises no auth methods and the Worker's
        # preflight passes the way it does against a configured install.
        keys = {name: "sk-test" for name in set(KEY_ENV.values())}
        manager = make_manager(
            paths, model_factory=ScriptedModels(), env=keys,
            summary_factory=fake_summary_factory(summary="done"),
        )
        await manager.start()
        try:
            port, token = _free_port(), "listener-token"
            cid = manager._connections.create_acp_connection(  # noqa: SLF001
                pid, port=port, token=token
            ).connection.id
            results["listener"] = await manager.start_acp_listener(cid)
            await _wait_listening(port)
            out = manager.acp_outbound[cid]

            # The Worker is down; a scheduled task finishes. Queued, durably.
            await finish_task(manager, pid, cid, "nightly")
            results["queued_before"] = [p["text"] for p in out.pending()]

            adapter = AcpAdapter(
                url=f"ws://127.0.0.1:{port}/acp", token=token,
                store=SessionStore(Path(tmp) / "sessions.json"),
            )
            relay, ops = FakeClient(), FakeOps()
            preflighted, go, done, outcome = (
                threading.Event(), threading.Event(), threading.Event(), {},
            )
            thread = threading.Thread(
                target=_worker_startup,
                args=(adapter, relay, ops, preflighted, go, done, outcome), daemon=True,
            )
            thread.start()
            await asyncio.to_thread(preflighted.wait, 20)
            await asyncio.sleep(0.3)  # long enough for a wrongly flushed outbox to be gone
            results["preflight"] = dict(outcome)
            results["queued_after_preflight"] = [p["text"] for p in out.pending()]
            results["posted_after_preflight"] = list(ops.sent)
            results["receivers_after_preflight"] = out.connected

            go.set()  # serve: notifier installed, listener link dialled
            results["delivered"] = await _until(lambda: len(ops.sent) >= 1)
            results["posted"] = list(ops.sent)
            results["queued_after_serve"] = [p["text"] for p in out.pending()]
            results["receivers_while_serving"] = out.connected

            # Idle between Turns: another task finishes.
            await finish_task(manager, pid, cid, "weekly")
            results["live"] = await _until(lambda: len(ops.sent) >= 2)
            results["posted_live"] = list(ops.sent)
            done.set()
            await asyncio.to_thread(thread.join, 15)
            assert not thread.is_alive(), "worker did not shut down"
        finally:
            await manager.close()
    return results


r = asyncio.run(scenario())
check(r["listener"][0], f"the real Assistant listener is up ({r['listener'][1]})")
check(r["queued_before"] == [QUEUED], "an outcome pushed while the Worker is down waits in its outbox")
check("problem" not in r["preflight"],
      f"the Worker's real preflight passes against it ({r['preflight'].get('problem', 'ok')})")
check(r["queued_after_preflight"] == [QUEUED],
      "and leaves the outbox alone: the probe is not handed the queue")
check(r["posted_after_preflight"] == [] and r["receivers_after_preflight"] == 0,
      "nothing was posted and nothing is attached after the probe hung up")
check(r["delivered"] and r["posted"] == [(ROOM, QUEUED)],
      "serve installs the notifier and dials the link, and the queued outcome reaches Room Ops")
check(r["queued_after_serve"] == [] and r["receivers_while_serving"] == 1,
      "the outbox is drained, by the one receiver that asked")
check(r["live"] and r["posted_live"] == [(ROOM, QUEUED), (ROOM, LIVE)],
      "an outcome finishing while the Worker is idle arrives live, in order")

print()
if fails:
    print(f"FAIL — {fails} failing")
    sys.exit(1)
print("PASS — cross-repo assistant startup green")
