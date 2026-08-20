"""The Relay Client, built from this Worker's settings.

The Relay Client is no longer a foreign process writing files into a directory —
it is a library this repository owns, running inside the Worker
(`docs/adr/0001` in the workspace: *agent-connect owns its Relay Client*). This
module is the whole of the wiring between the two: it turns the settings an
operator already writes down into an `ag2_relay_client.RelayClient`, and it
knows nothing else about either side.

**Everything the wire knows lives below this line.** Polling, leases, acks,
results, the journal, heartbeats, backoff, credential rotation, media — none of
it is here, and none of it is in `worker.py`. What is here is the four
construction facts the library asks for and cannot guess:

* **the credential**, which carries its own gateway. The onboarding token is the
  combined `https://gateway|secret` form, and the split belongs to the library
  (`ag2_relay_client.credentials`). `roomops.py` used to carry a naive
  literal-`|` copy of that parse *and* a different precedence for the gateway,
  which is how one process ended up speaking to two of them; it now asks the
  library, and transport-seam ticket 09 is where the module itself dissolves.
* **the state directory**, `<workspace>/relay/`. It hangs off the workspace
  because everything else the Worker owns on disk already does, and because a
  Worker's durable state should not need a setting of its own to be found. The
  journal under it is what makes a restart re-complete a Task rather than
  re-execute it — the guarantee the archived task file only pretended to give.
* **the instance name**, which namespaces that directory. One Worker, one
  bearer, one state dir: two Workers sharing one would share a journal, and a
  shared journal is two Workers each believing the other's Tasks are answered.
* **the egress allowlist roots** — the directories a file the Local Agent
  produced may be sent to a room *from*, fixed when the client is built and
  never afterwards. There used to be a staging airlock here instead: the Worker
  copied a file into a directory a *separate process* trusted, and that process
  decided. One process means the check is in-process now, and `outgoing.py`
  chooses the roots (the working directory this Worker's Turns run in, plus
  anything the operator added). A Worker that passes none sends no files, which
  is the fail-closed reading and the only safe one.

This module is also where **sync meets asyncio**. The library is sync and
threaded on purpose — sutando's shim will call it directly — and this side is
asyncio, so every call across the seam goes through `in_daemon_thread` below.
That is one function rather than a habit, because the reason it is not
`asyncio.to_thread` is a shutdown hang nobody would re-derive at the call site.

**No credential is a refusal, not a degradation.** A Worker without one has no
inbound seam at all: it would start, report `serving`, and never receive a
thing. `worker.main()` turns the `None` this module returns into the sentence an
operator can act on, at the documented status path, the same way a missing
Adapter is handled.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Mapping, Optional

from ag2_relay_client import RelayClient, TokenSource
from ag2_relay_client.state import valid_instance_name

from .outgoing import egress_roots
from .status import DEFAULT_INSTANCE as status_default_instance
from .status import INSTANCE_ENV, instance_name

#: The token an operator gets from the Agent Portal. `REMOTE_TASK_TOKEN` is the
#: name the old two-process launcher exported for the relay client's own
#: process, and it is still accepted: one credential, under either name it has
#: ever had, so an existing install keeps working across the swap.
#:
#: The documented name wins where both are set. It used to be the other way
#: round, which meant a `REMOTE_TASK_TOKEN` left in an old launchd plist
#: silently outranked a freshly rotated `AGENT_CONNECT_TOKEN` — the setting
#: README calls *the* setting losing to the one it calls "the old name".
TOKEN_ENV = "AGENT_CONNECT_TOKEN"
LEGACY_TOKEN_ENV = "REMOTE_TASK_TOKEN"

#: Only for a bare secret that carries no gateway of its own. A combined token
#: names its own gateway and that one wins — see `TokenSource`, which says so
#: out loud when the two disagree.
URL_ENV = "REMOTE_TASK_URL"

#: The library's state, under the workspace the Worker already owns.
STATE_NAME = "relay"

#: The instance name used when the operator named none. Defined beside the
#: setting that carries it (`status.DEFAULT_INSTANCE`) so the status file and
#: the state directory cannot disagree about what an unnamed Worker is called;
#: it is a real name rather than an empty string because it becomes a directory
#: component, and it is the library's own default too.
DEFAULT_INSTANCE = status_default_instance


def token(env: Optional[Mapping[str, str]] = None) -> str:
    """The onboarding token as written, under either of its names.

    The **one** reader of the credential in this package: `roomops.py` asks here
    too, so the Ladder and the wire cannot end up holding two different bearers.
    """
    env = os.environ if env is None else env
    return (env.get(TOKEN_ENV) or env.get(LEGACY_TOKEN_ENV) or "").strip()


def state_dir(workspace: Path) -> Path:
    """Where the library keeps its journal, status and lock."""
    return Path(workspace) / STATE_NAME


def instance(env: Optional[Mapping[str, str]] = None) -> str:
    """This Worker's name, as a directory component may spell it.

    `AGENT_CONNECT_INSTANCE` was a label nobody validated — it went into the
    status document and no further. It now also namespaces the state directory,
    so it has a grammar, and a name outside it is refused rather than mangled:
    two instances quietly sharing one sanitised name would share one journal,
    which is the failure this namespacing exists to prevent.

    The grammar lives here because this is where the library that enforces it is
    imported. `worker.main` asks before it asks for anything else, so a name it
    would refuse is refused while the operator is still reading the first error
    rather than after they have fixed a second one.
    """
    name = instance_name(env)
    if not name:
        return DEFAULT_INSTANCE
    if not valid_instance_name(name):
        raise ValueError(
            f"{INSTANCE_ENV}={name!r} cannot name this Worker's relay state "
            f"directory: use letters, digits, '_' or '-', at most 32 of them"
        )
    return name


def from_env(
    workspace: Path,
    env: Optional[Mapping[str, str]] = None,
    repo: Optional[Path] = None,
    **options,
) -> Optional[RelayClient]:
    """The Relay Client this Worker speaks through, or `None` with no token.

    Constructed, not started: a bad credential is a startup refusal and should
    be found before the Adapter's preflight spends a minute proving the Local
    Agent is fine. `worker.main()` calls `start()` once everything that can
    refuse has declined to.

    `repo` is the directory the Local Agent works in, and it is passed here
    rather than read here because `worker.main` is what resolves it — it is the
    same directory a Turn runs in, and a file that leaves this machine leaves
    from there. Without it the client is built with no roots and sends nothing:
    a Worker that cannot say where its agent works cannot vouch for a path.
    """
    env = os.environ if env is None else env
    raw = token(env)
    if not raw:
        return None
    credentials = TokenSource(token=raw, base_url=(env.get(URL_ENV) or "").strip())
    options.setdefault("egress_roots", egress_roots(repo, env))
    return RelayClient(
        credentials,
        state_dir=state_dir(workspace),
        instance=instance(env),
        **options,
    )


async def in_daemon_thread(call, *args):
    """Await a blocking call on a thread that cannot outlive this process.

    Every crossing of this seam uses it — the queue read, `complete`, `reject`,
    and every Room Op the Ladder asks for — because the library is sync and
    threaded and this side is asyncio, and a blocking call on the event loop is
    a Worker that has stopped doing everything else.

    `asyncio.to_thread` would be the obvious way, and it is the wrong one here.
    It runs on the loop's default executor, and `asyncio.run` shuts that
    executor down on the way out by **joining every thread in it**: measured, a
    SIGTERM during an eight-second call held the interpreter for 8.01 s after
    the loop had finished. That is the shutdown hang the queue reader is a
    daemon thread to avoid, and a `complete` can be inside it for the better
    part of a minute (the library's drain-lock wait plus its result budget, with
    a twenty-second POST able to start at the end of it — and now an upload
    before that).

    Nothing is lost by not waiting. Everything this is used for is durable
    before its network call — the library journals a result and then POSTs it,
    and re-POSTs what is owed on the next run — so an abandoned thread costs a
    round trip, not an answer. A Room Op abandoned this way costs a decoration,
    which is what I1 already says it is worth.
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
