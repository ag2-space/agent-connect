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
  anything the operator added), and `from_env` checks them here rather than
  letting the allowlist drop one quietly. A Worker that passes none sends no
  files, which is the fail-closed reading and the only safe one.

The library is sync and threaded on purpose — sutando's shim will call it
directly — and this side is asyncio, so every call across the seam goes through
`agent_connect.offthread.in_daemon_thread`. That is one function rather than a
habit, because the reason it is not `asyncio.to_thread` is a shutdown hang
nobody would re-derive at the call site; it lives in a module of its own because
the Adapter shim needs it too and must not import this one to get it.

**No credential is a refusal, not a degradation.** A Worker without one has no
inbound seam at all: it would start, report `serving`, and never receive a
thing. `worker.main()` turns the `None` this module returns into the sentence an
operator can act on, at the documented status path, the same way a missing
Adapter is handled.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from ag2_relay_client import RelayClient, TokenSource
from ag2_relay_client.state import valid_instance_name

from .outgoing import UNSENDABLE, egress_roots, unsendable
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

    The **one** reader of the credential in this package, and `from_env` below
    is its only caller — so there is one bearer in this process and one place
    the environment is read for it. `roomops.py` used to ask here as well, back
    when the Ladder built its own speaker; it now takes the Room Ops off the
    client this function's caller already built, which is the stronger version
    of the same property: not two readers that agree, one object.
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

    **A root that is not there is said here**, before the client is built. The
    allowlist would otherwise drop it, log one line at the first upload, and
    refuse every file named under it for the rest of the run — a Worker that has
    stopped attaching things, found out by the person who asked for one. Said,
    and not refused: a Worker that cannot attach a file can still answer the
    question, and stopping it from answering at all would be a worse outage than
    the one being reported. `outgoing` owns both the check and the sentence,
    because it owns the roots.
    """
    env = os.environ if env is None else env
    raw = token(env)
    if not raw:
        return None
    credentials = TokenSource(token=raw, base_url=(env.get(URL_ENV) or "").strip())
    roots = options.setdefault("egress_roots", egress_roots(repo, env))
    for name in unsendable(roots):
        print(UNSENDABLE.format(name=name), file=sys.stderr, flush=True)
    return RelayClient(
        credentials,
        state_dir=state_dir(workspace),
        instance=instance(env),
        **options,
    )
