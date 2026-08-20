"""Queue fixtures: a Task as the Relay Client delivers it, and a client to
answer it to.

The suite used to build Tasks by writing `tasks/task-<id>.txt` and read answers
back out of `results/task-<id>.txt`, because that was the seam. It is not the
seam any more: the Worker takes Tasks off the library's in-memory queue and
answers them with `complete` / `reject`. So the fixtures move with it — a test
puts a `Task` on a queue and asserts on what the client was told, which is
exactly what the outside world can see.

`FakeClient` mirrors `RelayClient`'s consumer surface and nothing below it. It
is not a fake broker: the library's own suite has one of those
(`relay-client/tests/fake_broker.py`) and tests the wire against it. What is
being tested up here is the Worker, and the Worker's whole knowledge of the wire
is these five methods.

    import _bootstrap  # noqa: F401
    from _queue import FakeClient, task

    client = FakeClient()
    body = asyncio.run(handle_one(task("t1", "do it"), adapter, "/repo",
                                  results, client=client))
    assert client.answer("t1") == body
"""
from __future__ import annotations

import os
import queue
from typing import Optional

import _bootstrap
from ag2_relay_client import Task
from ag2_relay_client.envelope import parse_task

#: A credential a child Worker can be started with. Combined and well-formed —
#: the onboarding token carries its own gateway and the library has no default
#: to fall back on (I3) — and pointed at a port nothing is listening on, so a
#: real `python3 -m agent_connect` constructs a real Relay Client, tries to
#: poll, fails, and goes on serving. Which is the point: a Worker whose broker
#: is unreachable is a Worker having a bad day, not a Worker that failed.
CHILD_TOKEN = "http://127.0.0.1:9/relay|test-secret"


def child_env(**extra) -> dict:
    """The environment a real `python3 -m agent_connect` child process needs.

    Two things the parent has for free and a child does not: the Relay Client on
    the import path — it is a separate distribution built from this same
    repository, and `_bootstrap` explains why the suite runs against the working
    tree — and a credential, without which the Worker refuses to start, because
    without one it has no way of being given any work at all.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(
            (str(_bootstrap.ROOT), str(_bootstrap.RELAY_CLIENT))),
        "AGENT_CONNECT_TOKEN": CHILD_TOKEN,
    }
    env.update(extra)
    return env


class QueuedTask(Task):
    """A delivered Task, with the attachment tuple media ingress will add.

    `Task.__slots__` has no `attachments` yet — transport-seam ticket 03 is
    where the library grows one, and ticket 08 is where `agent_connect`
    consumes it properly. The Worker already reads whatever the delivered Task
    carries, so this subclass is how a fixture hands it some in the meantime.
    It exists here rather than in the package because it is a stand-in for the
    library's future shape, and a stand-in that shipped would outlive the thing
    it stands in for.
    """

    __slots__ = ("attachments",)

    def __init__(self, *args, attachments=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.attachments = tuple(attachments)


def task(task_id: str, body: str = "do it", room: str = "", tier: str = "owner",
         attachments=(), **fields) -> QueuedTask:
    """One Task, as the library would have delivered it.

    Built through the library's own `parse_task` wherever the field is a wire
    field, so a fixture cannot quietly describe an envelope the broker could not
    send — a Task whose `access_tier` is `team` is spelled here the way it
    arrives, and reaches `attested_tier` the way it arrives. `attachments` is
    the one thing added on top, and `QueuedTask` says why.
    """
    raw = {"id": task_id, "task": body, "access_tier": tier}
    if room:
        raw["channel_id"] = room
    raw.update(fields)
    parsed = parse_task(raw)
    if parsed is None:
        raise ValueError(f"not a Task the library would deliver: {raw!r}")
    return QueuedTask(
        **{name: getattr(parsed, name) for name in Task.__slots__},
        attachments=attachments,
    )


class FakeClient:
    """The Relay Client's consumer surface, in memory.

    Records what it was told rather than posting it, and refuses what the real
    one refuses: an empty result body is "not ready", not an answer (H5), and a
    test that let one through would be testing a `complete` the library would
    have raised on.
    """

    def __init__(self):
        self.tasks: "queue.Queue" = queue.Queue()
        #: `(id, body)` in the order the Worker completed them.
        self.completed: list = []
        #: `(id, error_code)` in the order the Worker rejected them.
        self.rejected: list = []
        #: Whatever `on_status` was given, so a test can drive the hook itself.
        self.hook = None
        self.started = 0
        self.stopped = 0

    # -- what a test does to it ---------------------------------------------

    def offer(self, *tasks) -> "FakeClient":
        """Deliver these Tasks, in this order."""
        for one in tasks:
            self.tasks.put(one)
        return self

    def answer(self, task_id: str) -> Optional[str]:
        """The body this Task was completed with, or `None`."""
        for wire_id, body in self.completed:
            if wire_id == task_id:
                return body
        return None

    def refusal(self, task_id: str) -> Optional[str]:
        """The error code this Task was rejected with, or `None`."""
        for wire_id, code in self.rejected:
            if wire_id == task_id:
                return code
        return None

    @property
    def answered(self) -> set:
        """Every id that left the Worker, however it left."""
        return {i for i, _ in self.completed} | {i for i, _ in self.rejected}

    # -- what the Worker does to it -----------------------------------------

    def start(self) -> "FakeClient":
        self.started += 1
        return self

    def stop(self, timeout: float = 40.0) -> None:
        self.stopped += 1

    def next_task(self, timeout=None):
        try:
            return self.tasks.get(timeout=timeout)
        except queue.Empty:
            return None

    def complete(self, broker_id: str, body: str) -> None:
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"refusing an empty result for {broker_id}")
        self.completed.append((broker_id, body))

    def reject(self, broker_id: str, reason: str = "INVALID_TASK") -> None:
        self.rejected.append((broker_id, reason))

    def on_status(self, hook) -> None:
        self.hook = hook

    def snapshot(self) -> dict:
        return {"state": "connected", "connected": True, "gateway": "",
                "last_ok_ts": 0.0, "backoff_s": 0.0, "error": None,
                "inflight": 0, "pending_results": 0, "updated_ts": 0.0}
