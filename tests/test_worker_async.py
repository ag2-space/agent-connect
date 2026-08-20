"""Tests for the asynchronous, event-shaped Adapter contract and the shim.

Everything here is asserted at the Worker's handle-one-Task seam or at the
Adapter boundary: what the Adapter was handed, what events crossed, and what the
Relay Client was told the answer was. Nothing asserts on which internal object
called which.

The fixtures are queue fixtures — a `Task` put on a `FakeClient`, and the
`complete` / `reject` it recorded. They used to be files in `tasks/` and files
in `results/`, which is the seam this ticket removed.

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

from _queue import FakeClient, task
from agent_connect import events as ev
from agent_connect.adapters import ADAPTERS, ShimAdapter, get as get_adapter
from agent_connect.events import Done, MessageChunk, TurnContext
from agent_connect.worker import EMPTY_TASK, handle_one, process_one, serve, turn_context

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def workspace():
    """The outgoing directory, which is all a workspace is to a Turn now."""
    tmp = Path(tempfile.mkdtemp())
    results = tmp / "results"
    results.mkdir()
    return results


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

ctx = turn_context(
    task("task-c1", "summarise worker.py", room="!room:ag2.space",
         room_name="qingyun", sender_name="Nikita", user_id="@nikita:ag2.space",
         source_message_id="$msg-42"),
    "/repo")
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
    turn_context(task("task-r0", "x"), "/repo").session_key
    != turn_context(task("task-r1", "x"), "/repo").session_key,
    "two roomless Tasks do not share a Session key",
)

native = NativeStub()
results = workspace()
client = FakeClient()
tf = task("c2", "do it", room="!r2:ag2.space", sender_name="Nikita",
          user_id="@n:ag2.space", source_message_id="$m2")
asyncio.run(handle_one(tf, native, "/repo", results, client=client))
seen = native.seen[0]
check(isinstance(seen, TurnContext), "the Adapter is handed a TurnContext")
check(
    (seen.room, seen.access_tier, seen.sender_name, seen.user_id, seen.source_message_id)
    == ("!r2:ag2.space", "owner", "Nikita", "@n:ag2.space", "$m2"),
    "every carried field reaches the Adapter",
)
check(client.answer("c2") == "hi",
      "the answer the Relay Client is completed with comes from the event stream")
check(client.completed == [("c2", "hi")] and not client.rejected,
      "one Task off the queue, one `complete`, under the broker's own id")


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
results = workspace()
out = asyncio.run(process_one(task("x1", "do it", room="!e:ag2.space"),
                              errs, "/repo", results))
check(
    out == "agent-connect: codex timed out after 600s.",
    "an Adapter's own error text reaches the answer unchanged",
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
results = workspace()
client = FakeClient()
a = task("r1", "slow one", room="!a:ag2.space")
b = task("r2", "slow two", room="!b:ag2.space")


async def _two_rooms():
    sessions = {}
    started = time.monotonic()
    await asyncio.gather(
        handle_one(a, slow, "/repo", results, sessions, client=client),
        handle_one(b, slow, "/repo", results, sessions, client=client),
    )
    return time.monotonic() - started


elapsed = asyncio.run(_two_rooms())
check(elapsed < 0.75, f"two rooms are served at once (took {elapsed:.2f}s, not ~0.8s)")
check(slow.impl.peak == 2, "both Tasks were genuinely in flight together")
check(client.answered == {"r1", "r2"}, "both rooms got an answer")

same = ShimAdapter("same", SyncStub(delay=0.2))
results = workspace()
c = task("s1", "first", room="!same:ag2.space")
d = task("s2", "second", room="!same:ag2.space")


async def _one_session():
    sessions = {}
    await asyncio.gather(
        process_one(c, same, "/repo", results, sessions),
        process_one(d, same, "/repo", results, sessions),
    )


asyncio.run(_one_session())
check(same.impl.peak == 1, "one Session runs one Turn at a time")


# -- a failing Task is one Task's problem ------------------------------------

results = workspace()
client = FakeClient()
boom = task("b1", "explode", room="!x:ag2.space")
fine = task("b2", "fine", room="!y:ag2.space")


class Selective:
    async def turn(self, ctx):
        if "explode" in ctx.prompt:
            raise RuntimeError("adapter blew up")
        yield Done(text="ok")


async def _one_bad():
    sessions = {}
    await asyncio.gather(
        handle_one(boom, Selective(), "/repo", results, sessions, client=client),
        handle_one(fine, Selective(), "/repo", results, sessions, client=client),
    )


asyncio.run(_one_bad())
check(
    client.answer("b1") == "agent-connect: worker error: adapter blew up",
    "a failing Task completes with the same error text as before — the person "
    "who asked hears something back rather than nothing",
)
check(client.answer("b2") == "ok", "the healthy Task in another room is unaffected")
check(not client.rejected,
      "and neither is dead-lettered: a Worker bug is not a malformed Task, and "
      "rejecting one would tell the broker never to try this Task again")


# -- every Task leaves through complete or reject, once -----------------------

results = workspace()
client = FakeClient()
counted = NativeStub()
asyncio.run(handle_one(task("e1", ""), counted, "/repo", results, client=client))
check(client.refusal("e1") == EMPTY_TASK,
      "a Task with no prompt in it is dead-lettered rather than dropped: "
      "re-serving it produces the same nothing five times over")
check(not client.completed, "and nothing is completed for it")
check(counted.seen == [], "an empty Task never reaches the Adapter")

# A body that was only an unsigned metadata block is empty by the time it gets
# here — the library quarantined it (G2) and deliberately does not fall back to
# the unstripped text. That is a Task nothing could ever answer, too.
asyncio.run(handle_one(task("e2", "[room-ops metadata: reply_to=$x]"), counted,
                       "/repo", results, client=client))
check(client.refusal("e2") == EMPTY_TASK,
      "a body that was nothing but a quarantined metadata block is the same "
      "refusal, and still never reaches the Adapter")
check(counted.seen == [], "— still nothing handed to the Local Agent")

answered = asyncio.run(handle_one(task("e3", "hello", room="!z:ag2.space"),
                                  Selective(), "/repo", results, client=client))
check(answered == "ok" and client.answer("e3") == "ok",
      "and an ordinary Task is completed with what the Turn returned")
check(len(client.completed) + len(client.rejected) == 3,
      "three Tasks off the queue, three answers to the broker — no silent drops")


# -- the drain loop keeps going -----------------------------------------------

results = workspace()
client = FakeClient().offer(task("l1", "explode", room="!l1:ag2.space"),
                            task("l2", "fine", room="!l2:ag2.space"))


async def _drain():
    loop = asyncio.ensure_future(
        serve(Selective(), "/repo", results, client, 0.01))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if client.answered == {"l1", "l2"}:
            break
    still_running = not loop.done()
    loop.cancel()
    return still_running


check(asyncio.run(_drain()), "the drain loop survives a Task that raised")
check(client.answer("l2") == "ok", "the drain loop answered the healthy Task")
check(client.answer("l1") == "agent-connect: worker error: adapter blew up",
      "and answered the one that raised, instead of leaving its lease to expire")

print("\n" + ("PASS — async adapter contract green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
