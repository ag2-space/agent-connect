"""The one place this library makes an HTTP request.

Everything the broker conversation needs from HTTP is here, and only here,
because each of the properties below was rediscovered separately at each call
site the last time they were spread out:

- **An explicit User-Agent on every request (B1).** The deployment edge runs
  CloudFlare bot-fight, which answers `python-urllib`'s default UA with a 403.
  The fix existed three times — poll path, Room Ops, event channel — and the
  third copy classified its 403 as fatal, so the event channel stopped
  permanently the first time it met a real gateway.
- **The bearer read live (C3).** The token is fetched from the credential source
  per request rather than captured, so a rotation applied by auth recovery is on
  the very next call with no restart and no object rebuilt.
- **401/403 as their own exception class (C8).** Best-effort callers — ack,
  heartbeat — are allowed to swallow failures; they are not allowed to swallow a
  revoked bearer, and a distinct class is what makes the difference impossible
  to miss.
- **No redirect while credentialed.** urllib re-sends the `Authorization` header
  across a redirect hop, so following one would hand this bearer to whatever
  host the answer named. A redirect from the broker is a misconfiguration; it
  surfaces as an error.
- **This client's own resolver.** Connections are opened through a
  `BoundedResolver` handed in at construction, so the DNS bound and the v4
  preference are properties of this client rather than of the interpreter.

The base URL comes from the credential (I3) — there is no compiled-in gateway
anywhere in this package.

`fetch` is the one request that goes to an *absolute* URL rather than a path
under that base — the media ingress follows a URL the broker wrote — and it is
here, beside the bearer, because deciding whether that URL gets one is the whole
of G4. `same_origin` and `under_base` are that decision, written as two
functions a test can call directly.
"""
from __future__ import annotations

import http.client
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, NamedTuple, Optional

from .credentials import TokenSource
from .resolver import BoundedResolver
from .state import redact_url

log = logging.getLogger(__name__)

#: What this client calls itself to the deployment edge.
#:
#: The value is inherited rather than invented: it is the string the edge has
#: been letting through since the bot-fight 403 was first diagnosed, and the
#: requirement it satisfies — "explicit and not urllib's default" — is a
#: property of the contract with that edge, not of this package's name. Changing
#: it is an edge change, so it is a construction parameter for anyone who has
#: made one.
USER_AGENT = "sutando-gateway-client/1.0"

#: A default for calls that are not the long poll. The poll sets its own, tied
#: to its `wait` — a socket timeout below the long-poll window would turn every
#: idle poll into an error.
DEFAULT_TIMEOUT_S = 20.0


class RelayHTTPError(Exception):
    """The broker answered, and the answer was not usable.

    Carries the status and the response body, because more than one caller has
    to look at the body to know what the status meant — an ack answered `404`
    means "no such task" from one deployment and "no such endpoint" from
    another, and only the body tells them apart.

    The URL is on the attribute in full and in the message redacted (D3). The
    message is not just for reading: the client writes `str(exc)` into
    `connection-status.json`, which nothing downstream redacts and which lives
    under a state dir that syncs to a vault — so a gateway provisioned with
    `user:pass@` or a `?token=` query would land there in plaintext.
    """

    def __init__(self, status: int, body: str, url: str, message: str = ""):
        self.status = status
        self.body = body or ""
        #: The URL as requested, unredacted, for a caller that has to act on it.
        self.url = url
        super().__init__(
            message or f"HTTP {status} from {redact_url(url)}: {self.body[:200]}")


class AuthRejected(RelayHTTPError):
    """`401` or `403`: this bearer is not (or is no longer) accepted.

    Its own class so that a best-effort call site cannot absorb it as one more
    optional failure — auth recovery has to see it.
    """


class ResponseTooLarge(Exception):
    """The body ran past the ceiling the caller set, and was not read further.

    Its own class because it is the one fetch failure that must never be
    retried: a file does not shrink, and the second attempt would cost the same
    bandwidth to reach the same answer.
    """

    def __init__(self, limit: int):
        self.limit = int(limit)
        super().__init__(f"the answer ran past the {self.limit}-byte ceiling")


