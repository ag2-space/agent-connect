"""Tests for the ACP core against the fake ACP Agent.

The fake is a real child process speaking real JSON-RPC 2.0 over stdio
(`fake_acp_agent.py`), scripted per test and reporting back what it received.
Nothing here is mocked: every assertion is about what actually crossed a pipe.

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: python3 tests/test_acp_core.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import ast
import asyncio
import json
import os
import re
import sys
import tempfile
import tokenize
from pathlib import Path

try:
    from agent_connect.acp import (
        AcpAgentGone,
        AcpAuthRequired,
        AcpClient,
        AcpCommandMissing,
        AcpError,
        PermissionRequest,
        SessionResumeRefused,
        Update,
    )
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_core.py: {exc}\n"
        "This is the first test here with a dependency (see docs/adr/0001). Run it\n"
        "from an environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python tests/test_acp_core.py"
    )

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Fake:
    """One scripted run of the fake ACP Agent, plus the report it wrote."""

    def __init__(self, script: dict):
        self._dir = tempfile.TemporaryDirectory()
        self.script_path = os.path.join(self._dir.name, "script.json")
        self.report_path = os.path.join(self._dir.name, "report.json")
        Path(self.script_path).write_text(json.dumps(script))

    @property
    def command(self):
        return [sys.executable, FAKE, self.script_path]

    @property
    def env(self):
        return {"FAKE_ACP_REPORT": self.report_path}

    def report(self) -> dict:
        return json.loads(Path(self.report_path).read_text())

    def client(self, **kwargs):
        return AcpClient.spawn(self.command, env=self.env, **kwargs)


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


# --- connect, open a Session with a working directory, get an answer --------


async def _happy_path():
    fake = Fake({"turns": [{"actions": [{"type": "message", "text": "PONG"}],
                            "stopReason": "end_turn"}]})
    async with fake.client() as client:
        agent = await client.initialize()
        session_id = await client.new_session(cwd="/some/repo")
        turn = await client.prompt(session_id, "ping")
    return agent, session_id, turn, fake.report()


agent, session_id, turn, report = run(_happy_path())
check(agent.name == "fake-acp-agent", "initialize returns the ACP Agent's identity")
check(agent.protocol_version == 1, "protocol version 1 is negotiated")
check(agent.can_resume_sessions, "advertised loadSession capability is readable")
check(agent.accepts_prompt_content("image"), "advertised prompt capabilities are readable")
check(not agent.accepts_prompt_content("audio"), "unadvertised prompt content is not claimed")
check(turn.text == "PONG", "the agent's text answer comes back")
check(turn.stop_reason == "end_turn", "the stop reason comes back")

check(len(report["sessions"]) == 1, "exactly one Session was opened")
check(report["sessions"][0]["cwd"] == "/some/repo",
      "the fake reports the working directory the Session was opened with")
check(report["sessions"][0]["sessionId"] == session_id,
      "the session identifier the caller got is the one the agent issued")
check([p["prompt"] for p in report["prompts"]] ==
      [[{"type": "text", "text": "ping"}]],
      "the fake reports the prompt it received")

# --- the Client's own capabilities: no terminal, no filesystem --------------

# The protocol defaults every client capability to off and the schema library
# omits defaults from the wire, so "not advertised" is the assertion to make —
# absent and explicitly false say the same thing to the ACP Agent.
advertised = report["initialize"].get("clientCapabilities") or {}
fs = advertised.get("fs") or {}
check(not fs.get("readTextFile") and not fs.get("writeTextFile"),
      "the Worker advertises no filesystem methods")
check(not advertised.get("terminal"),
      "the Worker never offers terminal provisioning")

# --- progress notifications are surfaced, not swallowed ---------------------


async def _progress():
    fake = Fake({"turns": [{"actions": [
        {"type": "thought", "text": "hmm"},
        {"type": "tool_call", "toolCallId": "t1", "title": "read worker.py"},
        {"type": "message", "text": "the "},
        {"type": "message", "text": "answer"},
    ], "stopReason": "end_turn"}]})
    seen = []
    async with fake.client(on_update=seen.append) as client:
        await client.initialize()
        session_id = await client.new_session(cwd="/repo")
        turn = await client.prompt(session_id, "go")
    return seen, turn


seen, turn = run(_progress())
check([u.kind for u in seen] ==
      ["agent_thought_chunk", "tool_call", "agent_message_chunk", "agent_message_chunk"],
      "every session/update reaches the caller's handler, in order")
check(all(isinstance(u, Update) for u in seen), "updates arrive in our own shape")
check(seen[1].raw["title"] == "read worker.py",
      "the raw notification payload is carried through intact")
check([u.kind for u in turn.updates] == [u.kind for u in seen],
      "the Turn also collects its updates")
check(turn.text == "the answer",
      "only agent message chunks build the answer — thinking stays out of it")

# --- the Permission Policy: the Worker answers, and the agent sees it -------


async def _permission(handler):
    fake = Fake({"turns": [{"actions": [
        {"type": "permission",
         "toolCall": {"toolCallId": "t1", "title": "write /etc/passwd"},
         "options": [{"optionId": "yes", "name": "Allow", "kind": "allow_once"},
                     {"optionId": "no", "name": "Reject", "kind": "reject_once"}]},
        {"type": "message", "text": "done"},
    ], "stopReason": "end_turn"}]})
    asked = []

    def policy(request: PermissionRequest):
        asked.append(request)
        return handler(request)

    async with fake.client(permission_handler=policy) as client:
        await client.initialize()
        session_id = await client.new_session(cwd="/repo")
        await client.prompt(session_id, "go")
    return asked, fake.report()


asked, report = run(_permission(lambda r: r.option_of_kind("allow_once")))
check(len(asked) == 1, "the permission request reaches the Worker's handler")
check(asked[0].tool_call["title"] == "write /etc/passwd",
      "the handler sees what the agent wants to do")
check(report["permissions"][0]["answer"] ==
      {"outcome": {"outcome": "selected", "optionId": "yes"}},
      "the fake reports the answer it got — an allowed request")

asked, report = run(_permission(lambda r: None))
check(report["permissions"][0]["answer"] == {"outcome": {"outcome": "cancelled"}},
      "the fake reports the answer it got — a rejected request")


async def _default_policy():
    fake = Fake({"turns": [{"actions": [
        {"type": "permission"}, {"type": "message", "text": "done"}]}]})
    async with fake.client() as client:  # no handler passed
        await client.initialize()
        session_id = await client.new_session(cwd="/repo")
        await client.prompt(session_id, "go")
    return fake.report()


report = run(_default_policy())
check(report["permissions"][0]["answer"] == {"outcome": {"outcome": "cancelled"}},
      "with no policy injected the core fails closed and refuses")

# --- Session resumption: refused, and history replayed ----------------------


async def _refused_resume():
    fake = Fake({"loadSessionError": {"code": -32000, "message": "session expired"}})
    async with fake.client() as client:
        await client.initialize()
        try:
            await client.load_session("old-session", cwd="/repo")
            return None, fake.report()
        except SessionResumeRefused as exc:
            return exc, fake.report()


exc, report = run(_refused_resume())
check(exc is not None, "a refused resumption raises SessionResumeRefused")
check("session expired" in str(exc), "the agent's reason survives into the error")
check(report["sessions"][0]["refused"] is True,
      "the fake reports that resumption was attempted and refused")


async def _no_resume_capability():
    fake = Fake({"agentCapabilities": {"loadSession": False}})
    async with fake.client() as client:
        await client.initialize()
        try:
            await client.load_session("old", cwd="/repo")
            return None, fake.report()
        except SessionResumeRefused as exc:
            return exc, fake.report()


exc, report = run(_no_resume_capability())
check(exc is not None, "an agent that does not advertise resumption refuses it")
check("session/load" not in report["methods"],
      "no resumption is attempted against an agent that cannot do it")


async def _replay():
    fake = Fake({"loadSessionReplay": [
        {"sessionUpdate": "user_message_chunk",
         "content": {"type": "text", "text": "what did I ask before?"}},
        {"sessionUpdate": "agent_message_chunk",
         "content": {"type": "text", "text": "you asked about worker.py"}},
    ]})
    seen = []
    async with fake.client(on_update=seen.append) as client:
        await client.initialize()
        await client.load_session("old-session", cwd="/repo")
    return seen


seen = run(_replay())
check([u.kind for u in seen] == ["user_message_chunk", "agent_message_chunk"],
      "a resumed Session replays its history as updates")
check(seen[1].text == "you asked about worker.py",
      "replayed history is visible to the caller, which must suppress it itself")

# --- delaying past a deadline ----------------------------------------------


async def _slow():
    fake = Fake({"turns": [{"actions": [
        {"type": "message", "text": "starting"},
        {"type": "sleep", "seconds": 30},
        {"type": "message", "text": "never seen"},
    ]}]})
    seen = []
    async with fake.client(on_update=seen.append) as client:
        await client.initialize()
        session_id = await client.new_session(cwd="/repo")
        try:
            await asyncio.wait_for(client.prompt(session_id, "go"), timeout=1.5)
            return False, seen
        except asyncio.TimeoutError:
            return True, seen


timed_out, seen = run(_slow())
check(timed_out, "an ACP Agent can be scripted to delay past a deadline")
check([u.text for u in seen] == ["starting"],
      "work produced before the deadline is already in the caller's hands")

# --- exiting mid-Turn -------------------------------------------------------


async def _dies():
    fake = Fake({"turns": [{"actions": [
        {"type": "message", "text": "half an "},
        {"type": "exit", "code": 3},
    ]}]})
    seen = []
    async with fake.client(on_update=seen.append) as client:
        await client.initialize()
        session_id = await client.new_session(cwd="/repo")
        try:
            await client.prompt(session_id, "go")
            return None, seen, fake.report()
        except AcpAgentGone as exc:
            return exc, seen, fake.report()


exc, seen, report = run(_dies())
check(exc is not None, "an ACP Agent dying mid-Turn raises AcpAgentGone, not a hang")
check("code 3" in str(exc), "the exit code is reported")
check([u.text for u in seen] == ["half an "],
      "output produced before the death is not thrown away")
check(len(report["prompts"]) == 1,
      "a fake told to die mid-Turn still reports what it received")

# --- cancellation through the protocol --------------------------------------


async def _cancel():
    fake = Fake({"turns": [{"actions": [
        {"type": "message", "text": "working"},
        {"type": "sleep", "seconds": 30},
    ]}]})
    async with fake.client() as client:
        await client.initialize()
        session_id = await client.new_session(cwd="/repo")
        turn = asyncio.ensure_future(client.prompt(session_id, "go"))
        await asyncio.sleep(0.5)
        await client.cancel(session_id)
        await asyncio.sleep(0.3)
        turn.cancel()
    return fake.report()


report = run(_cancel())
check(report["cancelled"] == [report["sessions"][0]["sessionId"]],
      "session/cancel reaches the agent while a Turn is running")

# --- Sessions are keyed by the caller, and modes are observable -------------


async def _two_sessions():
    fake = Fake({})
    async with fake.client() as client:
        await client.initialize()
        a = await client.new_session(cwd="/repo")
        b = await client.new_session(cwd="/repo")
        await client.set_session_mode(a, "acceptEdits")
        await client.prompt(a, "one")
        await client.prompt(b, "two")
    return a, b, fake.report()


a, b, report = run(_two_sessions())
check(a != b, "two Sessions on one ACP Agent are distinct")
check(len(report["sessions"]) == 2, "the fake reports how many Sessions were opened")
check(report["modes"] == [{"sessionId": a, "modeId": "acceptEdits"}],
      "the fake reports the mode a Session was put into")
check([p["sessionId"] for p in report["prompts"]] == [a, b],
      "each prompt is reported against the Session it ran on")

# --- a missing ACP Agent is distinguishable from a broken one ---------------


async def _missing():
    try:
        async with AcpClient.spawn(["definitely-not-a-real-bridge-xyz"]):
            return None
    except Exception as exc:
        return exc


exc = run(_missing())
check(exc is not None and "not found" in str(exc),
      "a missing ACP Agent command fails with a clear error, not a traceback")
check(isinstance(exc, AcpCommandMissing),
      "and has its own class, so the caller need not match on the wording")

# --- an Agent's own explanation survives into the error ---------------------
# JSON-RPC puts the message ("Internal error") in `message` and the cause in
# `data`. Keeping only the first leaves a failed Turn with nothing to report.


async def _prompt_error(script_error):
    fake = Fake({"promptError": script_error})
    async with fake.client() as client:
        await client.initialize()
        session_id = await client.new_session(cwd="/repo")
        try:
            await client.prompt(session_id, "go")
            return None
        except AcpError as exc:
            return exc


exc = run(_prompt_error({
    "code": -32603, "message": "Internal error",
    "data": {"reason": "API key not valid. Please pass a valid API key.",
             "type": "ClientError"},
}))
check(exc is not None, "a prompt error raises")
check("API key not valid" in str(exc), "`data.reason` reaches the error text")
check("Internal error" in str(exc), "the JSON-RPC message is kept alongside it")
check(getattr(exc, "data", None) == {
          "reason": "API key not valid. Please pass a valid API key.",
          "type": "ClientError"},
      "the whole `data` member is kept for a caller that wants to branch on it")

exc = run(_prompt_error({
    "code": -32603, "message": "Internal error",
    "data": {"details": "Run `ag2-assistant onboard` in a terminal."},
}))
check("ag2-assistant onboard" in str(exc),
      "`details` is read as well as `reason` — the spelling is the Agent's")

exc = run(_prompt_error({
    "code": -32602, "message": "Invalid params", "data": {"uri": "file:///x"},
}))
check(str(exc) == "Invalid params",
      "a `data` with no operator-facing key adds nothing to the message")
check(getattr(exc, "data", None) == {"uri": "file:///x"},
      "and is still carried for the caller")

exc = run(_prompt_error({"code": -32603, "message": "Internal error"}))
check(str(exc) == "Internal error", "an error with no `data` reads as it always did")
check(getattr(exc, "data", None) is None, "and carries none")

exc = run(_prompt_error({
    "code": -32000, "message": "Authentication required",
    "data": {"reason": "Call authenticate first."},
}))
check(isinstance(exc, AcpAuthRequired),
      "the auth code still selects AcpAuthRequired")
check("Call authenticate first." in str(exc),
      "and the auth refusal carries its reason too")

long_reason = "x" * 900
exc = run(_prompt_error({
    "code": -32603, "message": "Internal error", "data": {"reason": long_reason},
}))
check(len(str(exc)) < 600, "a runaway `data.reason` is bounded, as the stderr tail is")


async def _refused_resume_data():
    fake = Fake({"loadSessionError": {
        "code": -32002, "message": "Resource not found",
        "data": {"reason": "no such session", "category": "session_missing"},
    }})
    async with fake.client() as client:
        await client.initialize()
        try:
            await client.load_session("old-session", cwd="/repo")
            return None
        except SessionResumeRefused as exc:
            return exc


exc = run(_refused_resume_data())
check("no such session" in str(exc), "a refused resumption keeps its reason")
check((getattr(exc, "data", None) or {}).get("category") == "session_missing",
      "and `category` — the stable key — survives for branching")


# --- the core knows nothing about Tasks, rooms or the relay -----------------

core_path = _bootstrap.ROOT / "agent_connect" / "acp" / "core.py"
core_source = core_path.read_text()

imported = set()
for node in ast.walk(ast.parse(core_source)):
    if isinstance(node, ast.Import):
        imported.update(a.name for a in node.names)
    elif isinstance(node, ast.ImportFrom):
        imported.add("." * (node.level or 0) + (node.module or ""))
check(not any("worker" in m or "adapter" in m for m in imported),
      "the core imports neither the Worker nor the Adapter registry")
check(not any(m.startswith(".") for m in imported),
      "the core imports nothing from the rest of the package at all")

# Executable code only — the prose is allowed to mention what the core is *not*
# for; the code is not. Comments and strings (including docstrings) are dropped.
code_tokens = []
with tokenize.open(str(core_path)) as fh:
    for tok in tokenize.generate_tokens(fh.readline):
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            code_tokens.append(tok.string)
code = " ".join(code_tokens).lower()

for forbidden in ("task", "room", "relay", "tier", "adapter", "matrix"):
    check(re.search(r"\b%s" % forbidden, code) is None,
          f"the core's code carries no knowledge of {forbidden!r}")

print("\n" + ("PASS — acp core green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
