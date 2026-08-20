"""The request core, against a broker on localhost (B1, C3, C8, I3, G4-adjacent).

The User-Agent is the load-bearing one. CloudFlare's bot-fight mode answers
`python-urllib`'s default UA with a 403, and the fix had to be rediscovered
three times — once per call site — the last of which classified the 403 as fatal
and stopped the event channel permanently on its first connect against a real
gateway. One request path here means one place that can forget it, so the test
is: whatever the library asks for, the UA is explicit and urllib's default never
reaches the wire.

Run: python3 tests/test_transport.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import socket
import tempfile
import time
from pathlib import Path

from fake_broker import FakeBroker

from ag2_relay_client.credentials import TokenSource
from ag2_relay_client.transport import AuthRejected, RelayHTTP, RelayHTTPError
from ag2_relay_client.resolver import BoundedResolver

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


with FakeBroker() as broker:
    creds = TokenSource(token=f"{broker.url}|SECRET")
    http = RelayHTTP(creds)

    # --- the request lands where the base URL says, not where a default says
    broker.on("GET", "/v1/tasks", json={"tasks": [{"id": "task-1"}]})
    answer = http.get("/v1/tasks", params={"wait": 25})
    check(answer == {"tasks": [{"id": "task-1"}]}, "a GET returns the parsed body")
    asked = broker.took("GET", "/v1/tasks")
    check(len(asked) == 1, "the path is composed onto the provisioned base URL")
    check(asked[0].query == "wait=25", "query parameters ride along")

    # --- B1: the UA is explicit on every path, and urllib's default on none
    broker.on("POST", "/v1/results", json={"ok": True})
    http.post("/v1/results", {"id": "task-1", "body": "done"})
    broker.on("POST", "/v1/heartbeat", status=204)
    http.post("/v1/heartbeat", {"status": "idle"})
    uas = {r.headers.get("User-Agent", "") for r in broker.requests}
    check(len(uas) == 1 and "" not in uas, "every request carried one explicit User-Agent")
    check(not any("urllib" in ua.lower() or "python" in ua.lower() for ua in uas),
          "urllib's default User-Agent never reaches the wire")
    check(http.user_agent in uas, "the UA is the one the client advertises")

    # --- the bearer, and the fact that it is read live
    posted = broker.took("POST", "/v1/results")[0]
    check(posted.headers.get("Authorization") == "Bearer SECRET", "the bearer is sent")
    check(posted.headers.get("Content-Type") == "application/json",
          "a POST body is JSON, and says so")
    check(posted.json == {"id": "task-1", "body": "done"}, "the body arrives intact")

    # --- an empty 2xx body is an empty answer, not a parse error
    check(http.post("/v1/heartbeat", {"status": "idle"}) == {},
          "a 204 with no body parses as {}")

    # --- C8: 401/403 are their own class, so a best-effort caller cannot
    # swallow them along with the failures it is allowed to swallow
    broker.on("POST", "/v1/ack", status=401, json={"error": "unknown token"})
    rejected = None
    try:
        http.post("/v1/ack", {"id": "task-1"})
    except AuthRejected as exc:
        rejected = exc
    check(rejected is not None and rejected.status == 401,
          "a 401 raises AuthRejected, carrying the status")
    check(isinstance(rejected, RelayHTTPError), "AuthRejected is an HTTP error too")

    broker.on("POST", "/v1/room", status=403, json={"error": "forbidden"})
    try:
        http.post("/v1/room", {"op": "message"})
        forbidden = None
    except AuthRejected as exc:
        forbidden = exc
    check(forbidden is not None, "a 403 raises AuthRejected as well")

    # --- other statuses carry status AND body: the ack path has to sniff a 404
    broker.on("POST", "/v1/ack", status=404, body="task not found")
    sniffable = None
    try:
        http.post("/v1/ack", {"id": "task-1"})
    except RelayHTTPError as exc:
        sniffable = exc
    check(sniffable is not None and sniffable.status == 404, "a 404 raises with its status")
    check("task not found" in sniffable.body,
          "the response body survives the exception, for content-sniffing")
    check("SECRET" not in str(sniffable), "no exception carries the bearer")

    # An edge that answers 200 with an HTML interstitial is not a task list;
    # it must fail loudly enough to back off, not parse into silence.
    broker.on("GET", "/v1/healthz", status=200, body="<html>just a moment…</html>")
    not_json = None
    try:
        http.get("/v1/healthz")
    except RelayHTTPError as exc:
        not_json = exc
    check(not_json is not None and "json" in str(not_json).lower(),
          "a 200 that is not JSON is an error, not an empty answer")

    # --- D3: `str(exc)` is not just for reading. The client writes it into
    # connection-status.json, which nothing downstream redacts and which lives
    # under a state dir that syncs to a vault — so the URL in the message goes
    # through the same redaction every persisted gateway URL does.
    broker.on("GET", "/v1/healthz", status=200, body="<html>just a moment…</html>")
    with_query = None
    try:
        http.get("/v1/healthz", params={"token": "abc"})
    except RelayHTTPError as exc:
        with_query = exc
    check(with_query is not None and "token=abc" not in str(with_query),
          "the not-JSON message redacts the URL it names")

    leaky = RelayHTTPError(500, "boom", "https://u:pw@gw.example/relay?token=abc#f")
    check("pw" not in str(leaky) and "token=abc" not in str(leaky)
          and "#f" not in str(leaky),
          "a gateway with userinfo, query or fragment reaches the message redacted")
    check("gw.example" in str(leaky) and "500" in str(leaky),
          "and still says which gateway answered what")
    check(leaky.url == "https://u:pw@gw.example/relay?token=abc#f",
          "the raw URL stays on the attribute, for a caller that has to act on it")

with FakeBroker() as broker:
    # --- C3/C5: the bearer is read through the token source on every request,
    # so a rotation reaches the wire without a restart
    with tempfile.TemporaryDirectory() as tmp:
        token_file = Path(tmp) / "token.env"
        token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CFIRST\n")
        creds = TokenSource(token_file=token_file)
        http = RelayHTTP(creds)
        broker.on("GET", "/v1/tasks", json={"tasks": []})
        http.get("/v1/tasks")
        token_file.write_text(f"REMOTE_TASK_TOKEN={broker.url}%7CSECOND\n")
        creds.reload()
        http.get("/v1/tasks")
        bearers = [r.headers.get("Authorization") for r in broker.took("GET", "/v1/tasks")]
        check(bearers == ["Bearer FIRST", "Bearer SECOND"],
              "a rotated token is on the very next request — no restart, no rebuild")

with FakeBroker() as broker:
    # --- the client's own resolver is what opens the connection (A4)
    calls = []

    def counting(host, port, *args, **kwargs):
        calls.append(host)
        return socket.getaddrinfo(host, port, *args, **kwargs)

    http = RelayHTTP(
        TokenSource(token=f"{broker.url}|SECRET"),
        resolver=BoundedResolver(getaddrinfo=counting),
    )
    broker.on("GET", "/v1/healthz", json={"ok": True})
    http.get("/v1/healthz")
    check(calls, "the request went through this client's bounded resolver")

    # --- a socket timeout is the caller's to set, and it is enforced
    broker.on("GET", "/v1/tasks", json={"tasks": []}, delay=1.0)
    started = time.monotonic()
    timed_out = None
    try:
        http.get("/v1/tasks", timeout=0.2)
    except OSError as exc:  # URLError and socket.timeout are both OSError
        timed_out = exc
    check(timed_out is not None, "a slow answer raises rather than hanging")
    check(time.monotonic() - started < 1.0, "and raises on the timeout, not on the answer")

with FakeBroker() as broker, FakeBroker() as elsewhere:
    # --- a credentialed request never follows a redirect: urllib re-sends the
    # Authorization header on the hop, which would hand this bearer to whatever
    # host the answer named.
    #
    # The named host is a SECOND live broker, because that is the only place a
    # followed redirect would be visible: a request that did follow lands there
    # and is never recorded by the one that redirected it. Asserting on the
    # first broker's log instead is an assertion that cannot fail.
    http = RelayHTTP(TokenSource(token=f"{broker.url}|SECRET"))
    elsewhere.on("GET", "/v1/tasks", json={"tasks": [{"id": "stolen"}]})
    broker.on("GET", "/v1/tasks", status=302,
              headers={"Location": f"{elsewhere.url}/v1/tasks"})
    followed = None
    try:
        http.get("/v1/tasks")
    except RelayHTTPError as exc:
        followed = exc
    check(followed is not None and followed.status == 302,
          "a redirect is refused, not followed, while credentialed")
    check(elsewhere.requests == [],
          "the host the redirect named was never contacted at all")
    check(not any(r.header("Authorization") for r in elsewhere.requests),
          "so the bearer never left for it")

print("\n" + ("PASS — transport green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