class Fetched(NamedTuple):
    """Bytes from an absolute URL, and the two facts about how they arrived."""

    body: bytes
    #: The response's `Content-Type`, verbatim and possibly empty. What the
    #: *fetch* said the bytes are — which outranks whatever a marker claimed.
    content_type: str
    #: Whether this bearer was sent. Carried so a caller can log the routing
    #: decision it did not make itself.
    credentialed: bool


def same_origin(url: str, base: str) -> bool:
    """True when `url` shares scheme, host and port with `base`.

    **Parsed, never string-prefix.** `https://relay.example.evil` starts with
    `https://relay.example`, and a substring comparison is how a bearer reaches
    a look-alike host (review 2026-07-03). The default port is filled in on both
    sides so `https://host` and `https://host:443` are the one origin they are.

    The whole body is guarded: `.port` raises `ValueError` at *access* time for
    a malformed authority (`https://host:bad/`), and a gateway-written URL must
    never crash task intake. Unparseable means no match, which means no bearer.
    """
    try:
        one, other = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base)
        if not one.scheme or not one.hostname or one.scheme != other.scheme:
            return False
        default = {"https": 443, "http": 80}.get(one.scheme)
        return (one.hostname.lower() == (other.hostname or "").lower()
                and (one.port or default) == (other.port or default))
    except ValueError:
        return False


def under_base(url: str, base: str) -> bool:
    """True when `url` is genuinely served by `base` — origin *and* base path.

    The path half matters as much as the origin: a gateway at
    `https://host/relay` does not vouch for `https://host/relay-evil/…`, and the
    boundary that tells those apart is a real `/`. Same guard as `same_origin`:
    malformed is False, never an exception.
    """
    if not base or not same_origin(url, base):
        return False
    try:
        base_path = urllib.parse.urlsplit(base).path.rstrip("/")
        path = urllib.parse.urlsplit(url).path
    except ValueError:  # pragma: no cover — same_origin already parsed both
        return False
    return path == base_path or path.startswith(base_path + "/")


