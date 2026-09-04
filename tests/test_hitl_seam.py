"""The HITL seam speaks Sutando's file contract and nothing else: it posts a
RuntimeEvent, returns the human's option id once the requirement record
carries it, leaves a tombstone either way, and turns a timeout or an expired
card into None (the caller's reject), never an allow. Stdlib only."""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_connect import hitl_seam  # noqa: E402


class Req:
    def __init__(self, tool_call, options):
        self.session_id = "s1"
        self.tool_call = tool_call
        self.options = options


def req(tc_id="tc-42"):
    return Req(
        {"toolCallId": tc_id, "title": "Run `rm -rf build`", "kind": "execute", "rawInput": {"command": "rm -rf build"}},
        [{"optionId": "allow-1", "name": "Allow", "kind": "allow_once"},
         {"optionId": "reject-1", "name": "Reject", "kind": "reject_once"}],
    )


def record(ws: Path, guard: str, chosen=None, status="pending", hid="hitl_abc"):
    d = ws / "state" / "hitl" / "requirements"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{hid}.json").write_text(json.dumps({
        "requirement": {"id": hid, "guard": guard, "chosen_action": chosen, "status": status},
        "projection": {"revision": 0, "event_id": None}}))


class SeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_not_configured_means_off(self):
        self.assertIsNone(hitl_seam.configured({}))
        self.assertEqual(hitl_seam.configured({"AGENT_CONNECT_HITL_WORKSPACE": "/w"}), Path("/w"))
        self.assertEqual(hitl_seam.timeout_s({"AGENT_CONNECT_HITL_TIMEOUT": "x"}), hitl_seam.DEFAULT_TIMEOUT_S)

    def test_event_carries_the_contract(self):
        ev = hitl_seam.event_for(req(), "worker-3")
        self.assertEqual((ev["schema"], ev["runtime"], ev["kind"], ev["session"]), (hitl_seam.SCHEMA, "acp", "permission", "worker-3"))
        self.assertEqual(ev["guard"], "acp:tc-42")
        self.assertEqual(ev["subject"]["tool"], "execute")
        self.assertIn("rm -rf build", ev["subject"]["input"])
        self.assertEqual(ev["options"], [{"id": "allow-1", "label": "Allow", "kind": "allow_once"},
                                         {"id": "reject-1", "label": "Reject", "kind": "reject_once"}])

    def test_human_allow_returns_the_option_and_tombstones(self):
        r = req()
        ev_path = self.ws / "state" / "hitl" / "events" / "worker-3-acp_tc-42.json"

        async def human():
            # Wait for the event to be posted, then answer it as the Manager would.
            while not ev_path.exists():
                await asyncio.sleep(0.01)
            posted = json.loads(ev_path.read_text())
            self.assertFalse(posted.get("cleared"))
            record(self.ws, "acp:tc-42", chosen="allow-1", status="in_progress")

        async def run():
            h = asyncio.create_task(human())
            out = await hitl_seam.escalate(r, session="worker-3", workspace=self.ws, timeout=5, poll=0.01)
            await h
            return out

        self.assertEqual(asyncio.run(run()), "allow-1")
        self.assertTrue(json.loads(ev_path.read_text()).get("cleared"))  # tombstone left for the Manager

    def test_timeout_is_none_never_allow_and_still_tombstones(self):
        r = req("tc-9")
        out = asyncio.run(hitl_seam.escalate(r, session="w", workspace=self.ws, timeout=0.05, poll=0.01))
        self.assertIsNone(out)
        ev = json.loads((self.ws / "state" / "hitl" / "events" / "w-acp_tc-9.json").read_text())
        self.assertTrue(ev.get("cleared"))

    def test_expired_card_is_none(self):
        r = req("tc-7")
        record(self.ws, "acp:tc-7", chosen=None, status="expired")
        out = asyncio.run(hitl_seam.escalate(r, session="w", workspace=self.ws, timeout=5, poll=0.01))
        self.assertIsNone(out)

    def test_a_choice_outside_the_request_options_is_not_honoured(self):
        r = req("tc-8")
        record(self.ws, "acp:tc-8", chosen="open_terminal", status="in_progress")  # the jump action is the client's
        out = asyncio.run(hitl_seam.escalate(r, session="w", workspace=self.ws, timeout=5, poll=0.01))
        self.assertIsNone(out)

    def test_other_requirements_do_not_answer_this_one(self):
        r = req("tc-1")
        record(self.ws, "acp:tc-OTHER", chosen="allow-1", status="in_progress", hid="hitl_other")
        out = asyncio.run(hitl_seam.escalate(r, session="w", workspace=self.ws, timeout=0.05, poll=0.01))
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main()
