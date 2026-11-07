"""Owner questions: discovery, the mint, the owner's private DM, ask and inspect.

The relay and its mint run on one fake broker (the mint must be the relay's own
origin); the MCP endpoint is a second server that answers by tool and action.

Run: python3 tests/test_questions.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import io
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fake_broker import FakeBroker

from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.questions import MINT_PATH, Answer, Owner, OwnerQuestions
from ag2_relay_client.transport import RelayHTTP

AGENT = "@cc.agent:s.space"
OWNER = "@mark:s.space"
DM = "!dm:s.space"
ACCESS = "ACCESS-TOKEN-xyz"
SECRET = "RELAY-SECRET-abc"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Mcp:
    """A fake MCP endpoint: `answers[name or action]` is a list of replies, the last repeating."""

    def __init__(self):
        self.calls, self.answers = [], {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                params = body.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                key = args.get("action") or name
                outer.calls.append((self.headers.get("Authorization"), name, args,
                                    self.headers.get("MCP-Protocol-Version")))
                queue = outer.answers.get(key) or [(200, {"result": {"isError": True,
                                                                     "structuredContent": {"code": "NOPE"}}})]
                status, reply = queue.pop(0) if len(queue) > 1 else queue[0]
                raw = json.dumps(reply).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        host, port = self.server.server_address[:2]
        self.url = f"http://{host}:{port}/mcp"

    def ok(self, key, envelope):
        self.answers[key] = [(200, {"jsonrpc": "2.0", "id": 1,
                                    "result": {"structuredContent": envelope}})]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def scene(broker, mcp, members=(AGENT, OWNER), mint_url=None, mint_status=200):
    broker.on("GET", "/v1/mcp/discovery",
              json={"mint_url": mint_url or broker.url + MINT_PATH, "mcp_url": mcp.url})
    broker.on("POST", MINT_PATH, status=mint_status,
              json={"access_token": ACCESS, "expires_in": 900} if mint_status == 200
              else {"error": "slow down"})
    broker.on("GET", "/v1/agents", json={"agents": [
        {"id": "@other.agent:s.space", "owner": "@someone:s.space", "owner_dm_room": "!x:s.space"},
        {"id": AGENT, "owner": OWNER, "owner_dm_room": DM}]})
    broker.on("POST", "/v1/room", json={"members": [{"user_id": m} for m in members]})
    mcp.ok("ag2.whoami", {"actor": {"id": AGENT, "kind": "agent"}})
    mcp.ok("room.question.ask", {"status": "completed", "result": {
        "question_id": "q1", "event_id": "$q", "room_id": DM, "revision": 1,
        "status": "pending", "expires_at": "2026-11-08T00:00:00Z"}})
    mcp.ok("room.question.inspect", {"status": "completed", "result": {
        "room_id": DM, "event_id": "$q", "can_answer": False, "question": {
            "status": "answered", "revision": 3,
            "accepted_answer": {"kind": "choice", "option_id": "release",
                                "respondent": OWNER, "accepted_at": "x"}}}})


def make(broker, clock=None):
    http = RelayHTTP(TokenSource(token=f"{broker.url}|{SECRET}"))
    return OwnerQuestions(http, clock=clock or Clock())


logs = io.StringIO()
handler = logging.StreamHandler(logs)
logging.getLogger("ag2_relay_client").addHandler(handler)
logging.getLogger("ag2_relay_client").setLevel(logging.DEBUG)

# --- the whole path ----------------------------------------------------------
with FakeBroker(base_path="") as broker:
    mcp = Mcp()
    try:
        scene(broker, mcp)
        q = make(broker)
        check(q.owner() == Owner(OWNER, DM), "the owner and their verified-private DM are found")
        qid = q.ask("Post this reply?", [("release", "Post it"), ("discard", "Discard")],
                    expires_in_s=86400, operation_id="wr_1")
        check(qid == "q1", "ask returns the question id")
        auth, name, args, version = mcp.calls[-1]
        check(name == "room.action.execute" and args["room_id"] == DM
              and args["action"] == "room.question.ask" and args["operation_id"] == "wr_1",
              "ask goes through room.action.execute in the owner's DM, with the operation id")
        a = args["arguments"]
        check(a["target_members"] == [OWNER] and a["answerable_by"] == "targets"
              and a["allow_free_text"] is False and a["response_mode"] == "first"
              and a["options"] == [{"id": "release", "label": "Post it"},
                                   {"id": "discard", "label": "Discard"}]
              and a["expires_in_seconds"] == 86400,
              "targeted at the owner alone, fixed options, first answer closes it")
        check(auth == f"Bearer {ACCESS}" and version == "2025-06-18",
              "MCP calls carry the access token, not the relay bearer, and the protocol version")
        check(q.inspect("q1") == Answer("answered", "release", "3"), "inspect reads the chosen option")
        check(mcp.calls[-1][1] == "room.action.read", "inspect goes through room.action.read")
        check(len(broker.took("POST", MINT_PATH)) == 1, "one mint serves every call while it lives")
        check(all(SECRET not in (c[0] or "") for c in mcp.calls),
              "the relay bearer never reaches the MCP endpoint")
        check(broker.took("POST", MINT_PATH)[0].header("Authorization") == f"Bearer {SECRET}",
              "the mint gets the relay bearer")

        mcp.ok("room.question.inspect", {"result": {"question": {
            "status": "answered", "revision": 4, "accepted_answer": {"kind": "text", "text": "hm"},
            "answer_withheld": {"answered": True, "held": True, "reason": "x"}}}})
        check(q.inspect("q1") == Answer("held", "", "4"), "a withheld answer reads as held, no option")
        mcp.ok("room.question.inspect", {"result": {"question": {"status": "pending", "revision": 1,
                                                                 "accepted_answer": None}}})
        check(q.inspect("q1") == Answer("pending", "", "1"), "a pending question has no option")
    finally:
        mcp.close()

# --- a shared room is not a DM -------------------------------------------------
with FakeBroker(base_path="") as broker:
    mcp = Mcp()
    try:
        scene(broker, mcp, members=(AGENT, OWNER, "@guest:s.space"))
        q = make(broker)
        check(q.owner() == Owner(OWNER, ""), "a room with anyone else in it is not the owner's DM")
        check(q.ask("p", [("a", "A")], expires_in_s=60, operation_id="o") is None,
              "and nothing is asked there")
        check(not any(c[2].get("action") == "room.question.ask" for c in mcp.calls),
              "not even attempted")
    finally:
        mcp.close()

# --- discovery rules -------------------------------------------------------------
for label, bad in (("another origin", "https://evil.example" + MINT_PATH),
                   ("another path", None)):
    with FakeBroker(base_path="") as broker:
        mcp = Mcp()
        try:
            scene(broker, mcp, mint_url=bad or broker.url + "/api/v1/other")
            q = make(broker)
            check(q.owner() is None and q.ask("p", [("a", "A")], expires_in_s=60,
                                              operation_id="o") is None,
                  f"a mint_url on {label} is refused")
            check(not broker.took("POST", MINT_PATH) and not mcp.calls,
                  f"and the relay bearer goes nowhere ({label})")
        finally:
            mcp.close()

# --- re-mint on 401 and on an auth envelope -------------------------------------
with FakeBroker(base_path="") as broker:
    mcp = Mcp()
    try:
        scene(broker, mcp)
        mcp.answers["ag2.whoami"] = [(401, {"error": "expired"}),
                                     (200, {"result": {"structuredContent": {"actor": {"id": AGENT}}}})]
        q = make(broker)
        check(q.owner() == Owner(OWNER, DM), "an HTTP 401 from MCP mints once more and succeeds")
        check(len(broker.took("POST", MINT_PATH)) == 2, "with exactly one extra mint")
        mcp.answers["room.question.ask"] = [
            (200, {"result": {"isError": True, "content": [
                {"type": "text", "text": "failed"},
                {"type": "text", "text": json.dumps({"code": "AUTHENTICATION_FAILED"})}]}}),
            (200, {"result": {"structuredContent": {"result": {"question_id": "q2"}}}})]
        check(q.ask("p", [("a", "A")], expires_in_s=60, operation_id="o") == "q2",
              "an AUTHENTICATION_FAILED envelope also mints again and retries")
        check(len(broker.took("POST", MINT_PATH)) == 3, "once")
        mcp.answers["room.question.ask"] = [(401, {})]
        check(q.ask("p", [("a", "A")], expires_in_s=60, operation_id="o") is None,
              "a second refusal gives up with None")
    finally:
        mcp.close()

# --- 429 backs the mint off -------------------------------------------------------
with FakeBroker(base_path="") as broker:
    mcp = Mcp()
    try:
        scene(broker, mcp, mint_status=429)
        clock = Clock()
        q = make(broker, clock)
        check(q.owner() is None, "a rate-limited mint is None")
        clock.now += 31  # past the owner retry window, inside the mint backoff
        check(q.owner() is None and len(broker.took("POST", MINT_PATH)) == 1,
              "no mint is tried again inside 60 s")
        broker.on("POST", MINT_PATH, json={"access_token": ACCESS, "expires_in": 900})
        clock.now += 30
        check(q.owner() == Owner(OWNER, DM), "after the backoff it mints and works")
    finally:
        mcp.close()

# --- an expiring token is re-minted -------------------------------------------------
with FakeBroker(base_path="") as broker:
    mcp = Mcp()
    try:
        scene(broker, mcp)
        clock = Clock()
        q = make(broker, clock)
        q.owner()
        clock.now += 900 - 59
        q.inspect("q1")
        check(len(broker.took("POST", MINT_PATH)) == 2, "a token is replaced a minute before it expires")
    finally:
        mcp.close()

# --- nothing reachable ----------------------------------------------------------------
dead = OwnerQuestions(RelayHTTP(TokenSource(token=f"http://127.0.0.1:9|{SECRET}"), timeout=2))
check(dead.owner() is None and dead.inspect("q") is None
      and dead.ask("p", [("a", "A")], expires_in_s=60, operation_id="o") is None,
      "an unreachable relay is None everywhere, nothing raised")

text = logs.getvalue()
check(ACCESS not in text and SECRET not in text, "no token or bearer in any log line")
check("owner questions" in text, "failures are logged")

try:
    dead._http = None
    check(False, "the transport is sealed")
except AttributeError:
    check(True, "the transport is sealed")

if fails:
    raise SystemExit(f"FAIL — {fails} check(s)")
print("PASS — owner questions")
