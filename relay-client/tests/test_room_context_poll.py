"""Per-poll scalar context-format proof on the actual localhost HTTP path.

Only an explicitly capable consumer opts in. The generic Relay Client remains
wait-only; capability declaration grants no room history or execution tier.
"""
import _bootstrap
import tempfile
from urllib.parse import parse_qs

from fake_broker import FakeBroker
from ag2_relay_client import RelayClient, TokenSource
from ag2_relay_client.client import CAPABILITIES

fails = 0

def check(cond, label):
    global fails
    print(("  ok   " if cond else "  FAIL ") + label)
    fails += not cond

for label, capability_options, expected in (
    ("generic default", {}, None),
    ("explicit room consumer", {"capabilities": CAPABILITIES + ("room-context-v1",)}, "room-context-v1"),
    ("empty capability list", {"capabilities": ()}, None),
    ("other heartbeat capability", {"capabilities": CAPABILITIES + ("other-format",)}, None),
):
    with FakeBroker() as broker, tempfile.TemporaryDirectory() as state:
        broker.on("GET", "/v1/tasks", json={"tasks": []})
        broker.on("POST", "/v1/heartbeat", json={"ok": True})
        client = RelayClient(TokenSource(token=broker.url + "|synthetic-secret"), state,
                             singleton=False, poll_wait=7, **capability_options)
        client.prepare()
        client.poll_once(); client.poll_once()
        polls = broker.took("GET", "/v1/tasks")
        query = {"wait": ["7"]}
        if expected:
            query["capabilities"] = [expected]
        check(len(polls) == 2 and all(parse_qs(poll.query) == query for poll in polls),
              label + " uses exactly its supported scalar proof on every poll")
        check(all(poll.header("Authorization") == "Bearer synthetic-secret" for poll in polls),
              label + " preserves authenticated poll path")
        heartbeat = broker.took("POST", "/v1/heartbeat")[0].json
        check(("room-context-v1" in heartbeat["capabilities"]) == bool(expected),
              label + " retains independent heartbeat metadata")

print("\n" + ("PASS — room-context poll negotiation" if not fails else f"FAIL — {fails}"))
raise SystemExit(bool(fails))
