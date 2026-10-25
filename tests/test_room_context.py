"""Real localhost relay -> Task -> Worker -> CLI/stdio ACP context regression.

No hosted room, real account, or paid model call is used. The ACP Agent is a
separate JSON-RPC subprocess; its report is the actual model-adapter input.
"""
from __future__ import annotations
import _bootstrap
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.append(str(_bootstrap.RELAY_CLIENT / "tests"))
from fake_broker import FakeBroker
from _room_context import wire
from ag2_relay_client import RelayClient, TokenSource
from ag2_relay_client.envelope import MAX_TEXT, parse_task
from ag2_relay_client.room_context import parse_room_context
from agent_connect.adapters import ShimAdapter
from agent_connect.adapters.acp import AcpAdapter
from agent_connect.sessions import SessionStore
from agent_connect.worker import handle_one, turn_context

fails = 0

def check(cond, label):
    global fails
    print(("  ok   " if cond else "  FAIL ") + label)
    fails += not cond


def carries_tool_guidance(text):
    # Consumer-input contract, not a claim about a model's subsequent tool calls.
    return all(clause in text for clause in (
        "if it is sufficient, answer without additional context reads",
        "specific missing or fresh permitted fact",
        "as the same agent in the originating room",
        "subject to current policy and normal permission checks",
        "Do not automatically fetch bulk history or documents",
        "Never retrieve denied sections or switch identity or credentials",
        "unavailable or denied, state the limitation",
    ))


def parsed(raw):
    return parse_task(raw)

base = wire()
packet = parsed(base).room_context
check(packet is not None, "valid packet crosses the relay parser")
if packet is None:
    raise SystemExit("room-context regression: parser discarded valid room data")
check(parsed(wire(room="!" + "x" * 800)).room_context is not None,
      "opaque long Matrix room IDs are preserved and bind exactly")
check(turn_context(parsed(base), "/repo").room_context == packet,
      "validated context survives Worker TurnContext")
check("CURRENT_ASK" in turn_context(parsed(base), "/repo").prompt
      and "README_SENTINEL" not in turn_context(parsed(base), "/repo").prompt,
      "room data stays separate from current request")
check(parsed({"id": "legacy", "task": "legacy ask"}).room_context is None,
      "old broker tasks remain supported")
from agent_connect.relay import from_env
with tempfile.TemporaryDirectory() as workspace:
    capable = from_env(Path(workspace), {"AGENT_CONNECT_TOKEN": "http://127.0.0.1:9|fixture"})
    check("room-context-v1" in capable.capabilities,
          "Worker advertises v1 consumption separately from generic Relay Client")

# Adversarial mutation reaches the real parser, not a test-only validator.
for field, value in (("room_id", "!other"), ("task_id", "other-task"),
                     ("access_tier", "guest"), ("anchor_event_id", "$other"),
                     ("policy_revision", "other-policy"), ("agent_id", "@other:x"),
                     ("membership_epoch", "other-membership")):
    raw = copy.deepcopy(base); raw["room_context"][field] = value
    got = parsed(raw)
    check(got.room_context is None and got.body == base["task"],
          "mismatched " + field + " drops context and preserves ask")
for name, mutate in (
    ("unknown version", lambda r: (r["room_context"].update(schema_version=2), r["room_context_binding"].update(schema_version=2))),
    ("unknown fields", lambda r: r["room_context"].update(owner=True)),
    ("expired", lambda r: (r["room_context"].update(observed_at=1, expires_at=2), r["room_context_binding"].update(observed_at=1, expires_at=2))),
    ("mentions policy bypass", lambda r: (r["room_context"].update(visibility="mentions"), r["room_context_binding"].update(visibility="mentions"))),
    ("denied content", lambda r: r["room_context"].update(status="denied")),
    ("oversized bytes", lambda r: r["room_context"].update(room={"name": "🙂" * 20000})),
    ("malformed section", lambda r: r["room_context"].update(messages="not a list")),
    ("float timestamp", lambda r: (r["room_context"].update(observed_at=1.5), r["room_context_binding"].update(observed_at=1.5))),
    ("overlong README", lambda r: r["room_context"]["readme"].update(text="x" * 8193)),
    ("too many messages", lambda r: r["room_context"].update(messages=r["room_context"]["messages"] * 21)),
):
    raw = copy.deepcopy(base); mutate(raw); got = parsed(raw)
    check(got.room_context is None and got.body == base["task"], name + " preserves ask")
