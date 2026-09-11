"""The HITL seam speaks Sutando's file contract and nothing else: it posts a
RuntimeEvent, returns the human's option id once the requirement record
carries it, leaves a tombstone either way, and turns a timeout, an expired or
cancelled card, or a foreign click into None (the caller's reject), never an
allow. The guard names the Session, so one room's click cannot answer
another's. Stdlib only."""

import asyncio
import json
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


def req(tc_id="tc-42", options=None):
    return Req(
        {"toolCallId": tc_id, "title": "Run `rm -rf build`", "kind": "execute", "rawInput": {"command": "rm -rf build"}},
        options if options is not None else
        [{"optionId": "allow-1", "name": "Allow", "kind": "allow_once"},
         {"optionId": "reject-1", "name": "Reject", "kind": "reject_once"}],
    )


def record(ws: Path, guard: str, chosen=None, status="pending", hid="hitl_abc"):
    d = ws / "state" / "hitl" / "requirements"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{hid}.json").write_text(json.dumps({
        "requirement": {"id": hid, "guard": guard, "chosen_action": chosen, "status": status},
        "projection": {"revision": 0, "event_id": None}}))


def event_path(ws: Path, session: str, r) -> Path:
    guard = hitl_seam.guard_for(r, session)
    return ws / "state" / "hitl" / "events" / f"{hitl_seam._safe(session)}-{hitl_seam._safe(guard)}.json"


