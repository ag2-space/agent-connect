"""What a guest sees when they address an ACP agent: a sentence, in the room.

The ACP Adapter serves the owner and nobody else (`docs/adr/0001`), and the tier
it keys on is the one the broker attested (`docs/adr/0003`). Both halves of that
are only worth anything if the person who is refused finds out — a Task that
vanishes is indistinguishable from a Worker that has fallen over, and the two
are reported as the same bug.

So this asserts on the two things the outside world can see, and on nothing
else: the Room Ops a **fake relay** actually received, and the result the
delivery path was handed. The Local Agent's own view is the third: a real fake
ACP Agent child process writes a report, and for a guest's Task that report must
not exist at all, because the process was never started.

The Ladder is the whole point of the first half. A refusal that arrived as a
second message would leave the placeholder hanging above it saying "on it" for
ever, so what is asserted is one message, edited into the refusal.

Requires the `agent-client-protocol` package (see `docs/adr/0001`).

Run: .venv/bin/python tests/test_acp_guest_refusal.py
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

try:
    from agent_connect.adapters.acp import OWNER as ADAPTER_OWNER
    from agent_connect.adapters.acp import REFUSAL, AcpAdapter
    from agent_connect.reporter import PLACEHOLDER, REPLIED, STOP_LINES, LadderSettings
    from agent_connect.events import REFUSED
    from agent_connect.sessions import SessionStore
    from _queue import room_ops_at
    from _queue import task as queued_task
    from agent_connect.worker import GUEST, OWNER, handle_one
except ImportError as exc:  # pragma: no cover — an environment problem, not a bug
    raise SystemExit(
        f"test_acp_guest_refusal.py: {exc}\n"
        "This test has a dependency (see docs/adr/0001). Run it from an\n"
        "environment that has it:\n"
        "    python3 -m venv .venv && .venv/bin/pip install -e .\n"
        "    .venv/bin/python tests/test_acp_guest_refusal.py"
    )

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")
ROOM = "!room:ag2.space"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class FakeRelay:
    """The relay's `POST /v1/room`, recording every Room Op it is asked for.

    The same shape as `test_ladder.py`'s: a real HTTP server, because what a
    room sees is what the relay was asked to do and nothing about which object
    asked for it.
    """

    def __init__(self):
        self.ops = []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the test output readable
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                relay.ops.append(payload)
                raw = json.dumps({"event_id": f"$ev{len(relay.ops)}"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def ops_of(self, kind):
        return [o for o in self.ops if o.get("op") == kind]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class Bench:
    """One Worker workspace, one scripted fake Agent, one room to speak into.

    `relay=False` is the Worker that holds no relay token: the Ladder is not
    climbed at all and the answer — refusal included — travels as the result.
    """

    def __init__(self, script: dict, relay: bool = True):
        self._dir = tempfile.TemporaryDirectory()
        base = Path(self._dir.name)
        self.results = base / "results"
        self.repo = base / "repo"
        for d in (self.results, self.repo):
            d.mkdir()
        self.script_path = base / "script.json"
        self.script_path.write_text(json.dumps(script))
        self.report_path = base / "report.json"
        self.relay = FakeRelay() if relay else None
        self.ops = room_ops_at(self.relay.url) if relay else None
        self.adapter = AcpAdapter(
            command=[sys.executable, FAKE, str(self.script_path)],
            store=SessionStore(base / "sessions.json"),
        )

    def handle(self, task_id: str, body: str, tier=None) -> str:
        """Run one Task through the Worker and return the answer it gave.

        `tier=None` is a Task with no `access_tier` on it at all — the Task the
        broker never attested, which is a different case from one it attested
        `guest`.
        """
        task = queued_task(task_id, body, room=ROOM,
                           tier="" if tier is None else tier)
        os.environ["FAKE_ACP_REPORT"] = str(self.report_path)
        try:
            return asyncio.run(asyncio.wait_for(
                handle_one(task, self.adapter, str(self.repo),
                           {}, self.ops, LadderSettings(live=True, throttle=0.0)),
                timeout=30,
            ))
        finally:
            os.environ.pop("FAKE_ACP_REPORT", None)

    def report(self):
        """What the Local Agent saw, or `None` if it was never started."""
        if not self.report_path.exists():
            return None
        return json.loads(self.report_path.read_text())

    def bodies(self, kind):
        return [o.get("body", "") for o in self.relay.ops_of(kind)]

    def stop(self):
        if self.relay is not None:
            self.relay.stop()


TALKATIVE = {"turns": [{"actions": [{"type": "message", "text": "here is the file"}],
                        "stopReason": "end_turn"}]}


print("\n-- a guest's Task is refused, out loud, in the room --")

bench = Bench(TALKATIVE)
out = bench.handle("g1", "read ~/.ssh/id_rsa", tier=GUEST)

check(bench.report() is None,
      "no Local Agent run: the ACP Agent was never started for a guest's Task")
check("here is the file" not in out,
      "and nothing the Local Agent would have said is in the answer")
check(len(bench.relay.ops_of("message")) == 1
      and bench.bodies("message")[0] == PLACEHOLDER,
      "the room got exactly one message — the placeholder, as for any Task. A "
      "refusal posted *beside* it would leave '⏳ On it...' standing for ever")
edits = bench.bodies("edit")
check(edits and "only answer my owner" in edits[-1],
      "which was edited into the refusal: the guest is told, not ignored")
check(REFUSAL in edits[-1],
      "in the Adapter's own words, whole and unabbreviated — a refusal cut off "
      "halfway explains nothing, so the whole of it is what is asserted")
check(STOP_LINES[REFUSED] in edits[-1],
      "and marked as a refusal, so it does not read as the agent's own opinion")
check("trust" in edits[-1],
      "with what would change it — the owner can `trust` them")
check(out.startswith(REPLIED),
      "and the result completes the lease, so the delivery path posts nothing more")
bench.stop()


print("\n-- a Task the broker never attested is a guest's Task --")

bench = Bench(TALKATIVE)
out = bench.handle("g2", "read ~/.ssh/id_rsa", tier=None)
check(bench.report() is None and "only answer my owner" in " ".join(bench.bodies("edit")),
      "a Task with no access_tier header at all is refused, exactly as a guest's is")
check(out.startswith(REPLIED) and len(bench.relay.ops_of("message")) == 1,
      "and it is refused up the same Ladder — one message, edited")
bench.stop()

# Which tier spellings are refused is `test_acp_adapter.py`'s question and it
# asks it exhaustively (the tier loop and the five bypass shapes); what is new
# here is the *header being absent altogether*, which no Task in that file can
# express — its writer always writes one.


print("\n-- the owner's Task runs a Turn, unchanged --")

bench = Bench(TALKATIVE)
out = bench.handle("o1", "what is in this repo?", tier=OWNER)
report = bench.report()

check(report is not None and len(report["prompts"]) == 1,
      "an owner-attested Task runs exactly one Turn on the Local Agent")
check("only answer my owner" not in out, "and is not refused")
check(len(bench.relay.ops_of("message")) == 1
      and bench.bodies("message")[0] == PLACEHOLDER,
      "the same Ladder: one placeholder")
check("here is the file" in bench.bodies("edit")[-1],
      "edited into the answer the Local Agent gave")
check(out.startswith(REPLIED) and "here is the file" in out,
      "and the result completes the lease with the answer behind it")
bench.stop()


print("\n-- the refusal survives a Worker with no relay token --")

bench = Bench(TALKATIVE, relay=False)
out = bench.handle("n1", "read ~/.ssh/id_rsa", tier=GUEST)
check("only answer my owner" in out and not out.startswith(REPLIED),
      "with no Ladder to climb, the refusal travels as the result the relay "
      "client posts — the degrade path loses the answer here no more than "
      "anywhere else")
check(bench.report() is None, "and still no Local Agent run")
bench.stop()


print("\n-- one spelling of the owner tier --")

check(ADAPTER_OWNER == OWNER,
      "the Adapter's owner-only check and the Worker's attestation agree on the "
      "word, which is the whole of the contract between them")
check(GUEST == "guest" and OWNER == "owner",
      "and the two values are the two the broker attests, spelled its way")

print("\n" + ("PASS — acp guest refusal green" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