raw = copy.deepcopy(base); raw["task"] = "Q" * (MAX_TEXT - 8) + "ASK_TAIL"
check(parsed(raw).body.endswith("ASK_TAIL"), "context never consumes current ask's text budget")
raw = copy.deepcopy(base); raw["room_context"]["messages"][0]["selection"] = "mention"
raw["room_context"].update(visibility="mentions"); raw["room_context_binding"].update(visibility="mentions")
check(parsed(raw).room_context is not None, "eligible mention-only context is supported")
for status in ("denied", "unavailable"):
    raw = copy.deepcopy(base)
    for key in ("room", "readme", "members", "messages"): raw["room_context"].pop(key)
    raw["room_context"]["status"] = status
    check(parsed(raw).room_context is not None, status + " is explicit without invented contents")
raw = copy.deepcopy(base); raw["access_tier"] = "guest"
check(turn_context(parsed(raw), "/repo").access_tier == "guest"
      and parsed(raw).room_context is None, "packet cannot promote the outer guest tier")
raw = wire(tier="team")
check(parsed(raw).room_context is not None and turn_context(parsed(raw), "/repo").room_context is None,
      "consumer's team-to-guest demotion invalidates broader packet")
old = os.environ.get("AGENT_CONNECT_AGENT_ID")
os.environ["AGENT_CONNECT_AGENT_ID"] = "@different:example.test"
check(turn_context(parsed(base), "/repo").room_context is None,
      "configured actor is checked independently of sender")
if old is None: os.environ.pop("AGENT_CONNECT_AGENT_ID", None)
else: os.environ["AGENT_CONNECT_AGENT_ID"] = old

class CLI:
    def __init__(self): self.prompts = []
    def run(self, prompt, sandbox, cwd):
        self.prompts.append(prompt); return "synthetic response"