def escalate(r, ws, session="w", timeout=5, poll=0.01):
    return asyncio.run(hitl_seam.escalate(r, session=session, workspace=ws, timeout=timeout, poll=poll))


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

    def test_timeout_must_be_a_finite_bound(self):
        # `float()` reads all of these as infinity, and an infinite deadline is
        # a Turn blocked forever on a card nobody has to click.
        for unbounded in ("inf", "Infinity", "1e400", "-inf", "nan", "-5"):
            self.assertEqual(hitl_seam.timeout_s({"AGENT_CONNECT_HITL_TIMEOUT": unbounded}),
                             hitl_seam.DEFAULT_TIMEOUT_S, unbounded)
        self.assertEqual(hitl_seam.timeout_s({"AGENT_CONNECT_HITL_TIMEOUT": "30"}), 30.0)
        self.assertEqual(hitl_seam.timeout_s({"AGENT_CONNECT_HITL_TIMEOUT": "0"}), 0.0)  # "do not wait" is a bound
        self.assertEqual(hitl_seam.timeout_s({}), hitl_seam.DEFAULT_TIMEOUT_S)

    def test_event_carries_the_contract(self):
        ev = hitl_seam.event_for(req(), "worker-3")
        self.assertEqual((ev["schema"], ev["runtime"], ev["kind"], ev["session"]), (hitl_seam.SCHEMA, "acp", "permission", "worker-3"))
        self.assertEqual(ev["guard"], hitl_seam.guard_for(req(), "worker-3"))
        self.assertIn("worker-3", ev["guard"])
        self.assertIn("tc-42", ev["guard"])
        self.assertEqual(ev["subject"]["tool"], "execute")
        self.assertIn("rm -rf build", ev["subject"]["input"])
        self.assertEqual(ev["options"], [{"id": "allow-1", "label": "Allow", "kind": "allow_once"},
                                         {"id": "reject-1", "label": "Reject", "kind": "reject_once"}])

    def test_guard_names_the_session_and_never_the_clock(self):
        a = hitl_seam.guard_for(req("t1"), "room-a")
        b = hitl_seam.guard_for(req("t1"), "room-b")
        self.assertNotEqual(a, b, "the same tool-call id in two Sessions is two guards")
        self.assertEqual(a, hitl_seam.guard_for(req("t1"), "room-a"), "and a guard is stable for its request")
        anonymous = Req({"title": "no id"}, [])
        self.assertNotEqual(hitl_seam.guard_for(anonymous, "w"), hitl_seam.guard_for(anonymous, "w"),
                            "a request without an id does not share a guard with its neighbour")

    def test_human_allow_returns_the_option_and_tombstones(self):
        r = req()
        ev_path = event_path(self.ws, "worker-3", r)

        async def human():
            # Wait for the event to be posted, then answer it as the Manager would.
            while not ev_path.exists():
                await asyncio.sleep(0.01)
            posted = json.loads(ev_path.read_text())
            self.assertFalse(posted.get("cleared"))
            record(self.ws, posted["guard"], chosen="allow-1", status="in_progress")

        async def run():
            h = asyncio.create_task(human())
            out = await hitl_seam.escalate(r, session="worker-3", workspace=self.ws, timeout=5, poll=0.01)
            await h
            return out

        self.assertEqual(asyncio.run(run()), "allow-1")
        self.assertTrue(json.loads(ev_path.read_text()).get("cleared"))  # tombstone left for the Manager

    def test_timeout_is_none_never_allow_and_still_tombstones(self):
        r = req("tc-9")
        self.assertIsNone(escalate(r, self.ws, timeout=0.05))
        ev = json.loads(event_path(self.ws, "w", r).read_text())
        self.assertTrue(ev.get("cleared"))

    def test_expired_card_is_none(self):
        r = req("tc-7")
        record(self.ws, hitl_seam.guard_for(r, "w"), chosen=None, status="expired")
        self.assertIsNone(escalate(r, self.ws))

    def test_a_withdrawn_card_keeps_its_click_and_the_click_is_still_no_answer(self):
        # Sutando's cancel()/expire() transition the status and keep
        # `chosen_action`, so a card the owner answered can be expired under
        # that answer; the Manager shows it as no longer applicable.
        for status in ("expired", "cancelled"):
            r = req(f"tc-{status}")
            record(self.ws, hitl_seam.guard_for(r, "w"), chosen="allow-1", status=status, hid=f"hitl_{status}")
            self.assertIsNone(escalate(r, self.ws), status)

    def test_a_resolved_card_with_a_click_is_the_happy_path(self):
        # `resolved` is terminal too, and the one terminal state whose action
        # counts — the fix for the withdrawn case must not swallow it.
        r = req("tc-done")
        record(self.ws, hitl_seam.guard_for(r, "w"), chosen="allow-1", status="resolved")
        self.assertEqual(escalate(r, self.ws), "allow-1")

    def test_a_choice_outside_the_request_options_is_not_honoured(self):
        r = req("tc-8")
        record(self.ws, hitl_seam.guard_for(r, "w"), chosen="open_terminal", status="in_progress")  # the jump action is the client's
        self.assertIsNone(escalate(r, self.ws))

    def test_other_requirements_do_not_answer_this_one(self):
        r = req("tc-1")
        record(self.ws, hitl_seam.guard_for(req("tc-OTHER"), "w"), chosen="allow-1", status="in_progress", hid="hitl_other")
        self.assertIsNone(escalate(r, self.ws, timeout=0.05))

    def test_a_decision_does_not_cross_sessions(self):
        # Two Sessions, the same tool-call id, opposite answers. Each room's
        # record must answer only its own request.
        record(self.ws, hitl_seam.guard_for(req("t1"), "room-a"), chosen="allow-1", status="in_progress", hid="hitl_a")
        record(self.ws, hitl_seam.guard_for(req("t1"), "room-b"), chosen="reject-1", status="in_progress", hid="hitl_b")
        # Control first: the fixtures are found, so a None below is a refusal
        # and not a guard that silently stopped matching.
        self.assertEqual(escalate(req("t1"), self.ws, session="room-a"), "allow-1")
        self.assertEqual(escalate(req("t1"), self.ws, session="room-b"), "reject-1")
        # A third room asking about t1 inherits neither.
        self.assertIsNone(escalate(req("t1"), self.ws, session="room-c", timeout=0.05))

    def test_option_ids_are_normalised_the_same_way_in_and_out(self):
        # The card shows str(optionId); a click on what the card showed must be
        # honoured even when the wire carried the id as a number.
        r = req("tc-num", options=[{"optionId": 123, "name": "Allow", "kind": "allow_once"},
                                   {"optionId": "reject-1", "name": "Reject", "kind": "reject_once"}])
        self.assertEqual([o["id"] for o in hitl_seam.event_for(r, "w")["options"]], ["123", "reject-1"])
        record(self.ws, hitl_seam.guard_for(r, "w"), chosen="123", status="in_progress")
        self.assertEqual(escalate(r, self.ws), "123")


if __name__ == "__main__":
    unittest.main()
