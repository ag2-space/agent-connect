"""Seam fixtures: a Task as the Relay Client delivers it, a client to answer it
to, and the Room Ops a Ladder climbs.

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
    from _taskqueue import FakeClient, task

    client = FakeClient()
    body = asyncio.run(handle_one(task("t1", "do it"), adapter, "/repo",
                                  client=client))
    assert client.answer("t1") == body
"""
from __future__ import annotations

import os
import queue
from typing import Optional

import _bootstrap
from ag2_relay_client import Task
from ag2_relay_client.client import STOP_JOIN_S, _error_code
from ag2_relay_client.envelope import parse_task
from ag2_relay_client.state import valid_wire_id

def room_ops_at(url: str, roots=()) -> object:
    """The Ladder's Room Ops, pointed at a relay listening on `url`.

    Built the way `worker.main` builds them — around a real
    `ag2_relay_client.roomops.RoomOps` — because there is no other way to build
    one any more. `agent_connect.roomops` used to take a URL and a bearer and do
    its own HTTP; it now takes the object the client already made, so a test
    that wants a relay of its own makes one the same way the Worker does.

    The credential is the combined `<url>|<secret>` form, which is the only form
    the library takes: there is no compiled-in gateway anywhere below this line.
    `roots` is the egress allowlist — empty by default, because a Ladder test is
    not an egress test and a client built with no roots sends no files.
    """
    from ag2_relay_client.credentials import TokenSource
    from ag2_relay_client.egress import EgressAllowlist
    from ag2_relay_client.roomops import RoomOps as WireRoomOps
    from ag2_relay_client.transport import RelayHTTP

    from agent_connect.roomops import RoomOps

    http = RelayHTTP(TokenSource(token=f"{url}|test-secret"))
    return RoomOps(WireRoomOps(http, EgressAllowlist(roots)))


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


def task(task_id: str, body: str = "do it", room: str = "", tier: str = "owner",
         attachments=(), **fields) -> Task:
    """One Task, as the library would have delivered it.

    Built through the library's own `parse_task` wherever the field is a wire
    field, so a fixture cannot quietly describe an envelope the broker could not
    send — a Task whose `access_tier` is `team` is spelled here the way it
    arrives, and reaches `attested_tier` the way it arrives.

    `attachments` is set on the parsed Task the way media ingress sets it: the
    marker stage resolves the files after `parse_task` and before delivery, so
    a fixture that assigned it any other way would be describing a Task the
    library does not build. (This used to be a `QueuedTask` subclass, back when
    ticket 03 had not landed and `Task.__slots__` had no `attachments`.)
    """
    raw = {"id": task_id, "task": body, "access_tier": tier}
    if room:
        raw["channel_id"] = room
    raw.update(fields)
    parsed = parse_task(raw)
    if parsed is None:
        raise ValueError(f"not a Task the library would deliver: {raw!r}")
    parsed.attachments = tuple(attachments)
    return parsed


class FakeClient:
    """The Relay Client's consumer surface, in memory.

    Records what it was told rather than posting it, and **refuses what the real
    one refuses**: an empty result body is "not ready", not an answer (H5), and
    an id that is not a broker task id is refused at egress as well as at intake
    (F8). A fixture that accepts what the real client raises on is a test that
    proves nothing, so the checks are not reimplemented here — the library's own
    `valid_wire_id` and `_error_code` are imported, and the join default is read
    off `STOP_JOIN_S` rather than written down a second time. Each of those was
    drift: this file said `40.0` for a default the library had moved to `5.0`,
    took ids the real client rejects, and recorded a reject reason raw where the
    real one coerces it to an error code.
    """

    def __init__(self):
        self.tasks: "queue.Queue" = queue.Queue()
        #: `(id, body)` in the order the Worker completed them.
        self.completed: list = []
        #: `(id, error_code)` in the order the Worker rejected them.
        self.rejected: list = []
        #: `(id, base_dir)` — what a relative attachment path is read against.
        self.base_dirs: list = []
        #: Whatever `on_status` was given, so a test can drive the hook itself.
        self.hook = None
        self.started = 0
        self.stopped = 0
        #: The `timeout=` of every `stop` this client was asked for.
        self.stop_timeouts: list = []

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

    def stop(self, timeout: float = STOP_JOIN_S) -> None:
        #: What the Worker asked for, so a test can assert it waited long enough
        #: to be able to release the singleton guard.
        self.stop_timeouts.append(timeout)
        self.stopped += 1

    def next_task(self, timeout=None):
        try:
            return self.tasks.get(timeout=timeout)
        except queue.Empty:
            return None

    def complete(self, broker_id: str, body: str, base_dir=None) -> None:
        """The real one's signature, including the third argument.

        `base_dir` is what a relative path in a `[file:]` marker is read
        against, and it is a *parameter* rather than something the library
        reads: only the consumer knows where its turn ran. Recorded here so a
        test can assert the Worker passes it — a Worker that forgot to would
        turn every relatively-named attachment into a refusal, quietly.
        """
        wire_id = self._wire_id(broker_id)
        if not isinstance(body, str) or not body.strip():
            raise ValueError(f"refusing an empty result for {wire_id}")
        self.base_dirs.append((wire_id, base_dir))
        self.completed.append((wire_id, body))

    def reject(self, broker_id: str, reason: str = "INVALID_TASK") -> None:
        # Coerced, as the real one coerces it: free text degrades to the schema
        # code rather than travelling to the broker as one.
        self.rejected.append((self._wire_id(broker_id), _error_code(reason)))

    def on_status(self, hook) -> None:
        self.hook = hook

    def snapshot(self) -> dict:
        return {"state": "connected", "connected": True, "gateway": "",
                "last_ok_ts": 0.0, "backoff_s": 0.0, "recheck_s": 0.0,
                "acks_paused_s": 0.0, "singleton": "held", "error": None,
                "inflight": 0, "pending_results": 0, "updated_ts": 0.0}

    @staticmethod
    def _wire_id(broker_id) -> str:
        """The real client's egress check, borrowed rather than imitated."""
        if not isinstance(broker_id, str) or not valid_wire_id(broker_id):
            raise ValueError(f"not a broker task id: {broker_id!r:.80}")
        return broker_id
