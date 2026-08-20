"""Where the agent works when nobody said: `~/agents`, and a warning if not.

Regression for the pwd-default friction (owner-caught): defaulting the agent's
working directory to the launch cwd put it under a macOS TCC-protected location
(e.g. `~/Documents`), producing opaque file-access failures — the agent is told
it may write and then is not allowed to. The fix defaults to a dedicated
`~/agents` and warns loudly when the resolved directory is protected anyway.

Run: python3 tests/test_worker_repo.py   (no dependencies)
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from _queue import child_env
from agent_connect.worker import _resolve_repo

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


@contextlib.contextmanager
def home_at(path: Path):
    """Run with `Path.home()` somewhere disposable, so nothing real is created."""
    with mock.patch.object(Path, "home", staticmethod(lambda: path)):
        yield


def resolve(repo=None, home=None):
    """`_resolve_repo`, with what it printed. Both are what an operator sees."""
    env = {} if repo is None else {"AGENT_CONNECT_REPO": str(repo)}
    with mock.patch.dict(os.environ, env, clear=False):
        if repo is None:
            os.environ.pop("AGENT_CONNECT_REPO", None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with home_at(home) if home else contextlib.nullcontext():
                resolved = _resolve_repo()
    return resolved, buf.getvalue()


with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)

    with tempfile.TemporaryDirectory() as explicit:
        resolved, said = resolve(repo=explicit)
        check(str(resolved) == explicit,
              "an explicit AGENT_CONNECT_REPO is where the agent works, verbatim")

    resolved, said = resolve(home=home)
    check(resolved == home / "agents",
          "with nothing set the agent works in ~/agents — not in whatever "
          "directory the operator happened to launch from")
    check(resolved.exists(), "which is created rather than left to fail later")
    check("defaulting repo to" in said,
          "and said out loud: an invisible default is how an agent ends up in "
          "the wrong folder")

    resolved, said = resolve(repo=home / "Documents" / "a", home=home)
    check("WARNING" in said and "TCC-protected" in said and "Documents" in said,
          "a working directory under a macOS privacy-protected folder is warned "
          "about, because the failure it causes says only 'operation not permitted'")
    check("AGENT_CONNECT_REPO" in said, "and the warning names what to change")

    resolved, said = resolve(repo=home / "agents", home=home)
    check("WARNING" not in said, "a directory outside those folders warns about nothing")

# --- and the entry point actually uses it -----------------------------------
# `_resolve_repo` being right is worth nothing if `main()` stops calling it, and
# that is a live risk rather than a hypothetical: this default arrived on a
# branch that rewrote `main()`, and so did the status file, so a careless merge
# of the two silently restores the old `os.getcwd()` line. Every assertion above
# would still pass. This one would not.

with tempfile.TemporaryDirectory() as home:
    log = Path(home) / "out.log"
    child = child_env(**{
        "HOME": home,
        "AGENT_CONNECT_ADAPTER": "ollama",
        "AGENT_CONNECT_WORKSPACE": str(Path(home) / "ws"),
        "AGENT_CONNECT_POLL": "0.05",
    })
    with open(log, "w") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_connect"],
            cwd=str(_bootstrap.ROOT), env=child,
            stdout=sink, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if "agent-connect worker:" in log.read_text() or proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=20)
    said = log.read_text()
    check(f"repo={home}/agents" in said,
          "a Worker started with no AGENT_CONNECT_REPO works in ~/agents — the "
          "entry point uses the default, not just the function that computes it")
    check("defaulting repo to" in said, "and says so where an operator will see it")

print("\n" + ("PASS — the agent's working directory green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
