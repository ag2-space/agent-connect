"""The Relay Client, built from this Worker's settings.

The transport is no longer a foreign process writing files into a directory —
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
  (`ag2_relay_client.credentials`) — the naive literal-`|` split that
  `roomops.py` still carries is the fourth copy of that parse in this workspace,
  and transport-seam ticket 09 is where it dissolves.
* **the state directory**, `<workspace>/relay/`. It hangs off the workspace
  because everything else the Worker owns on disk already does, and because a
  Worker's durable state should not need a setting of its own to be found. The
  journal under it is what makes a restart re-complete a Task rather than
  re-execute it — the guarantee the archived task file only pretended to give.
* **the instance name**, which namespaces that directory. One Worker, one
  bearer, one state dir: two Workers sharing one would share a journal, and a
  shared journal is two Workers each believing the other's Tasks are answered.
* **nothing else.** Media directories and egress allowlist roots arrive with
  transport-seam tickets 08 and 09; there is deliberately no placeholder for
  them here, because a construction argument nobody passes is a setting nobody
  documents.

**No credential is a refusal, not a degradation.** A Worker without one has no
inbound seam at all: it would start, report `serving`, and never receive a
thing. `worker.main()` turns the `None` this module returns into the sentence an
operator can act on, at the documented status path, the same way a missing
Adapter is handled.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from ag2_relay_client import RelayClient, TokenSource
from ag2_relay_client.state import valid_instance_name

from .status import INSTANCE_ENV, instance_name

#: The token an operator gets from the Agent Portal. `REMOTE_TASK_TOKEN` is the
#: name the old two-process launcher exported for the relay client's own
#: process, and it is still accepted: one credential, under either name it has
#: ever had, so an existing install keeps working across the swap.
TOKEN_ENV = "AGENT_CONNECT_TOKEN"
LEGACY_TOKEN_ENV = "REMOTE_TASK_TOKEN"

#: Only for a bare secret that carries no gateway of its own. A combined token
#: names its own gateway and that one wins — see `TokenSource`, which says so
#: out loud when the two disagree.
URL_ENV = "REMOTE_TASK_URL"

#: The library's state, under the workspace the Worker already owns.
STATE_NAME = "relay"

#: The instance name used when the operator named none. It is the library's own
#: default too, and it is a real name rather than an empty string because it
#: becomes a directory component.
DEFAULT_INSTANCE = "default"


def token(env: Optional[Mapping[str, str]] = None) -> str:
    """The onboarding token as written, under either of its names."""
    env = os.environ if env is None else env
    return (env.get(LEGACY_TOKEN_ENV) or env.get(TOKEN_ENV) or "").strip()


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
    **options,
) -> Optional[RelayClient]:
    """The Relay Client this Worker speaks through, or `None` with no token.

    Constructed, not started: a bad credential is a startup refusal and should
    be found before the Adapter's preflight spends a minute proving the Local
    Agent is fine. `worker.main()` calls `start()` once everything that can
    refuse has declined to.
    """
    env = os.environ if env is None else env
    raw = token(env)
    if not raw:
        return None
    credentials = TokenSource(token=raw, base_url=(env.get(URL_ENV) or "").strip())
    return RelayClient(
        credentials,
        state_dir=state_dir(workspace),
        instance=instance(env),
        **options,
    )