class RelayHTTP:
    """Requests to one gateway, as one bearer."""

    def __init__(
        self,
        credentials: TokenSource,
        user_agent: str = USER_AGENT,
        resolver: Optional[BoundedResolver] = None,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self.credentials = credentials
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.resolver = resolver or BoundedResolver()
        self._opener = _opener_for(self.resolver)

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        # Redacted for the same reason the errors are: a repr reaches logs and
        # tracebacks, and the gateway may carry userinfo or a query (D3).
        return f"<RelayHTTP {redact_url(self.base_url)}>"

    @property
    def base_url(self) -> str:
        """Where this client is provisioned to talk. Never changes: a gateway
        move is a restart, not a rotation."""
        return self.credentials.base_url

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None,
            timeout: Optional[float] = None) -> Any:
        return self.request("GET", path, params=params, timeout=timeout)

    def post(self, path: str, payload: Optional[Mapping[str, Any]] = None,
             timeout: Optional[float] = None) -> Any:
        return self.request("POST", path, payload=payload, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """One request, and its parsed answer.

        Raises `AuthRejected` on 401/403, `RelayHTTPError` on any other
        unusable answer, and lets network errors through as the `OSError`
        subclasses urllib raises — the poll loop's reconnect branch already
        knows those.
        """
        url = self._url(path, params)
        data = None
        headers = self._headers()
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url, data=data, method=method.upper())
        for name, value in headers.items():
            request.add_header(name, value)

        try:
            deadline = self.timeout if timeout is None else timeout
            with self._opener.open(request, timeout=deadline) as response:
                status = getattr(response, "status", 200)
                raw = (response.read() or b"").decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as exc:
            body = _drain(exc)
            if exc.code in (401, 403):
                raise AuthRejected(exc.code, body, url) from None
            raise RelayHTTPError(exc.code, body, url) from None

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            # A 200 carrying an HTML interstitial is the edge talking, not the
            # broker. Loud enough to back off on, rather than an empty answer.
            raise RelayHTTPError(
                status, raw, url,
                f"answer from {redact_url(url)} was not JSON: {raw[:200]}"
            ) from None

    def fetch(self, url: str, cap: int, timeout: Optional[float] = None) -> Fetched:
        """GET an absolute URL for its bytes, deciding for itself about the bearer.

        The media ingress is the one caller: the URL it follows was written by
        the broker, into a body a room member composed, so *this* method decides
        the credential routing rather than trusting the caller to (G4).

        - The bearer goes out **only** when `under_base` says the URL is the
          gateway's own — exact parsed origin, at or under the base path with a
          real `/` boundary. Everything else is fetched anonymously. There is no
          second credential here: on the AG2 Space wire the broker downloads
          from the homeserver box-side as the agent, so a worker never needs a
          Matrix token to read an attachment.
        - **No redirect is followed, credentialed or not.** urllib re-sends the
          `Authorization` header across a hop, so following one while
          credentialed hands this bearer to whatever host the answer named. The
          uncredentialed case is held to the same rule on purpose: the softer
          alternative is a second opener that does follow, and a second opener
          is a second place a bearer could end up.
        - **`cap + 1` bytes are read**, and one over the cap is a refusal rather
          than a truncated file. `Content-Length` is not consulted at all — a
          missing or lying one was the OOM vector this replaced.

        Raises `AuthRejected` (401/403), `RelayHTTPError` (any other unusable
        answer, a 3xx included), `ResponseTooLarge`, or the `OSError` family
        urllib raises for a network that did not answer.
        """
        cap = int(cap)
        credentialed = under_base(url, self.base_url)
        request = urllib.request.Request(url, method="GET")
        request.add_header("User-Agent", self.user_agent)  # B1, on every request
        request.add_header("Accept", "*/*")
        if credentialed:
            request.add_header("Authorization", f"Bearer {self.credentials.secret}")

        try:
            deadline = self.timeout if timeout is None else timeout
            with self._opener.open(request, timeout=deadline) as response:
                status = getattr(response, "status", 200)
                if 300 <= status < 400:  # pragma: no cover — the opener refuses
                    # first. Kept because "a 3xx is a failure" must not depend
                    # on which layer noticed: saving a redirect page's body as
                    # if it were the media is the outcome being refused.
                    raise RelayHTTPError(status, "", url,
                                         f"refusing a {status} redirect from a fetch")
                content_type = response.headers.get("Content-Type") or ""
                data = response.read(cap + 1)
        except urllib.error.HTTPError as exc:
            body = _drain(exc)
            if exc.code in (401, 403):
                raise AuthRejected(exc.code, body, url) from None
            raise RelayHTTPError(exc.code, body, url) from None

        if len(data) > cap:
            raise ResponseTooLarge(cap)
        return Fetched(data, content_type, credentialed)

    def open_stream(
        self,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        accept: str = "text/event-stream",
    ):
        """A response handed back **still open**, for a body read as it arrives.

        The one caller is the events channel, and it is here rather than there
        because the last time a streaming call site built its own request it
        rebuilt everything except the User-Agent — and the edge's bot-fight 403,
        which that call site classified as fatal, stopped the channel
        permanently on its first connect against a real gateway (B1). Sharing
        this method is what makes forgetting impossible: same UA, same bearer
        read live (C3), same `AuthRejected` class (C8), same refusal to carry
        this bearer across a redirect.

        `timeout` is the *socket* timeout, so it bounds each read of the open
        body and not the life of the stream — which is exactly what a channel
        watching for a black-holed TCP path wants.

        The caller owns the response and MUST close it — `close_stream` below
        is how, when the reader is parked in another thread.
        """
        url = self._url(path, params)
        request = urllib.request.Request(url, method="GET")
        for name, value in self._headers(accept).items():
            request.add_header(name, value)
        for name, value in (headers or {}).items():
            request.add_header(name, value)

        try:
            deadline = self.timeout if timeout is None else timeout
            return self._opener.open(request, timeout=deadline)
        except urllib.error.HTTPError as exc:
            body = _drain(exc)
            if exc.code in (401, 403):
                raise AuthRejected(exc.code, body, url) from None
            raise RelayHTTPError(exc.code, body, url) from None

    def _headers(self, accept: str = "application/json") -> Dict[str, str]:
        """The three headers every request to this gateway carries.

        One place, because each of them was once per-call-site: the explicit UA
        the edge requires (B1), and the bearer read through the credential on
        *every* request rather than captured once, so a rotation applied by auth
        recovery is on the next call with no restart and no object rebuilt (C3).
        """
        return {
            "User-Agent": self.user_agent,
            "Authorization": f"Bearer {self.credentials.secret}",
            "Accept": accept,
        }

    def _url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url = f"{url}?{urllib.parse.urlencode(query)}"
        return url


