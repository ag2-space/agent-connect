"""Tests for worker._resolve_repo (the ~/agents default + TCC-path warning).

Regression for the pwd-default friction (owner-caught): defaulting the agent's
working dir to the launch cwd put it under a macOS TCC-protected location
(e.g. ~/Documents), producing opaque file-access failures. The fix defaults to
a dedicated ~/agents dir and warns loudly if the resolved repo is TCC-protected.

Run: python3 test_worker_repo.py
"""
import contextlib
import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from agent_connect.worker import _resolve_repo

fails = 0


def check(name, cond):
    global fails
    if cond:
        print(f"ok  - {name}")
    else:
        fails += 1
        print(f"FAIL- {name}")


with tempfile.TemporaryDirectory() as home:
    home_p = Path(home)

    # 1. explicit AGENT_CONNECT_REPO wins verbatim (expanduser applied).
    with tempfile.TemporaryDirectory() as d:
        with mock.patch.dict(os.environ, {"AGENT_CONNECT_REPO": d}):
            check("explicit env wins", str(_resolve_repo()) == d)

    # 2. no env -> defaults to ~/agents and creates it.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AGENT_CONNECT_REPO", None)
        with mock.patch.object(Path, "home", staticmethod(lambda: home_p)):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                r = _resolve_repo()
            check("default is ~/agents", r == home_p / "agents")
            check("default dir created", r.exists())
            check("default prints where it landed", "defaulting repo to" in buf.getvalue())

    # 3. a repo under a TCC-protected dir (~/Documents) warns loudly.
    with mock.patch.object(Path, "home", staticmethod(lambda: home_p)):
        with mock.patch.dict(os.environ, {"AGENT_CONNECT_REPO": str(home_p / "Documents" / "a")}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _resolve_repo()
            out = buf.getvalue()
            check("TCC path warns", "WARNING" in out and "TCC-protected" in out and "Documents" in out)

    # 4. a repo NOT under a protected dir does not warn.
    with mock.patch.object(Path, "home", staticmethod(lambda: home_p)):
        with mock.patch.dict(os.environ, {"AGENT_CONNECT_REPO": str(home_p / "agents")}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                _resolve_repo()
            check("safe path does not warn", "WARNING" not in buf.getvalue())

if fails:
    raise SystemExit(f"{fails} check(s) failed")
print("all ok")
