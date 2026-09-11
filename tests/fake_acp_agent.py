#!/usr/bin/env python3
"""A scriptable fake ACP Agent — a real child process, real JSON-RPC 2.0 over stdio.

This is a *test artefact*, not shipped code (`pyproject.toml` packages only
`agent_connect*`). Every later ticket in the ACP feature tests through it, so it
is written to be read and extended rather than thrown away.

It deliberately does **not** use the `agent-client-protocol` library that
`agent_connect.acp.core` uses. It hand-rolls the wire — newline-delimited JSON
objects on stdin/stdout — so that a test exercises the protocol rather than one
library talking to itself. If the library changes how it frames or names things,
these tests notice.

## Running it

    python3 fake_acp_agent.py            # script from $FAKE_ACP_SCRIPT, or defaults
    python3 fake_acp_agent.py script.json
    python3 fake_acp_agent.py script.json report.json

Environment:

  FAKE_ACP_SCRIPT   path to the script JSON (argv[1] wins over it)
  FAKE_ACP_REPORT   path to write the report JSON to (argv[2] wins over it —
                    two Turns running at once cannot share one environment
                    variable, and concurrency across rooms is a thing tests
                    have to observe)

## The script

A JSON object. Every key is optional; the defaults give a polite agent that
answers every prompt with one message and stops with `end_turn`.

    {
      "agentCapabilities": {...},     # merged over the default capabilities
      "authMethods": [...],           # advertised by initialize
      "newSessionError": {"code": -32000, "message": "..."},
      "promptError":     {"code": -32000, "message": "..."},
      "loadSessionError": {...},      # refuse Session resumption
      "loadSessionReplay": [ <update>, ... ],   # replayed history on resume
      "sessionPrefix": "roomA",       # how session ids are named (default
                                      # "fake-session"), so several processes'
                                      # Sessions are distinguishable
      "ignoreCancel": true,           # keep working after session/cancel —
                                      # an agent that does NOT honour the
                                      # protocol's cancellation
      "turns": [ <turn>, ... ],       # consumed one per session/prompt
      "defaultTurn": <turn>           # used once "turns" is exhausted
    }

A `<turn>` is:

    {
      "actions": [ <action>, ... ],
      "stopReason": "end_turn"        # or refusal / max_tokens / cancelled ...
    }

An `<action>` is one of:

    {"type": "message",   "text": "..."}          agent_message_chunk
    {"type": "thought",   "text": "..."}          agent_thought_chunk
    {"type": "tool_call", "toolCallId": "t1", "title": "...", "status": "pending"}
    {"type": "update",    "update": { ... }}      any raw session/update payload
    {"type": "permission",                        ask the Client for permission
        "toolCall": {...}, "options": [{"optionId": "allow", "name": "Allow",
                                        "kind": "allow_once"}, ...]}
    {"type": "request",   "method": "terminal/create", "params": {...}}
                                                  ask the Client for anything at
                                                  all, and record its answer
    {"type": "sleep",     "seconds": 2.0}         delay past a deadline —
                                                  interrupted by session/cancel
    {"type": "exit",      "code": 1}              die mid-Turn

**Cancellation.** `session/cancel` is honoured the way the protocol says an
Agent should honour it: the running Turn stops between actions (a `sleep` stops
at once), and `session/prompt` returns `{"stopReason": "cancelled"}` — whatever
the script said the stop reason would be. Everything already sent stands, which
is what makes partial output on a deadline observable. `"ignoreCancel": true`
models the other kind of agent: one that records the cancel and keeps working
regardless, so a Client's last resort can be tested.

An `<update>` is a raw ACP `session/update` payload — i.e. the object that goes
under `update`, i.e. `{"sessionUpdate": "agent_message_chunk", "content": {...}}`.

## The report

Written to `$FAKE_ACP_REPORT` after **every** recorded event, not just at exit,
so a fake that was told to die mid-Turn still reports what it saw. Shape:

    {
      "initialize":  <params of the initialize request>,
      "sessions":    [ {"method": "session/new"|"session/load",
                        "cwd": ..., "sessionId": ..., "params": <raw>} ],
      "modes":       [ {"sessionId": ..., "modeId": ...} ],
      "prompts":     [ {"sessionId": ..., "prompt": [<content blocks>]} ],
      "permissions": [ {"sessionId": ..., "options": [...],
                        "answer": <the Client's RequestPermissionResponse>} ],
      "cancelled":   [ <sessionId>, ... ],
      "requests":    [ {"method": ..., "answer": <result or error>,
                        "errored": true|false} ],
      "methods":     [ <every method name received, in order> ]
    }

`permissions[].answer` is how a test observes what the Worker's Permission
Policy decided, without reaching inside the Worker.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

PROTOCOL_VERSION = 1

DEFAULT_CAPABILITIES = {
    "loadSession": True,
    "promptCapabilities": {"image": True, "embeddedContext": True},
}


def _text_block(text: str) -> dict:
    return {"type": "text", "text": text}


class FakeAcpAgent:
    """Serves one stdio connection for the lifetime of the process."""

    def __init__(self, script: dict, report_path: str | None):
        self.script = script
        self.report_path = report_path
        self.report: dict = {
            "initialize": None,
            "sessions": [],
            "modes": [],
            "prompts": [],
            "permissions": [],
            "cancelled": [],
            "requests": [],
            "methods": [],
        }
        self._turns = list(script.get("turns") or [])
        self._session_seq = 0
        self._next_id = 1
        self._pending: dict = {}
        self._out_lock = asyncio.Lock()
        #: Sessions whose running Turn has been cancelled, one Event each, so a
        #: `sleep` ends the moment the cancel arrives rather than running out.
        self._cancels: dict = {}

    # -- report -----------------------------------------------------------

    def _flush_report(self) -> None:
        """Rewrite the report after every event.

        Deliberately not deferred to exit: `{"type": "exit"}` and an external
        kill are both behaviours later tickets script, and a report that only
        landed on a clean shutdown would be useless for exactly those tests.
        """
        if not self.report_path:
            return
        tmp = self.report_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.report, fh, indent=2)
        os.replace(tmp, self.report_path)

    # -- wire -------------------------------------------------------------

    async def _send(self, message: dict) -> None:
        async with self._out_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    async def _notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, method: str, params: dict):
        """Send a request to the ACP Client and wait for its response."""
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        await self._send(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
        return await fut

    async def _update(self, session_id: str, update: dict) -> None:
        await self._notify(
            "session/update", {"sessionId": session_id, "update": update}
        )

    # -- dispatch ---------------------------------------------------------

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin
        )
        self._flush_report()
        while True:
            line = await reader.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            # A response to something we asked (i.e. a permission answer).
            if "method" not in message and "id" in message:
                fut = self._pending.pop(message["id"], None)
                if fut is not None and not fut.done():
                    fut.set_result(message)
                continue
            # Handle each request concurrently: a long `prompt` must not stop
            # us seeing the `session/cancel` that is meant to end it.
            asyncio.ensure_future(self._handle(message))

    async def _handle(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        rid = message.get("id")
        self.report["methods"].append(method)
        try:
            result = await self._dispatch(method, params)
        except _JsonRpcError as err:
            self._flush_report()
            if rid is not None:
                await self._send(
                    {"jsonrpc": "2.0", "id": rid, "error": err.payload}
                )
            return
        self._flush_report()
        if rid is not None:
            await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _dispatch(self, method: str, params: dict):
        if method == "initialize":
            return self._initialize(params)
        if method == "session/new":
            return self._new_session(params)
        if method == "session/load":
            return await self._load_session(params)
        if method == "session/set_mode":
            self.report["modes"].append(
                {
                    "sessionId": params.get("sessionId"),
                    "modeId": params.get("modeId"),
                }
            )
            return {}
        if method == "session/prompt":
            return await self._prompt(params)
        if method == "session/cancel":
            self.report["cancelled"].append(params.get("sessionId"))
            self._cancel_event(params.get("sessionId")).set()
            return None
        if method == "authenticate":
            return {}
        raise _JsonRpcError(-32601, f"method not found: {method}")

    # -- methods ----------------------------------------------------------

    def _initialize(self, params: dict) -> dict:
        self.report["initialize"] = params
        capabilities = dict(DEFAULT_CAPABILITIES)
        capabilities.update(self.script.get("agentCapabilities") or {})
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": capabilities,
            "agentInfo": {"name": "fake-acp-agent", "version": "1.0.0"},
            "authMethods": self.script.get("authMethods") or [],
        }

    def _new_session(self, params: dict) -> dict:
        error = self.script.get("newSessionError")
        if error:
            raise _JsonRpcError(
                error.get("code", -32000), error.get("message", ""), error.get("data")
            )
        self._session_seq += 1
        # The prefix is scriptable so that a test running several Sessions —
        # each in its own process, each counting from one — can still tell them
        # apart in the Session map it is asserting on.
        prefix = self.script.get("sessionPrefix") or "fake-session"
        session_id = f"{prefix}-{self._session_seq}"
        self.report["sessions"].append(
            {
                "method": "session/new",
                "cwd": params.get("cwd"),
                "sessionId": session_id,
                "params": params,
            }
        )
        return {"sessionId": session_id}

    async def _load_session(self, params: dict) -> dict:
        error = self.script.get("loadSessionError")
        if error:
            # Refusing resumption is the point of this branch; record the
            # attempt anyway so a test can see it was tried.
            self.report["sessions"].append(
                {
                    "method": "session/load",
                    "cwd": params.get("cwd"),
                    "sessionId": params.get("sessionId"),
                    "params": params,
                    "refused": True,
                }
            )
            raise _JsonRpcError(
                error.get("code", -32000), error.get("message", ""), error.get("data")
            )
        session_id = params.get("sessionId")
        self.report["sessions"].append(
            {
                "method": "session/load",
                "cwd": params.get("cwd"),
                "sessionId": session_id,
                "params": params,
            }
        )
        # Resumption replays the whole prior conversation as notifications.
        for update in self.script.get("loadSessionReplay") or []:
            await self._update(session_id, update)
        return {}

    def _cancel_event(self, session_id) -> asyncio.Event:
        event = self._cancels.get(session_id)
        if event is None:
            event = self._cancels[session_id] = asyncio.Event()
        return event

    def _cancelled(self, session_id) -> bool:
        if self.script.get("ignoreCancel"):
            return False
        return self._cancel_event(session_id).is_set()

    async def _prompt(self, params: dict) -> dict:
        session_id = params.get("sessionId")
        # Refuses only when real work arrives, as the Claude bridge does with
        # a fresh config dir. -32000 is the auth-required code.
        error = self.script.get("promptError")
        if error:
            raise _JsonRpcError(
                error.get("code", -32000), error.get("message", ""), error.get("data")
            )
        self._cancel_event(session_id).clear()
        self.report["prompts"].append(
            {"sessionId": session_id, "prompt": params.get("prompt")}
        )
        self._flush_report()
        turn = (
            self._turns.pop(0)
            if self._turns
            else self.script.get("defaultTurn")
            or {"actions": [{"type": "message", "text": "ok"}]}
        )
        for action in turn.get("actions") or []:
            if self._cancelled(session_id):
                break
            await self._act(session_id, action)
        if self._cancelled(session_id):
            # What a cancelled Turn returns is fixed by the protocol, not by
            # the script: the prompt call ends normally, with `cancelled` and
            # whatever was produced before it. Everything already sent stands.
            return {"stopReason": "cancelled"}
        return {"stopReason": turn.get("stopReason", "end_turn")}

    async def _act(self, session_id: str, action: dict) -> None:
        kind = action.get("type")
        if kind == "message":
            await self._update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": _text_block(action.get("text", "")),
                },
            )
        elif kind == "thought":
            await self._update(
                session_id,
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": _text_block(action.get("text", "")),
                },
            )
        elif kind == "tool_call":
            update = {
                "sessionUpdate": "tool_call",
                "toolCallId": action.get("toolCallId", "tool-1"),
                "title": action.get("title", "a tool"),
                "status": action.get("status", "pending"),
            }
            update.update(
                {
                    k: v
                    for k, v in action.items()
                    if k not in ("type", "toolCallId", "title", "status")
                }
            )
            await self._update(session_id, update)
        elif kind == "update":
            await self._update(session_id, action.get("update") or {})
        elif kind == "permission":
            await self._ask_permission(session_id, action)
        elif kind == "request":
            # Any request at all, back at the Client, with whatever the Client
            # answered recorded verbatim. This is how a test asks the question
            # "what happens if the Agent asks for something we do not offer?" —
            # terminal provisioning, for instance, which agent-connect
            # deliberately does not implement.
            response = await self._request(
                action.get("method", ""),
                {"sessionId": session_id, **(action.get("params") or {})},
            )
            self.report["requests"].append(
                {
                    "method": action.get("method", ""),
                    "answer": response.get("result", response.get("error")),
                    "errored": "error" in response,
                }
            )
            self._flush_report()
        elif kind == "sleep":
            # Interruptible: a Turn cancelled through the protocol stops when
            # the cancel arrives, not when the script's clock runs out. An
            # agent scripted to ignore cancellation sleeps it out instead.
            seconds = float(action.get("seconds", 1))
            if self.script.get("ignoreCancel"):
                await asyncio.sleep(seconds)
            else:
                try:
                    await asyncio.wait_for(
                        self._cancel_event(session_id).wait(), timeout=seconds
                    )
                except asyncio.TimeoutError:
                    pass
        elif kind == "exit":
            self._flush_report()
            os._exit(int(action.get("code", 0)))
        else:
            raise _JsonRpcError(-32602, f"unknown scripted action: {kind!r}")

    async def _ask_permission(self, session_id: str, action: dict) -> None:
        options = action.get("options") or [
            {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
        ]
        tool_call = action.get("toolCall") or {
            "toolCallId": "tool-1",
            "title": "write a file",
        }
        record = {
            "sessionId": session_id,
            "toolCall": tool_call,
            "options": options,
            "answer": None,
        }
        self.report["permissions"].append(record)
        self._flush_report()
        response = await self._request(
            "session/request_permission",
            {"sessionId": session_id, "toolCall": tool_call, "options": options},
        )
        # Record the whole envelope: a policy that errored is as interesting as
        # one that answered, and a test should be able to tell them apart.
        record["answer"] = response.get("result", response.get("error"))
        self._flush_report()


class _JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data=None):
        super().__init__(message)
        self.payload = {"code": code, "message": message}
        if data is not None:
            self.payload["data"] = data


def load_script(argv: list) -> dict:
    path = argv[1] if len(argv) > 1 else os.environ.get("FAKE_ACP_SCRIPT")
    if not path:
        return {}
    with open(path) as fh:
        return json.load(fh)


def main(argv: list | None = None) -> None:
    argv = sys.argv if argv is None else argv
    report = argv[2] if len(argv) > 2 else os.environ.get("FAKE_ACP_REPORT")
    agent = FakeAcpAgent(load_script(argv), report)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    finally:
        agent._flush_report()


if __name__ == "__main__":
    main()
