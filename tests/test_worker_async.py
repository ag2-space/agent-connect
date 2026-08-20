"""Tests for the asynchronous, event-shaped Adapter contract and the shim.

Everything here is asserted at the Worker's handle-one-Task seam or at the
Adapter boundary: what the Adapter was handed, what events crossed, and what the
Relay Client was told the answer was. Nothing asserts on which internal object
called which.

The fixtures are queue fixtures — a `Task` put on a `FakeClient`, and the
`complete` / `reject` it recorded. They used to be files in `tasks/` and files
in `results/`, which is the seam that ticket removed.

Run: python3 tests/test_worker_async.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import ast
import asyncio
import threading
import time
from pathlib import Path

from _queue import FakeClient, task
from ag2_relay_client import media
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
client = FakeClient()
tf = task("c2", "do it", room="!r2:ag2.space", sender_name="Nikita",
          user_id="@n:ag2.space", source_message_id="$m2")
asyncio.run(handle_one(tf, native, "/repo", client=client))
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
out = asyncio.run(process_one(task("x1", "do it", room="!e:ag2.space"),
                              errs, "/repo"))
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
client = FakeClient()
a = task("r1", "slow one", room="!a:ag2.space")
b = task("r2", "slow two", room="!b:ag2.space")


async def _two_rooms():
    sessions = {}
    started = time.monotonic()
    await asyncio.gather(
        handle_one(a, slow, "/repo", sessions, client=client),
        handle_one(b, slow, "/repo", sessions, client=client),
    )
    return time.monotonic() - started


elapsed = asyncio.run(_two_rooms())
check(elapsed < 0.75, f"two rooms are served at once (took {elapsed:.2f}s, not ~0.8s)")
check(slow.impl.peak == 2, "both Tasks were genuinely in flight together")
check(client.answered == {"r1", "r2"}, "both rooms got an answer")

same = ShimAdapter("same", SyncStub(delay=0.2))
c = task("s1", "first", room="!same:ag2.space")
d = task("s2", "second", room="!same:ag2.space")


async def _one_session():
    sessions = {}
    await asyncio.gather(
        process_one(c, same, "/repo", sessions),
        process_one(d, same, "/repo", sessions),
    )


asyncio.run(_one_session())
check(same.impl.peak == 1, "one Session runs one Turn at a time")


# -- a failing Task is one Task's problem ------------------------------------

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
        handle_one(boom, Selective(), "/repo", sessions, client=client),
        handle_one(fine, Selective(), "/repo", sessions, client=client),
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

client = FakeClient()
counted = NativeStub()
asyncio.run(handle_one(task("e1", ""), counted, "/repo", client=client))
check(client.refusal("e1") == EMPTY_TASK,
      "a Task with no prompt in it is dead-lettered rather than dropped: "
      "re-serving it produces the same nothing five times over")
check(not client.completed, "and nothing is completed for it")
check(counted.seen == [], "an empty Task never reaches the Adapter")

# A body that was only an unsigned metadata block is empty by the time it gets
# here — the library quarantined it (G2) and deliberately does not fall back to
# the unstripped text. That is a Task nothing could ever answer, too.
asyncio.run(handle_one(task("e2", "[room-ops metadata: reply_to=$x]"), counted,
                       "/repo", client=client))
check(client.refusal("e2") == EMPTY_TASK,
      "a body that was nothing but a quarantined metadata block is the same "
      "refusal, and still never reaches the Adapter")
check(counted.seen == [], "— still nothing handed to the Local Agent")

answered = asyncio.run(handle_one(task("e3", "hello", room="!z:ag2.space"),
                                  Selective(), "/repo", client=client))
check(answered == "ok" and client.answer("e3") == "ok",
      "and an ordinary Task is completed with what the Turn returned")
check(len(client.completed) + len(client.rejected) == 3,
      "three Tasks off the queue, three answers to the broker — no silent drops")

# An upload with no caption is not an empty Task. Element sends `caption ==
# filename` for one, so the body the broker forwards is only a media marker,
# and the library empties such a body by design — "the attachment tuple is
# where that task's content is". Judged on the body alone it reads as empty,
# and empty is *terminal*: the broker parks it, posts a failure notice, and no
# retry can recover it, for a screenshot somebody dropped into a room.
client = FakeClient()
shot = task("e4", "", attachments=(media.Attachment(
    path="/tmp/nothing/Screenshot.png", name="Screenshot.png",
    mime="image/png", ok=True),))
looked = NativeStub()
body = asyncio.run(handle_one(shot, looked, "/repo", client=client))
check(client.refusal("e4") is None,
      "a Task carrying a file is never dead-lettered for having no words in it")
check(client.answer("e4") == body and body,
      "it is completed, like any other Task")
check(len(looked.seen) == 1 and "Screenshot.png" in looked.seen[0].prompt,
      "the Adapter is asked about the file by name — an upload with no caption "
      "is still a question, and this is the only place that knows what it is")
check(looked.seen and [a.path for a in looked.seen[0].attachments]
      == ["/tmp/nothing/Screenshot.png"],
      "and the file travels beside the prompt, never folded into it")

still_empty = task("e5", "")
asyncio.run(handle_one(still_empty, looked, "/repo", client=client))
check(client.refusal("e5") == EMPTY_TASK,
      "a Task with neither text nor files is still the reject — emptiness is "
      "judged on both, and `reject` stays reserved for genuinely nothing")


# -- a cancelled Turn answers neither, on purpose --------------------------------

# `CancelledError` is a `BaseException`, so it goes straight through the guard
# that turns a failure into a body — and it must. On a stop, `asyncio.run`
# cancels every Turn in flight; an id left accepted-and-unanswered is re-served
# by the broker and re-executed, which is right for work that never finished,
# while a `reject` would be terminal for a Task whose only problem was that the
# Worker was asked to stop.
client = FakeClient()


class Wedged:
    """An Adapter that never finishes, so its Turn can be cancelled mid-flight."""

    def __init__(self):
        self.entered = threading.Event()

    async def turn(self, ctx):
        self.entered.set()
        await asyncio.sleep(30)
        yield Done(text="never said")


async def _cancelled():
    adapter = Wedged()
    fut = asyncio.ensure_future(
        handle_one(task("c9", "take your time", room="!c:ag2.space"), adapter,
                   "/repo", client=client))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if adapter.entered.is_set():
            break
    fut.cancel()
    try:
        await fut
    except asyncio.CancelledError:
        return "cancelled"
    return "finished"


check(asyncio.run(_cancelled()) == "cancelled",
      "a cancelled Turn leaves through the cancellation, not through an answer")
check(not client.completed and not client.rejected,
      "nothing is said to the broker for it: the id stays accepted-unanswered, "
      "the broker re-serves it, and it runs again — which is what unanswered "
      "work is supposed to do")

# And the answer that was in flight when the stop came does not hold the process
# open. `asyncio.run` shuts the default executor down by joining every thread in
# it, so an answer sent with `asyncio.to_thread` kept the interpreter alive for
# the whole of a `complete` — which the library bounds at its drain-lock wait
# plus its result budget, near a minute, against a launchd that kills at twenty
# seconds. Nothing is lost by not waiting: the library journals a result before
# it POSTs it, and re-POSTs what is owed on the next run.
class Sluggish(FakeClient):
    def complete(self, broker_id, body):
        time.sleep(1.0)
        super().complete(broker_id, body)


async def _answering_at_the_stop():
    fut = asyncio.ensure_future(
        handle_one(task("s1", "quick", room="!s:ag2.space"), NativeStub(),
                   "/repo", client=Sluggish()))
    await asyncio.sleep(0.15)                # the answer is on its thread now
    fut.cancel()                             # what the teardown does on SIGTERM
    try:
        await fut
    except asyncio.CancelledError:
        pass


began = time.monotonic()
asyncio.run(_answering_at_the_stop())
took = time.monotonic() - began
check(took < 0.6,
      f"a stop in the middle of answering returns at once ({took:.2f}s), rather "
      f"than holding the process for the length of the answer's POST")


# -- the drain loop keeps going -----------------------------------------------

client = FakeClient().offer(task("l1", "explode", room="!l1:ag2.space"),
                            task("l2", "fine", room="!l2:ag2.space"))


async def _drain():
    loop = asyncio.ensure_future(
        serve(Selective(), "/repo", client, 0.01))
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


# -- but a Worker that cannot be given work does not go on saying it is serving --

# The queue reader is the Worker's only inlet, and it used to catch exactly one
# exception — the `RuntimeError` its handover raises against a closed loop.
# Anything raised by the queue read itself ended the thread in silence, after which the
# Worker reported `serving`, beat for ever with `tasks_running: 0`, and received
# nothing: alive by every measure anybody has, and no longer an agent.
class Unreadable(FakeClient):
    def next_task(self, timeout=None):
        raise OSError("the queue went away")


async def _dead_reader():
    try:
        await asyncio.wait_for(
            serve(NativeStub(), "/repo", Unreadable(), 0.01), 5.0)
    except asyncio.TimeoutError:
        return "still serving, receiving nothing"
    except RuntimeError as exc:
        return str(exc)
    return "returned"


ended = asyncio.run(_dead_reader())
check("the queue reader stopped" in ended,
      "a queue read that raised takes the drain loop down with it, so the "
      "status file gets an `error` and a service manager restarts something "
      "that can actually be given work")
check("the queue went away" in ended,
      "carrying what actually happened, because nothing else saw it")

print("\n" + ("PASS — async adapter contract green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
