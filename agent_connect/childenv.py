"""The environment a worker hands to the agent processes it starts.

An agent never needs the worker's credentials, and an inherited relay token
would let it act as the worker on the relay.
"""
from __future__ import annotations

import os
from typing import Optional

#: Dropped by prefix: Sutando desktop installs add per-lane suffixes
#: (AG2_REMOTE_TOKEN_<LANE>).
WORKER_SECRET_PREFIXES = ("AGENT_CONNECT_TOKEN", "REMOTE_TASK_TOKEN", "AG2_REMOTE_TOKEN")
WORKER_SECRET_NAMES = ("AGENT_CONNECT_ACP_TOKEN", "AGENT_CONNECT_CONFIG")


def is_worker_secret(name: str) -> bool:
    return name in WORKER_SECRET_NAMES or name.startswith(WORKER_SECRET_PREFIXES)


def child_env(extra: Optional[dict] = None) -> dict:
    """The worker's environment minus its secrets, then `extra` on top."""
    env = {k: v for k, v in os.environ.items() if not is_worker_secret(k)}
    env.update(extra or {})
    return env
