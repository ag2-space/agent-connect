"""A broker on localhost, for tests that need the real HTTP path.

The library's whole job is a conversation with one HTTP service, and the parts
of it that have historically broken — a header the edge rejects, a lease
redelivered, an ack answered 404 by two different things — are only visible
through a socket. So the suite drives a stdlib `http.server` that records what
arrived and answers what the test told it to.

    with FakeBroker() as broker:
        broker.on("GET", "/v1/tasks", json={"tasks": []})
        ...
        assert broker.requests[0].headers["User-Agent"] == ...

Responses are queued per (method, path): each call to `on` appends one, and the
last queued response repeats once the queue is drained — so a test that cares
about a sequence programs a sequence, and one that does not programs a single
answer and stops thinking about it.

A path may carry a `*` (`/v1/tasks/*/ack`), which matches any single segment
run. The ack route carries the task id *in the path*, and a suite that had to
name every id it was about to invent would program its fixtures twice; an exact
route always wins over a pattern, so a test can still answer one id differently
from the rest — which is the whole shape of the ack-404 scar (F4).

The base path (`/relay`) is deliberate: it is where the real deployment lives,
and it catches a client that builds URLs from the host instead of the base.
"""
from __future__ import annotations

import json as jsonlib
import threading
import time
from fnmatch import fnmatchcase
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, NamedTuple, Optional
from urllib.parse import urlsplit

BASE_PATH = "/relay"


class Recorded(NamedTuple):
    """One request, as the broker saw it."""

    method: str
    path: str          # full path, base included, query stripped
    query: str
    headers: Dict[str, str]
    body: bytes

    @property
    def json(self):
        return jsonlib.loads(self.body.decode() or "{}")


class _Answer(NamedTuple):
    status: int
    body: bytes
    headers: Dict[str, str]
    delay: float


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: A003 — silence the stderr access log
        pass

    def _serve(self):
        broker: "FakeBroker" = self.server.broker  # type: ignore[attr-defined]
        split = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        broker.requests.append(Recorded(
            self.command, split.path, split.query,
            {k: v for k, v in self.headers.items()}, body,
        ))
        answer = broker.next_answer(self.command, split.path)
        if answer.delay:
            time.sleep(answer.delay)
        self.send_response(answer.status)
        for key, value in answer.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(answer.body)))
        self.end_headers()
        self.wfile.write(answer.body)

    do_GET = do_POST = do_PUT = do_DELETE = _serve


class FakeBroker:
    """A broker that answers on 127.0.0.1 and remembers what it was asked."""

    def __init__(self, base_path: str = BASE_PATH):
        self.base_path = base_path
        self.requests: List[Recorded] = []
        self._answers: Dict[tuple, List[_Answer]] = {}
        self._served: Dict[tuple, int] = {}
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.broker = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # --- lifecycle
    def __enter__(self) -> "FakeBroker":
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    @property
    def url(self) -> str:
        """The base URL a client is provisioned with, base path included."""
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}{self.base_path}"

    # --- programming
    def on(self, method: str, path: str, status: int = 200, body=b"",
           json=None, headers: Optional[Dict[str, str]] = None, delay: float = 0.0):
        """Queue one answer for `method` on `path` (relative to the base path).

        Queueing onto a route whose answers have all been served starts a fresh
        sequence: "answer this from now on" is what a test means when it
        programs a route it has already exercised, not "after the repeats".
        """
        if json is not None:
            body = jsonlib.dumps(json).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        if isinstance(body, str):
            body = body.encode()
        key = (method.upper(), self.base_path + path)
        with self._lock:
            queued = self._answers.setdefault(key, [])
            if self._served.get(key, 0) >= len(queued) and queued:
                queued.clear()
                self._served[key] = 0
            queued.append(_Answer(status, body, headers or {}, delay))
        return self

    def next_answer(self, method: str, path: str) -> _Answer:
        key = (method.upper(), path)
        with self._lock:
            if key not in self._answers:
                key = self._pattern_for(key) or key
            queued = self._answers.get(key)
            if not queued:
                return _Answer(404, b'{"error":"no route"}',
                               {"Content-Type": "application/json"}, 0.0)
            # The last queued answer repeats: a test that programmed one answer
            # is saying "always this", not "once".
            index = min(self._served.get(key, 0), len(queued) - 1)
            self._served[key] = self._served.get(key, 0) + 1
            return queued[index]

    def _pattern_for(self, key: tuple) -> Optional[tuple]:
        """The programmed pattern route matching `key`, longest first.

        Longest-first so `/v1/tasks/task-1/ack` beats `/v1/tasks/*/ack` when a
        test programmed both — an exact answer is never shadowed by a general
        one, which is what lets one stale lease be tested against a host that
        keeps acking everything else.
        """
        method, path = key
        candidates = [k for k in self._answers
                      if k[0] == method and "*" in k[1] and fnmatchcase(path, k[1])]
        candidates.sort(key=lambda k: len(k[1]), reverse=True)
        return candidates[0] if candidates else None

    # --- reading
    def took(self, method: str, path: str) -> List[Recorded]:
        """Every request that arrived for `method` on `path` (`*` allowed)."""
        full = self.base_path + path
        return [r for r in self.requests
                if r.method == method.upper()
                and (r.path == full or ("*" in full and fnmatchcase(r.path, full)))]

    def forget(self) -> "FakeBroker":
        """Drop the request log, keeping the programmed answers.

        A test that has finished setting a scene and wants to assert on what
        happens next reads much better than one counting offsets into a log of
        everything since the client started.
        """
        with self._lock:
            self.requests = []
        return self
