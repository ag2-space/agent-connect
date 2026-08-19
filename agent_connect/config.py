"""The config file: the same settings, written down instead of exported.

Every setting agent-connect reads is an environment variable, documented once in
`README.md` § Settings. That is a fine interface for a shell and a bad one for a
service: a manual install ends up depending on a "source this first" ritual
nobody records, and the launchd plist that survives a reboot has to carry the
bearer token in plaintext, in a file every process on the machine can read.

So the same keys can be written in a file instead:

    # ~/.agent-connect/config.env
    AGENT_CONNECT_TOKEN=...
    AGENT_CONNECT_ADAPTER=acp
    AGENT_CONNECT_ACP_AGENT=claude
    AGENT_CONNECT_REPO=/Users/me/agents

**The environment always wins.** A value already in the environment is left
exactly as it is and the file's is not applied — a config file is a default that
persists, not an authority that overrules the operator's own shell. Anything
else would make `AGENT_CONNECT_ADAPTER=codex agent-connect` a lie, and a setting
whose effect depends on where it was written is a setting nobody can reason
about. An empty environment value counts as unset, because every reader in this
package already treats `""` as "not configured".

**The file may set settings and nothing else.** Only the three key families the
Settings table documents are applied: `AGENT_CONNECT_*`, the relay client's
`REMOTE_TASK_*`, and `OLLAMA_HOST`. A file that could set any variable at all
would be an environment-injection surface wearing a config file's clothes —
`PATH` alone would decide which `codex` binary runs. Unknown keys are named on
stderr rather than dropped in silence: a setting that had no effect and said
nothing is how an operator spends an afternoon.

**It holds a bearer token, so its permissions are checked.** The installer
writes it 0600. A file readable by anyone else is loaded — refusing would brick
a working install over a warning's worth of problem — and complained about
loudly, once, at startup.

The format is deliberately not a shell script, so that `launch.sh` reading the
same file and this module reading it agree: `KEY=value`, one per line, `#`
comments, no substitution, no quoting rules, the value taken verbatim to the end
of the line (surrounding whitespace and one matching pair of quotes removed, and
that is the whole of it).
"""
from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, MutableMapping, Optional, Tuple

#: The environment variable naming the config file. The one thing an operator
#: may still have to export — and the launchd plist carries this instead of the
#: token, which is the whole point.
CONFIG_ENV = "AGENT_CONNECT_CONFIG"

#: Where the installer writes it, and where the Worker looks when nobody said.
#: With the file in its default place, `agent-connect` on its own is a complete
#: start: no flag, no export, no ritual.
DEFAULT_PATH = "~/.agent-connect/config.env"

#: The key families the Settings table documents, and the only ones this file
#: may set. See the module docstring for why the list is closed.
PREFIXES = ("AGENT_CONNECT_", "REMOTE_TASK_")
EXTRA_KEYS = frozenset({"OLLAMA_HOST"})


class ConfigError(Exception):
    """A config file that was named and could not be read."""


def accepts(key: str) -> bool:
    """Whether the file is allowed to set this key."""
    return key in EXTRA_KEYS or key.startswith(PREFIXES)


@dataclass(frozen=True)
class Config:
    """One config file, read but not yet acted on.

    Reading and applying are separate on purpose: what a file says is a fact a
    test can assert, and what the environment ends up holding is a second one.
    """

    path: Optional[Path] = None
    values: Dict[str, str] = field(default_factory=dict)
    #: Keys the file set that are not settings. Named, never silently dropped.
    ignored: Tuple[str, ...] = ()
    #: True when someone other than the owner can read a file holding a token.
    exposed: bool = False

    def apply(self, env: Optional[MutableMapping[str, str]] = None) -> Tuple[List[str], List[str]]:
        """Put the file's settings into the environment, where the env is silent.

        Returns `(applied, overridden)` — what the file supplied, and what it
        offered that the environment had already decided. Both are named in the
        startup line, because "the file I edited had no effect" is the failure
        this interface is most likely to produce.
        """
        env = os.environ if env is None else env
        applied: List[str] = []
        overridden: List[str] = []
        for key, value in self.values.items():
            if (env.get(key) or "").strip():
                overridden.append(key)
                continue
            env[key] = value
            applied.append(key)
        return applied, overridden


def parse(text: str, path: Optional[Path] = None) -> Config:
    """Read `KEY=value` lines. Not a shell: nothing here is evaluated.

    A line without `=`, a key with a character no environment variable may
    carry, and a key that is not a setting are each left out — the first two in
    silence (they are not addressed to us), the third in `ignored`, because a
    misspelt setting looks exactly like a setting that did not work.
    """
    values: Dict[str, str] = {}
    ignored: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum():
            continue
        if not accepts(key):
            ignored.append(key)
            continue
        values[key] = _unquote(value.strip())
    return Config(path=path, values=values, ignored=tuple(ignored))


def read(path: Path) -> Config:
    """The config file at `path`, or a `ConfigError` naming what went wrong."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read the config file {path}: {exc}") from exc
    config = parse(text, path)
    return Config(config.path, config.values, config.ignored, _exposed(path))


def locate(
    explicit: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Tuple[Optional[Path], bool]:
    """Which file to read, and whether someone asked for it by name.

    `--config` beats `AGENT_CONNECT_CONFIG` beats the default location, which is
    used only if something is actually there. The flag exists so that a start
    needs no environment at all; the variable exists so that a service unit can
    point at a file without naming it in a command line.

    The boolean is the difference between "the file you named is missing" —
    which must stop the Worker, since it is holding the token — and "there is no
    config file", which is the ordinary case for a shell-configured run.
    """
    env = os.environ if env is None else env
    named = (explicit or env.get(CONFIG_ENV) or "").strip()
    if named:
        return Path(named).expanduser(), True
    default = Path(DEFAULT_PATH).expanduser()
    return (default, False) if default.is_file() else (None, False)


def load(
    explicit: Optional[str] = None,
    env: Optional[MutableMapping[str, str]] = None,
) -> Optional[Config]:
    """Find the config file, apply it, and say out loud what it did.

    A file named explicitly and missing is fatal: it holds the credential, and
    starting without it produces a Worker that runs, pulls nothing, and looks
    healthy. A default location with nothing in it is not an error at all.
    """
    env = os.environ if env is None else env
    path, named = locate(explicit, env)
    if path is None:
        return None
    if named and not path.is_file():
        raise ConfigError(f"no config file at {path}")
    config = read(path)
    applied, overridden = config.apply(env)
    print(f"agent-connect: config {path} — {len(applied)} setting(s) applied"
          + (f", {len(overridden)} already set in the environment "
             f"({', '.join(sorted(overridden))}) and left alone" if overridden else ""))
    if config.ignored:
        print(f"agent-connect: WARNING — {path} sets {', '.join(sorted(set(config.ignored)))}, "
              "which agent-connect does not read. See README.md § Settings for the "
              "keys this file may carry.", file=sys.stderr, flush=True)
    if config.exposed:
        print(f"agent-connect: WARNING — {path} is readable by other users and it "
              "holds your agent's token. Fix it with: chmod 600 "
              f"{path}", file=sys.stderr, flush=True)
    return config


def _unquote(value: str) -> str:
    """One matching pair of surrounding quotes, removed. No other unescaping."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _exposed(path: Path) -> bool:
    """Whether anyone but the owner can read this file. False if unknowable."""
    try:
        mode = path.stat().st_mode
    except OSError:  # pragma: no cover — it was readable a moment ago
        return False
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))
