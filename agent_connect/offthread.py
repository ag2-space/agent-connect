"""Blocking work, awaited without giving the shutdown something to join.

One function, in a module of its own, because two parts of this Worker that must
not import each other both need it: the seam to the Relay Client
(`agent_connect.relay`, `agent_connect.roomops`, `agent_connect.worker`), where
a sync library is called from asyncio, and the Adapter shim
(`agent_connect.adapters.shim`), where a synchronous Adapter's blocking `run()`
is. An Adapter has no business importing the relay wiring to borrow a thread,
and the wiring has no business being importable from an Adapter — so the thing
they share lives here, under neither of them.

It exists at all because `asyncio.to_thread` is the obvious way and the wrong
one, for a reason nobody would re-derive at a call site. See below.
"""
from __future__ import annotations

import asyncio
import threading


async def in_daemon_thread(call, *args):
    """Await a blocking call on a thread that cannot outlive this process.

    Every crossing of the library seam uses it — the queue read, `complete`,
    `reject`, and every Room Op the Ladder asks for — and so does the shim that
    runs a synchronous Adapter, because the library is sync and threaded, the
    Adapters are blocking, and this side is asyncio: a blocking call on the
    event loop is a Worker that has stopped doing everything else.

    `asyncio.to_thread` would be the obvious way, and it is the wrong one here.
    It runs on the loop's default executor, and `asyncio.run` shuts that
    executor down on the way out by **joining every thread in it**: measured, a
    SIGTERM during an eight-second call held the interpreter for 8.01 s after
    the loop had finished. That is the shutdown hang the queue reader is a
    daemon thread to avoid, and a `complete` can be inside it for the better
    part of a minute (the library's drain-lock wait plus its result budget, with
    a twenty-second POST able to start at the end of it — and an upload before
    that). An Adapter's `run()` is worse: it is a `codex` or `ollama` process
    the Worker is waiting on, bounded only by that Adapter's own timeout, up to
    600 s — against a `STOP_BUDGET_S` of 12 s and a launchd that sends SIGKILL
    twenty seconds after its SIGTERM.

    Nothing is lost by not waiting. Everything this is used for is either
    durable before its network call — the library journals a result and then
    POSTs it, and re-POSTs what is owed on the next run — or already abandoned
    by the cancel that is stopping it, which is the case for a Turn: an accepted
    Task nobody answered is re-served by the broker and run again, which is what
    unanswered work is supposed to do. An abandoned thread costs a round trip,
    not an answer. A Room Op abandoned this way costs a decoration, which is
    what I1 already says it is worth.

    What it does **not** do is stop the work: the thread runs on, and a Local
    Agent's subprocess with it, until the interpreter exits. This buys the
    *stop* its seconds back — the `stopped` record and the singleton guard's
    release — not a graceful end to what was running.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def hand_back(setter, value) -> None:
        # The awaiting Turn may have been cancelled, and the loop may be closed
        # — both mean nobody is waiting for this any more, and neither is worth
        # a traceback on the way out.
        if not done.cancelled():
            setter(value)

    def run() -> None:
        try:
            result = call(*args)
        except BaseException as exc:  # noqa: BLE001 — carried, not swallowed
            handed = (done.set_exception, exc)
        else:
            handed = (done.set_result, result)
        try:
            loop.call_soon_threadsafe(hand_back, *handed)
        except RuntimeError:
            pass                            # the loop closed; nobody is waiting

    threading.Thread(target=run, name="agent-connect-answer", daemon=True).start()
    return await done
