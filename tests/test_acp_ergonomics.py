"""Installing and running the ACP Adapter without inside knowledge.

Three operator failures are what this file is about, and each is asserted at the
seam the operator actually meets:

1. **"What do I set?"** — naming an agent is enough, and naming a command still
   overrides the preset, so an ACP Agent nobody anticipated is never blocked.
2. **"It exploded."** — a bridge that is not installed produces the package name
   and the line that installs it.
3. **"It said please log in, in the room."** — an unauthenticated Local Agent
   stops the Worker at startup with the login command, before anybody asks it
   anything.

The startup check runs against the real fake ACP Agent child process, which can
be scripted to advertise `authMethods` (what a logged-out agent does) or not
(what a logged-in one does).

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: .venv/bin/python tests/test_acp_ergonomics.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

try:
    from agent_connect import worker
    from agent_connect.acp.core import AcpError
    from agent_connect.adapters.acp import (
        AGENT_ENV,
        BRIDGE_PACKAGE,
        BRIDGE_SPEC,
        BRIDGE_VERSION,
        COMMAND_ENV,
        PRESETS,
        SKIP_AUTH_ENV,
        AcpAdapter,
        command_from_env,
        install_advice,
        preset_for,
        resolve_command,
    )
    from agent_connect.events import TurnContext
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_ergonomics.py: {exc}\n"
        "This test has a dependency (see docs/adr/0001). Run it from an\n"
        "environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python tests/test_acp_ergonomics.py"
    )

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Env:
    """Process environment for the duration of a `with`, restored after."""

    def __init__(self, **values):
        self._values = values
        self._saved: dict = {}

    def __enter__(self):
        for key, value in self._values.items():
            self._saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


# --- 1) a preset is enough, and a command always wins ----------------------

check(resolve_command({AGENT_ENV: "claude"})[-1].endswith(("claude-agent-acp", BRIDGE_SPEC)),
      "naming the claude preset resolves to a command without any further setting")

check(resolve_command({AGENT_ENV: "claude", COMMAND_ENV: "my-agent --acp"})
      == ["my-agent", "--acp"],
      "an explicit command overrides the preset — an unanticipated ACP Agent is "
      "never blocked by the preset table")

check(resolve_command({COMMAND_ENV: "my-agent --acp"}) == ["my-agent", "--acp"],
      "and works with no preset named at all")

try:
    resolve_command({})
    unset = None
except AcpError as exc:
    unset = str(exc)
check(unset is not None and AGENT_ENV in unset and COMMAND_ENV in unset,
      "an operator who set neither is told about both, with an example of each")

try:
    preset_for("cline")
    unknown = None
except AcpError as exc:
    unknown = str(exc)
check(unknown is not None and "claude" in unknown and COMMAND_ENV in unknown,
      "an unknown preset names the presets that exist AND the way past them")

check(resolve_command({AGENT_ENV: " CLAUDE "}),
      "a preset name is matched after stripping and lowercasing, like a person "
      "would type it")

# The preset prefers an installed copy, and falls back to a pinned fetch.
with tempfile.TemporaryDirectory() as tmp:
    installed = Path(tmp) / "claude-agent-acp"
    installed.write_text("#!/bin/sh\n")
    installed.chmod(installed.stat().st_mode | stat.S_IEXEC)
    check(PRESETS["claude"].command({"PATH": tmp}) == [str(installed)],
          "a preset runs the installed bridge when there is one")
    empty = Path(tmp) / "empty"
    empty.mkdir()
    fallback = PRESETS["claude"].command({"PATH": str(empty)})
    check(BRIDGE_SPEC in fallback and "npx" in fallback,
          "and falls back to fetching the PINNED spec, never a floating one")

check(BRIDGE_SPEC == f"{BRIDGE_PACKAGE}@{BRIDGE_VERSION}"
      and BRIDGE_PACKAGE == "@agentclientprotocol/claude-agent-acp",
      "the pinned bridge is the CURRENT package name, not the one it renamed from")
check(BRIDGE_VERSION.count(".") == 2 and "latest" not in BRIDGE_SPEC,
      f"pinned to an exact version ({BRIDGE_VERSION})")

for name, preset in sorted(PRESETS.items()):
    check(preset.install and preset.login,
          f"the {name} preset says how to install it and how to log in")
check(PRESETS["claude"].verified and not PRESETS["gemini"].verified,
      "presets say whether a real Turn was observed against them — claude was, "
      "gemini was read from documentation")

# The pure function ticket 04 left alone still behaves as it did.
check(command_from_env({COMMAND_ENV: "npx @scope/pkg --acp"})
      == ["npx", "@scope/pkg", "--acp"],
      "command_from_env is unchanged: still pure, still shell-split")


# --- 2) a missing bridge names what to install -----------------------------

advice = install_advice(["npx", "-y", BRIDGE_SPEC], {AGENT_ENV: "claude"})
check(BRIDGE_SPEC in advice and "npm install -g" in advice,
      "a missing preset bridge names the package AND the line that installs it")
check("Node" in advice,
      "and says where npx comes from, since that is what is actually missing")

advice = install_advice(["my-agent"], {COMMAND_ENV: "my-agent"})
check("my-agent" in advice and COMMAND_ENV in advice and "npm" not in advice,
      "a missing command the operator wrote themselves says so honestly rather "
      "than recommending a package it knows nothing about")


async def preflight_of(adapter):
    return await adapter.preflight()


problem = asyncio.run(preflight_of(AcpAdapter(command=["definitely-not-a-bridge"])))
check(isinstance(problem, str) and "definitely-not-a-bridge" in problem,
      "preflight REPORTS a missing bridge as a sentence — no traceback, no raise")
check("not installed" in problem,
      "and says the thing an operator can act on: it is not installed")

with Env(**{COMMAND_ENV: None, AGENT_ENV: None}):
    problem = asyncio.run(preflight_of(AcpAdapter()))
check(problem is not None and AGENT_ENV in problem,
      "an unconfigured Worker fails its own startup check rather than serving")


# --- 3) an unauthenticated Local Agent stops the Worker at startup ---------


class FakeAgentBench:
    """An AcpAdapter pointed at a scripted fake ACP Agent."""

    def __init__(self, script: dict):
        self._dir = tempfile.TemporaryDirectory()
        base = Path(self._dir.name)
        self.script_path = base / "script.json"
        self.script_path.write_text(json.dumps(script))
        self.report_path = base / "report.json"
        self.adapter = AcpAdapter(command=[sys.executable, FAKE, str(self.script_path)])

    def preflight(self, **env):
        with Env(FAKE_ACP_REPORT=str(self.report_path), **env):
            return asyncio.run(asyncio.wait_for(self.adapter.preflight(), timeout=30))

    def report(self):
        if not self.report_path.exists():
            return None
        return json.loads(self.report_path.read_text())


LOGGED_OUT = {"authMethods": [
    {"id": "claude-login", "name": "Log in with Claude Code",
     "description": "Run `claude` and use /login"},
]}

bench = FakeAgentBench(LOGGED_OUT)
problem = bench.preflight(**{AGENT_ENV: "claude", SKIP_AUTH_ENV: None})
check(problem is not None, "an ACP Agent advertising authMethods stops the Worker")
check("not logged in" in problem,
      "and says what is wrong in words an operator recognises")
check(PRESETS["claude"].login.split("(")[0].strip() in problem,
      "and names the command to run, from the preset")
check("Log in with Claude Code" in problem,
      "and repeats what the agent itself offered, for an agent with no preset")
check("never opens an interactive terminal" in problem,
      "and states plainly that agent-connect will not do it for them")
check(SKIP_AUTH_ENV in problem,
      "and offers the escape hatch, because this check is verified in one "
      "direction only")

report = bench.report()
check(report is not None and report["methods"] == ["initialize"],
      f"the startup check sends `initialize` and NOTHING else ({report['methods']})")
check(report["sessions"] == [] and report["prompts"] == [],
      "no Session is opened and no prompt is sent — the check costs no tokens")

bench = FakeAgentBench(LOGGED_OUT)
skipped = bench.preflight(**{AGENT_ENV: "claude", SKIP_AUTH_ENV: "1"})
check(skipped is None,
      f"{SKIP_AUTH_ENV}=1 starts the Worker anyway, for an agent whose "
      "authMethods mean something else")

bench = FakeAgentBench({})
ok_problem = bench.preflight(**{AGENT_ENV: "claude", SKIP_AUTH_ENV: None})
check(ok_problem is None,
      "an authenticated ACP Agent — no authMethods — passes the startup check")
check("fake-acp-agent" in bench.adapter.describe(),
      "and the Worker can say which agent it found")


# --- 4) the Worker itself stops, rather than answering a room with a login --

class Adapterless:
    """An Adapter with no preflight — the five synchronous ones."""

    name = "codex-ish"


worker.preflight(Adapterless())
check(True, "an Adapter without a preflight starts as it always did")

bench = FakeAgentBench(LOGGED_OUT)
with Env(FAKE_ACP_REPORT=str(bench.report_path), **{AGENT_ENV: "claude",
                                                    SKIP_AUTH_ENV: None}):
    try:
        worker.preflight(bench.adapter)
        stopped = None
    except SystemExit as exc:
        stopped = str(exc)
check(stopped is not None and "not logged in" in stopped,
      "the Worker exits at startup on an unauthenticated Local Agent, so the "
      "first person in a room gets an answer rather than a login instruction")

bench = FakeAgentBench({})
with Env(FAKE_ACP_REPORT=str(bench.report_path), **{AGENT_ENV: "claude"}):
    try:
        worker.preflight(bench.adapter)
        started = True
    except SystemExit:
        started = False
check(started, "and starts normally when the Local Agent is logged in")


# --- 5) a missing bridge mid-Turn is advice too, not a traceback -----------

async def one_turn(adapter, prompt="hello"):
    ctx = TurnContext(prompt=prompt, task_id="t1", room="!r", access_tier="owner",
                      cwd=os.getcwd())
    return await worker.run_turn(adapter, ctx)


with Env(**{AGENT_ENV: "claude", COMMAND_ENV: None}):
    text = asyncio.run(one_turn(AcpAdapter(command=["definitely-not-a-bridge"])))
check("not installed" in text and "npm install -g" in text,
      "a bridge that goes missing between startup and a Turn still produces "
      "install advice in the room, not a stack trace")

print("\n" + ("PASS — acp ergonomics green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