def close_stream(response) -> None:
    """End an open stream, including one another thread is parked in.

    `close()` alone is not enough: it drops this object's handle on the socket
    file, but a thread already blocked inside `readline()` stays blocked until
    the socket's own read timeout expires — which for the events channel is
    120 s, and which would make a `stop()` that "returns promptly" a promise
    this library could not keep. Shutting the socket down first makes the parked
    read return EOF at once.

    Every step is defensive on purpose. The attribute chain is CPython's private
    plumbing (`HTTPResponse.fp` is a buffered reader over a `SocketIO`), so if a
    future release renames it the channel degrades to waiting out its read
    timeout rather than raising in the middle of a shutdown.
    """
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:  # already gone; nothing to wake
            pass
    try:
        response.close()
    except Exception:  # noqa: BLE001 — closing must never raise in its turn
        pass


def _drain(exc: urllib.error.HTTPError) -> str:
    """The error's body, or nothing. Reading it must never raise in its turn."""
    try:
        return (exc.read() or b"").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 — a body we cannot read is a body we lack
        return ""


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are not followed while credentialed.

    Returning `None` here hands the response back to the default error handler,
    so the caller sees the 3xx as a `RelayHTTPError` instead of the bearer
    quietly making a second trip to a host the broker named.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Redacted rather than `netloc`, which carries userinfo when the
        # redirect target has any — and a redirect target is the one URL in this
        # module that this client did not provision (D3).
        log.warning("refusing a %s redirect to %s — this request is credentialed",
                    code, redact_url(newurl))
        return None


def _bind(connection_class, resolver: BoundedResolver):
    """A connection factory whose sockets come from `resolver`."""

    def factory(*args, **kwargs):
        connection = connection_class(*args, **kwargs)
        # Instance-level, so nothing outside this client's connections is
        # affected — the process-global alternative needed a host-substring
        # check to stay out of unrelated traffic, and a re-exec guard to avoid
        # recursing into itself.
        connection._create_connection = resolver.create_connection
        return connection

    return factory


class _ResolvingHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, resolver: BoundedResolver):
        super().__init__()
        self._resolver = resolver

    def http_open(self, req):
        return self.do_open(_bind(http.client.HTTPConnection, self._resolver), req)


class _ResolvingHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, resolver: BoundedResolver):
        super().__init__()
        self._resolver = resolver

    def https_open(self, req):
        # `_context` is urllib's own private attribute for the SSL context it
        # was constructed with; read defensively so this keeps working if a
        # future release renames it.
        return self.do_open(
            _bind(http.client.HTTPSConnection, self._resolver), req,
            context=getattr(self, "_context", None),
        )


def _opener_for(resolver: BoundedResolver) -> urllib.request.OpenerDirector:
    """An opener with exactly the handlers this client wants.

    Built by hand rather than through `build_opener`, so that nothing arrives by
    default — no proxy handler reading the environment, no cookie jar, and a
    redirect handler that refuses.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (
        _ResolvingHTTPHandler(resolver),
        _ResolvingHTTPSHandler(resolver),
        _RefuseRedirect(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
    ):
        opener.add_handler(handler)
    return opener