async def main():
    with tempfile.TemporaryDirectory() as tmp, FakeBroker() as broker:
        tmp = Path(tmp)
        broker.on("GET", "/v1/tasks", json={"tasks": [base]})
        broker.on("POST", "/v1/tasks/*/ack", json={"ok": True})
        broker.on("POST", "/v1/heartbeat", json={"ok": True})
        client = from_env(tmp, {"AGENT_CONNECT_TOKEN": broker.url + "|synthetic-secret"},
                          singleton=False, poll_wait=7)
        client.prepare(); client.poll_once()
        task = client.tasks.get_nowait()
        from urllib.parse import parse_qs
        outgoing = broker.took("GET", "/v1/tasks")[0]
        check(parse_qs(outgoing.query) == {"wait": ["7"], "capabilities": ["room-context-v1"]},
              "Worker opts in on its actual outgoing HTTP task poll")
        check(outgoing.header("Authorization") == "Bearer synthetic-secret",
              "Worker capability proof preserves authenticated poll")
        check("room-context-v1" in broker.took("POST", "/v1/heartbeat")[0].json["capabilities"],
              "Worker also retains its heartbeat capability declaration")
        check(task.room_context == packet, "localhost HTTP response preserves typed context")
        journal = client.layout.journal_path.read_text()
        check("README_SENTINEL" not in journal and "CURRENT_ASK" not in journal,
              "durable journal contains no inbound room transcript or ask")
        cli = CLI()
        await handle_one(task, ShimAdapter("fixture", cli), str(tmp))
        await handle_one(task, ShimAdapter("fixture", cli), str(tmp))
        check(len(cli.prompts) == 2 and all("README_SENTINEL" in p and "HISTORY_SENTINEL" in p
              and p.endswith(base["task"]) for p in cli.prompts),
              "every one-shot turn receives context and unchanged current ask")
        check(all(carries_tool_guidance(p) for p in cli.prompts),
              "one-shot input carries briefing-first and bounded targeted-tool guidance")
        broker.on("GET", "/v1/tasks", json={"tasks": [base]})
        client.poll_once()
        check(client.tasks.empty(), "duplicate in-flight relay delivery does not trigger another turn")
        restarted = RelayClient(TokenSource(token=broker.url + "|synthetic-secret"),
                                tmp / "relay", singleton=False)
        restarted.prepare(); restarted.poll_once()
        replayed = restarted.tasks.get_nowait()
        check(replayed.room_context == packet, "restart revalidates broker-redelivered context")
        script = tmp / "script.json"
        script.write_text(json.dumps({"defaultTurn": {"actions": [{"type": "message", "text": "fixture reply"}], "stopReason": "end_turn"}}))
        store = tmp / "sessions.json"
        reports = []
        def adapter():
            return AcpAdapter(command=[sys.executable, str(_bootstrap.ROOT / "tests/fake_acp_agent.py"), str(script)], store=SessionStore(store))
        inputs = [base, wire("followup"), wire("other-room", "!second"),
                  wire("policy-change"), wire("denied-status"), wire("membership-change")]
        for i, raw in enumerate(inputs):
            if i == 3:
                raw["room_context"]["policy_revision"] = "policy-two"
                raw["room_context_binding"]["policy_revision"] = "policy-two"
            if i == 4:
                raw["room_context"]["status"] = "denied"
                for name in ("room", "readme", "members", "messages"):
                    raw["room_context"].pop(name)
            if i == 5:
                raw["room_context"]["membership_epoch"] = "membership-two"
                raw["room_context_binding"]["membership_epoch"] = "membership-two"
            report = tmp / (str(i) + ".json")
            os.environ["FAKE_ACP_REPORT"] = str(report)
            try: await handle_one(parsed(raw), adapter(), str(tmp))
            finally: os.environ.pop("FAKE_ACP_REPORT", None)
            reports.append(json.loads(report.read_text()))
        for report, raw in zip(reports, inputs):
            blocks = report["prompts"][0]["prompt"]
            check(blocks[0]["text"].endswith(raw["task"])
                  and "README_SENTINEL" not in blocks[0]["text"]
                  and blocks[0]["_meta"]["ag2/task"]["task_id"] == raw["id"],
                  "stdio ACP retains current ask as separately bound first block")
            check(("README_SENTINEL" in blocks[1]["text"]) == (raw["room_context"]["status"] == "partial")
                  and "ag2/room_context" in blocks[1]["_meta"],
                  "stdio ACP carries separately marked context on every turn")
            check(carries_tool_guidance(blocks[1]["text"]),
                  "stdio ACP input keeps targeted reads available under the same denial boundaries")
        check("session/load" in reports[1]["methods"], "same policy room resumes after Adapter restart")
        check("session/load" not in reports[2]["methods"], "different room cannot reuse context session")
        check("session/load" not in reports[3]["methods"], "changed policy rotates context-aware session")
        check("session/load" not in reports[4]["methods"], "denied status cannot resume prior partial context")
        check("session/load" not in reports[5]["methods"], "reinvite membership rotates context-aware session")
        disk = json.loads(store.read_text())
        check(not disk["sessions"] and len(disk["room_context_sessions"]) >= 4,
              "old Workers cannot read scoped context Sessions as legacy Sessions")
        for i, raw in enumerate((wire("legacy-session"), wire("invalid-session"))):
            if i == 0:
                raw.pop("room_context"); raw.pop("room_context_binding")
            else:
                raw["room_context"]["room_id"] = "!mismatch"
            report = tmp / ("invalid-" + str(i) + ".json")
            os.environ["FAKE_ACP_REPORT"] = str(report)
            try: await handle_one(parsed(raw), adapter(), str(tmp))
            finally: os.environ.pop("FAKE_ACP_REPORT", None)
            if i == 1:
                invalid = json.loads(report.read_text())
                check("session/load" not in invalid["methods"],
                      "managed-invalid context never resumes a legacy room Session")
                check(len(invalid["prompts"][0]["prompt"]) == 1
                      and invalid["prompts"][0]["prompt"][0]["text"].endswith(raw["task"]),
                      "invalid context preserves actual ask without supplying historical contents")
                check(carries_tool_guidance(invalid["prompts"][0]["prompt"][0]["text"]),
                      "managed-invalid input preserves bounded targeted-tool guidance")

        # Advance validation time after the Session has opened, without a
        # wall-clock sleep or a paid model call.
        from unittest.mock import patch
        from ag2_relay_client.room_context import RoomContext
        expiry_raw = wire("startup-expiry")
        original_current = RoomContext.current
        calls = 0
        def expire_during_startup(self, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 4:
                kwargs["now"] = self.binding["expires_at"] + 1
            return original_current(self, **kwargs)
        expiry_script = json.loads(script.read_text())
        expiry_script["sessionPrefix"] = "expiry-session"
        script.write_text(json.dumps(expiry_script))
        report = tmp / "startup-expiry.json"
        os.environ["FAKE_ACP_REPORT"] = str(report)
        try:
            with patch.object(RoomContext, "current", expire_during_startup):
                await handle_one(parsed(expiry_raw), AcpAdapter(
                    command=[sys.executable, str(_bootstrap.ROOT / "tests/fake_acp_agent.py"), str(script)],
                    mode="fixture-mode", store=SessionStore(store)), str(tmp))
        finally:
            os.environ.pop("FAKE_ACP_REPORT", None)
        expired = json.loads(report.read_text())
        prompt = expired["prompts"][0]
        check(len(expired["sessions"]) == 2 and len(prompt["prompt"]) == 1
              and "README_SENTINEL" not in prompt["prompt"][0]["text"],
              "expiry during startup replaces Session and omits stale room data")
        check(expired["modes"][-1]["sessionId"] == prompt["sessionId"],
              "expiry replacement preserves configured ACP mode")

        # Attachment preparation can cross expiration after Session load. Test
        # both before context-block construction and after it has already been
        # materialized, as an actual stdio prompt rather than a helper result.
        from agent_connect.adapters import acp as acp_module
        real_blocks = acp_module.prompt_blocks
        from ag2_relay_client.media import Attachment as RelayAttachment
        import base64
        attachment_data = b"PREPARED_ATTACHMENT_SENTINEL"
        attachment_path = tmp / "prepared-context-attachment.txt"
        attachment_path.write_bytes(attachment_data)
        for phase in ("before", "after"):
            seed = wire("preparation-seed-" + phase)
            script_data = json.loads(script.read_text())
            script_data["sessionPrefix"] = "seed-" + phase
            script.write_text(json.dumps(script_data))
            seed_report = tmp / ("preparation-seed-" + phase + ".json")
            os.environ["FAKE_ACP_REPORT"] = str(seed_report)
            try: await handle_one(parsed(seed), adapter(), str(tmp))
            finally: os.environ.pop("FAKE_ACP_REPORT", None)
            followup = wire("preparation-expiry-" + phase)
            clock = followup["room_context"]["observed_at"]
            expires = followup["room_context"]["expires_at"]
            def controlled(self, **kwargs):
                return original_current(self, **dict(kwargs, now=clock))
            def prepare(*args, **kwargs):
                nonlocal clock
                if phase == "before": clock = expires + 1
                prepared = real_blocks(*args, **kwargs)
                if phase == "after":
                    clock = expires + 1
                    attachment_path.unlink()  # prepared bytes must not be reopened
                return prepared
            script_data["sessionPrefix"] = "replacement-" + phase
            script.write_text(json.dumps(script_data))
            followup_task = parsed(followup)
            if phase == "after":
                followup_task.attachments = (RelayAttachment(
                    path=str(attachment_path), mime="text/plain", name=attachment_path.name,
                    size=len(attachment_data), ok=True),)
            report = tmp / ("preparation-expiry-" + phase + ".json")
            os.environ["FAKE_ACP_REPORT"] = str(report)
            try:
                with patch.object(RoomContext, "current", controlled), patch.object(acp_module, "prompt_blocks", prepare):
                    await handle_one(followup_task, AcpAdapter(
                        command=[sys.executable, str(_bootstrap.ROOT / "tests/fake_acp_agent.py"), str(script)],
                        mode="fixture-mode", store=SessionStore(store)), str(tmp))
            finally:
                os.environ.pop("FAKE_ACP_REPORT", None)
            prepared_report = json.loads(report.read_text())
            prepared_prompt = prepared_report["prompts"][0]
            loaded = [entry["sessionId"] for entry in prepared_report["sessions"] if entry["method"] == "session/load"]
            check(bool(loaded) and prepared_prompt["sessionId"] not in loaded,
                  "expiry " + phase + " block construction cannot prompt the loaded Session")
            check(len(prepared_prompt["prompt"]) == (2 if phase == "after" else 1)
                  and "README_SENTINEL" not in prepared_prompt["prompt"][0]["text"]
                  and prepared_prompt["prompt"][0]["text"].endswith(followup["task"]),
                  "expiry " + phase + " block construction removes cached context and preserves ask")
            check(prepared_report["modes"][-1]["sessionId"] == prepared_prompt["sessionId"],
                  "expiry " + phase + " block construction preserves replacement mode")
            check(not json.loads(store.read_text())["room_context_sessions"]
                  or all(entry["room_context_scope"] != parsed(seed).room_context.scope
                         for entry in json.loads(store.read_text())["room_context_sessions"]),
                  "expiry " + phase + " block construction forgets persistent context Session")
            if phase == "after":
                check(base64.b64decode(prepared_prompt["prompt"][1]["resource"]["blob"]) == attachment_data,
                      "expiry after preparation preserves already-read attachment bytes")

asyncio.run(main())
print("\n" + ("PASS — room context" if not fails else f"FAIL — {fails} assertions"))
raise SystemExit(bool(fails))
