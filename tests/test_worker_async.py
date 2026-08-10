"""Tests for the asynchronous, event-shaped Adapter contract and the shim.

Everything here is asserted at the Worker's handle-one-Task seam or at the
Adapter boundary: what the Adapter was handed, what events crossed, and what
landed in results/. Nothing asserts on which internal object called which.

Run: python3 tests/test_worker_async.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import ast
import asyncio
import tempfile
import threading
import time
from pathlib import Path

from agent_connect import events as ev
from agent_connect.adapters import ADAPTERS, ShimAdapter, get as get_adapter
from agent_connect.events import Done, MessageChunk, TurnContext
from agent_connect.worker import handle_one, parse_task, process_one, serve, turn_context

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def workspace():
    tmp = Path(tempfile.mkdtemp())
    tasks, results = tmp / "tasks", tmp / "results"
    tasks.mkdir()
    results.mkdir()
    return tasks, results


def write_task(tasks: Path, task_id: str, body: str, **headers) -> Path:
    lines = [f"id: {task_id}", f"task: {body}"]
    lines += [f"{k}: {v}" for k, v in headers.items() if k != "access_tier"]
    lines.append(f"access_tier: {headers.get('access_tier', 'owner')}")
    path = tasks / f"task-{task_id}.txt"
    path.write_text("\n".join(lines) + "\n")
    return path


class SyncStub:
    """A synchronous Adapter of the shape the five existing ones have."""

    def __init__(self, output="stub-output", delay=0.0, boom=None):
        self.output, self.delay, self.boom = output, delay, boom
        self.calls = []
        self.concurrent = 0
        self.peak = 0
        self._lock = threading.Lock()

    def run(self, task, sandbox, cwd):
        with self._lock:
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
        try:
            self.calls.append((task, sandbox, cwd))
            if self.delay:
                time.sleep(self.delay)
            if self.boom:
                raise RuntimeError(self.boom)
            return self.output
        finally:
            with self._lock:
                self.concurrent -= 1


class NativeStub:
    """An Adapter written to the contract directly, as the ACP one will be."""

    def __init__(self, stream=None):
        self.seen = []
        self._stream = stream

    async def turn(self, ctx):
        self.seen.append(ctx)
        for event in self._stream or [MessageChunk(text="hi"), Done(text="hi")]:
            yield event


# -- the context reaches the Adapter, carrying every field -------------------

fields = parse_task(
    "id: task-c1\n"
    "channel_id: !room:ag2.space\n"
    "room_name: qingyun\n"
    "sender_name: Nikita\n"
    "user_id: @nikita:ag2.space\n"
    "source: ag2space\n"
    "source_message_id: $msg-42\n"
    "task: summarise worker.py\n"
    "access_tier: owner\n"
)
ctx = turn_context(fields, "task-c1", "/repo")
check(ctx.room == "!room:ag2.space", "context carries the room")
check(ctx.access_tier == "owner", "context carries the Access Tier")
check(ctx.sender_name == "Nikita", "context carries the sender name")
check(ctx.user_id == "@nikita:ag2.space", "context carries the user identifier")
check(ctx.source_message_id == "$msg-42", "context carries the source message identifier")
check(ctx.prompt == "summarise worker.py", "context carries the prompt, preamble-free")
check(ctx.sandbox == "workspace-write", "the Sandbox is derived from the Tier, here")
check(ctx.cwd == "/repo", "context carries the working directory")
check(ctx.session_key == ("!room:ag2.space", "owner"), "Session key is room + Tier, never room alone")
check(
    turn_context({"access_tier": "owner", "task": "x"}, "task-r0", "/repo").session_key
    != turn_context({"access_tier": "owner", "task": "x"}, "task-r1", "/repo").session_key,
    "two roomless Tasks do not share a Session key",
)

native = NativeStub()
tasks, results = workspace()
tf = write_task(
    tasks, "c2", "do it",
    channel_id="!r2:ag2.space", sender_name="Nikita", user_id="@n:ag2.space",
    source_message_id="$m2", access_tier="owner",
)
asyncio.run(process_one(tf, native, "/repo", results))
seen = native.seen[0]
check(isinstance(seen, TurnContext), "the Adapter is handed a TurnContext")
check(
    (seen.room, seen.access_tier, seen.sender_name, seen.user_id, seen.source_message_id)
    == ("!r2:ag2.space", "owner", "Nikita", "@n:ag2.space", "$m2"),
    "every carried field reaches the Adapter",
)
check((results / "task-c2.txt").read_text() == "hi\n", "the answer is written from the event stream")


# -- the shim: two events, and codex unchanged -------------------------------

stub = SyncStub(output="stub-output")
shim = ShimAdapter("stub", stub)
emitted = []


async def _collect():
    async for event in shim.turn(
        TurnContext(prompt="do the thing", access_tier="owner", sandbox="workspace-write", cwd="/repo")
    ):
        emitted.append(event)


asyncio.run(_collect())
check([type(e) for e in emitted] == [MessageChunk, Done], "the shim emits exactly two events")
check(emitted[0].text == "stub-output", "the message chunk carries what the Adapter returned")
check(emitted[1].reason == ev.COMPLETED and emitted[1].ok, "the Turn is done-with-reason 'completed'")
check(emitted[1].text == "stub-output", "the terminal event repeats the whole answer")

sent_task, sent_sandbox, sent_cwd = stub.calls[0]
check(sent_sandbox == "workspace-write", "the shim passes the Sandbox through unchanged")
check(sent_cwd == "/repo", "the shim passes the working directory through unchanged")
check(
    sent_task.startswith("[agent-connect: this run's sandbox is 'workspace-write'")
    and sent_task.endswith("do the thing"),
    "the shim prepends the same sandbox preamble as before",
)

empty = ShimAdapter("empty", SyncStub(output=""))
blank = []


async def _collect_blank():
    async for event in empty.turn(TurnContext(prompt="x")):
        blank.append(event)


asyncio.run(_collect_blank())
check([type(e) for e in blank] == [Done], "an Adapter that said nothing emits no empty chunk")

check(
    sorted(ADAPTERS) == ["cline", "codex", "kilo", "ollama", "omnigent"],
    "all five original Adapters are still registered",
)
check(
    all(isinstance(get_adapter(n), ShimAdapter) for n in ADAPTERS),
    "all five are selectable and run through the shim",
)
check(
    all(hasattr(m, "run") and not hasattr(m, "turn") for m in ADAPTERS.values()),
    "none of the five was rewritten — each still has only its synchronous entry point",
)
check(get_adapter("codex").impl is ADAPTERS["codex"], "codex is driven by its own unchanged module")

# A synchronous Adapter reports its own failures in band. The shim must carry
# that prose through verbatim rather than classify it — the error text a person
# sees for a missing CLI or a timeout is unchanged.
errs = ShimAdapter("codex-ish", SyncStub(output="agent-connect: codex timed out after 600s."))
tasks, results = workspace()
et = write_task(tasks, "x1", "do it", channel_id="!e:ag2.space")
asyncio.run(process_one(et, errs, "/repo", results))
check(
    (results / "task-x1.txt").read_text() == "agent-connect: codex timed out after 600s.\n",
    "an Adapter's own error text reaches the result unchanged",
)


# -- the vocabulary is ours: no protocol type crosses the boundary -----------

def imported_modules(path: Path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0] or "." * node.level)
    return names


root = _bootstrap.ROOT / "agent_connect"
vocab_imports = imported_modules(root / "events.py")
check("acp" not in vocab_imports, "the vocabulary module does not import the protocol library")
check(
    vocab_imports <= {"__future__", "dataclasses", "typing"},
    "the vocabulary module depends on nothing but the standard library",
)
check("acp" not in imported_modules(root / "adapters" / "shim.py"), "the shim knows no protocol either")
# The list is closed on purpose: it grows by argument, not by accident. It grew
# once, for `notice` — Sessions have to tell a room that its context was reset,
# and that is neither the answer, nor progress towards it, nor a footnote on how
# it ended, so none of the other seven could carry it honestly.
check(
    {"message_chunk", "thinking", "tool_started", "tool_finished", "plan",
     "permission_asked", "notice", "done"}
    == {
        cls.kind
        for cls in vars(ev).values()
        if isinstance(cls, type) and issubclass(cls, ev.TurnEvent) and cls is not ev.TurnEvent
    },
    "the vocabulary is exactly the eight agreed events",
)


# -- rooms stop blocking each other ------------------------------------------

slow = ShimAdapter("slow", SyncStub(delay=0.4))
tasks, results = workspace()
a = write_task(tasks, "r1", "slow one", channel_id="!a:ag2.space")
b = write_task(tasks, "r2", "slow two", channel_id="!b:ag2.space")


async def _two_rooms():
    sessions = {}
    started = time.monotonic()
    await asyncio.gather(
        process_one(a, slow, "/repo", results, sessions),
        process_one(b, slow, "/repo", results, sessions),
    )
    return time.monotonic() - started


elapsed = asyncio.run(_two_rooms())
check(elapsed < 0.75, f"two rooms are served at once (took {elapsed:.2f}s, not ~0.8s)")
check(slow.impl.peak == 2, "both Tasks were genuinely in flight together")
check(
    (results / "task-r1.txt").exists() and (results / "task-r2.txt").exists(),
    "both rooms got a result",
)

same = ShimAdapter("same", SyncStub(delay=0.2))
tasks, results = workspace()
c = write_task(tasks, "s1", "first", channel_id="!same:ag2.space")
d = write_task(tasks, "s2", "second", channel_id="!same:ag2.space")


async def _one_session():
    sessions = {}
    await asyncio.gather(
        process_one(c, same, "/repo", results, sessions),
        process_one(d, same, "/repo", results, sessions),
    )


asyncio.run(_one_session())
check(same.impl.peak == 1, "one Session runs one Turn at a time")


# -- a failing Task is one Task's problem ------------------------------------

tasks, results = workspace()
boom = write_task(tasks, "b1", "explode", channel_id="!x:ag2.space")
fine = write_task(tasks, "b2", "fine", channel_id="!y:ag2.space")


class Selective:
    async def turn(self, ctx):
        if "explode" in ctx.prompt:
            raise RuntimeError("adapter blew up")
        yield Done(text="ok")


async def _one_bad():
    sessions = {}
    await asyncio.gather(
        handle_one(boom, Selective(), "/repo", results, sessions),
        handle_one(fine, Selective(), "/repo", results, sessions),
    )


asyncio.run(_one_bad())
check(
    (results / "task-b1.txt").read_text() == "agent-connect: worker error: adapter blew up\n",
    "a failing Task writes the same error result as before",
)
check((results / "task-b2.txt").read_text() == "ok\n", "the healthy Task in another room is unaffected")


# -- a result is written exactly once ----------------------------------------

tasks, results = workspace()
empty_task = tasks / "task-e1.txt"
empty_task.write_text("id: task-e1\ntask:\naccess_tier: owner\n")
counted = NativeStub()
asyncio.run(process_one(empty_task, counted, "/repo", results))
check((results / "task-e1.txt").read_text() == "[no-send] empty task\n", "an empty Task is marked no-send")
check(counted.seen == [], "an empty Task never reaches the Adapter")

(results / "task-e1.txt").write_text("already answered\n")
asyncio.run(process_one(empty_task, counted, "/repo", results))
check((results / "task-e1.txt").read_text() == "already answered\n", "an answered Task is not answered twice")

pre = write_task(tasks, "e2", "hello", channel_id="!z:ag2.space")
(results / "task-e2.txt").write_text("first answer\n")
asyncio.run(handle_one(pre, Selective(), "/repo", results))
check(
    (results / "task-e2.txt").read_text() == "first answer\n",
    "an existing result survives a second pass, failures included",
)


# -- the scan loop keeps going ------------------------------------------------

tasks, results = workspace()
write_task(tasks, "l1", "explode", channel_id="!l1:ag2.space")
write_task(tasks, "l2", "fine", channel_id="!l2:ag2.space")


async def _scan():
    loop = asyncio.ensure_future(serve(Selective(), "/repo", results, tasks, 0.01))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if (results / "task-l1.txt").exists() and (results / "task-l2.txt").exists():
            break
    still_running = not loop.done()
    loop.cancel()
    return still_running


check(asyncio.run(_scan()), "the scan loop survives a Task that raised")
check((results / "task-l2.txt").read_text() == "ok\n", "the scan loop answered the healthy Task")

print("\n" + ("PASS — async adapter contract green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
