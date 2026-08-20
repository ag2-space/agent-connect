"""Name resolution with a hard wall-clock bound, single-flight, IPv4 first.

`getaddrinfo` cannot be interrupted and has no timeout of its own: it blocks
until the system resolver answers or the OS gives up, which on a captive portal
or a link dropped mid-query can be minutes or never. A socket timeout does not
cover it — it covers connect and read. Unbounded, one wedged resolve wedges the
whole poll loop, silently: the observed shape is a client stuck for 21 hours
with the UI showing "reconnecting" and nothing to diagnose from. Bounding it
turns the wedge into an ordinary `gaierror`, which urllib surfaces as a
`URLError` and the reconnect path already handles — so the connection heals by
itself the moment DNS recovers.

The bound needs a helper thread, because the call cannot be interrupted. That
would leak a thread per retry against a resolver that never answers, so resolves
are **single-flight**: while one call for a key is outstanding, every other
caller attaches to it. A permanently hung resolver therefore pins exactly one
thread, no matter how long the loop retries.

IPv4 is preferred with IPv6 kept as fallback. Some networks black-hole v6 — the
SYN is dropped, not refused — and `getaddrinfo` returns v6 first, so every fresh
connection paid a full TCP connect timeout (~26 s observed) before falling back
to v4 in under a second. That cost landed on every message in both directions.

All of it is **scoped to this object**, deliberately. The same behaviour has
lived as a monkeypatch on `socket.getaddrinfo`, which needed a host-substring
check to avoid inflicting itself on unrelated traffic and a re-exec guard to
avoid recursing into itself on module reload. A resolver a connection is handed
needs neither.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, List, Optional

log = logging.getLogger(__name__)

#: `socket.create_connection`'s "no explicit timeout" sentinel, which urllib
#: passes down verbatim. Read through `getattr` because it is a private name:
#: this code has to recognise the value, not depend on the attribute existing.
_NO_TIMEOUT = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT", None)

#: Long enough that a slow-but-working resolver is not cut off, short enough
#: that a wedged one becomes a retry rather than an outage.
DEFAULT_DNS_TIMEOUT_S = 8.0


class _Inflight:
    """One outstanding resolve; every waiter shares its event and its outcome."""

    __slots__ = ("done", "result", "error")

    def __init__(self):
        self.done = threading.Event()
        self.result: List = []
        self.error: Optional[BaseException] = None


class BoundedResolver:
    """Resolution and connection for one client's traffic.

    Hand it to whatever opens sockets; nothing global changes.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_DNS_TIMEOUT_S,
        prefer_ipv4: bool = True,
        getaddrinfo: Callable[..., list] = socket.getaddrinfo,
    ):
        self.timeout = float(timeout)
        self.prefer_ipv4 = bool(prefer_ipv4)
        self._resolve = getaddrinfo
        self._inflight = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        return f"<BoundedResolver timeout={self.timeout}s prefer_ipv4={self.prefer_ipv4}>"

    def getaddrinfo(self, host, port, family=0, type=0, proto=0, flags=0):
        """`socket.getaddrinfo`, bounded, single-flight, v4 first.

        Raises `socket.gaierror` when the bound is exceeded — the same error
        class a real resolution failure raises, so callers need no new branch.
        """
        args = (host, port, family, type, proto, flags)
        infos = self._bounded(args)
        if not self.prefer_ipv4:
            return infos
        v4 = [info for info in infos if info[0] == socket.AF_INET]
        # No v4 record is not a failure: a genuinely v6-only destination keeps
        # whatever the resolver said.
        return v4 or infos

    def _bounded(self, args):
        if self.timeout <= 0:
            return self._resolve(*args)

        with self._lock:
            call = self._inflight.get(args)
            if call is None:
                call = _Inflight()
                self._inflight[args] = call
                worker = threading.Thread(
                    target=self._run, args=(args, call),
                    name="ag2-relay-dns", daemon=True,
                )
                try:
                    worker.start()
                except Exception as exc:  # noqa: BLE001 — see below
                    # A start that fails means `_run` never runs, so its
                    # `finally` never clears the slot and nothing else ever
                    # will: every later resolve for this key would attach to a
                    # call that will not happen, wait out the full bound and
                    # fail, with the underlying resolver asked zero times —
                    # forever, long after threads recover. That is a permanent
                    # version of the 21-hour wedge A1 and A2 exist to prevent,
                    # so the slot goes back before the error goes up.
                    self._inflight.pop(args, None)
                    # And it goes up as a `gaierror`: `start()` raises
                    # `RuntimeError` ("can't create new thread", and at
                    # interpreter shutdown on 3.12+), which is not an `OSError`,
                    # so urllib would not wrap it as a `URLError` and the
                    # reconnect path would not recognise it. A1 requires a
                    # resolve failure to surface as an ordinary retryable
                    # network error.
                    raise socket.gaierror(
                        f"DNS resolution for {args[0]!r} could not start its "
                        f"resolver thread: {exc}"
                    ) from exc

        if not call.done.wait(self.timeout):
            raise socket.gaierror(
                f"DNS resolution for {args[0]!r} exceeded {self.timeout}s "
                "(resolver hung)"
            )
        if call.error is not None:
            raise call.error
        return call.result

    def _run(self, args, call):
        try:
            call.result = self._resolve(*args)
        except BaseException as exc:  # noqa: BLE001 — re-raised to every waiter
            call.error = exc
        finally:
            # Clear the slot BEFORE signalling: a waiter woken by the event must
            # never be able to attach to a call that has already finished.
            with self._lock:
                self._inflight.pop(args, None)
            call.done.set()

    def create_connection(self, address, timeout=_NO_TIMEOUT, source_address=None):
        """`socket.create_connection` over this resolver.

        Same shape as the stdlib's, including its "try each address, raise the
        last error" behaviour — the difference is only in which addresses are
        offered, and in the fact that resolving them is bounded.
        """
        host, port = address[0], address[1]
        last_error: Optional[BaseException] = None
        for family, socktype, proto, _canonname, sockaddr in self.getaddrinfo(
            host, port, 0, socket.SOCK_STREAM
        ):
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                if timeout is not _NO_TIMEOUT and timeout is not None:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()
        if last_error is not None:
            raise last_error
        raise OSError(f"no addresses to connect to for {host!r}")
