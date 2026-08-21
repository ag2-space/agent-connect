"""Name resolution: bounded, single-flight, IPv4-first, and nobody else's
business (A1-A4).

`getaddrinfo` has no native timeout — it blocks until the resolver answers or
the OS gives up, which on a captive portal or a link dropped mid-query is
"never". urllib's socket timeout covers connect and read but not resolution, so
an unbounded resolve wedges the poll loop with no status write and no
self-recovery: observed once as a client stuck for 21 hours while the UI showed
"reconnecting". Bounding it turns that into an ordinary retryable network error,
which the reconnect path already knows how to survive.

The v4 preference is the other half of the same afternoon: the relay host
publishes AAAA records, some networks black-hole IPv6 (the SYN is dropped, not
refused), and `getaddrinfo` returns v6 first — ~26 s of dead TCP connect on
every fresh connection, added to every message in both directions.

Run: python3 tests/test_resolver.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path
import socket
import threading
import time
import types

from ag2_relay_client import resolver as resolver_module
from ag2_relay_client.resolver import BoundedResolver

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", 443, 0, 0))
V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.16.0.1", 443))


# --- A1: a hung resolver becomes a retryable error, on the clock
class Hung:
    """A resolver that never answers, and counts how many times it was asked."""

    def __init__(self):
        self.calls = 0
        self.release = threading.Event()
        self.entered = threading.Event()

    def __call__(self, host, port, *args, **kwargs):
        self.calls += 1
        self.entered.set()
        self.release.wait(30)
        return [V4]


hung = Hung()
resolver = BoundedResolver(timeout=0.2, getaddrinfo=hung)
started = time.monotonic()
raised = None
try:
    resolver.getaddrinfo("gw.example", 443)
except Exception as exc:  # noqa: BLE001 — the type is the assertion
    raised = exc
elapsed = time.monotonic() - started
check(isinstance(raised, socket.gaierror),
      "a resolve overrun raises gaierror — the ordinary retryable network error")
check(elapsed < 5, f"the caller is released on the bound, not on the resolver ({elapsed:.2f}s)")
check("gw.example" in str(raised), "the error names the host that would not resolve")

# --- A2: retries against a hung resolver attach, they do not accumulate threads
before = threading.active_count()
for _ in range(6):
    try:
        resolver.getaddrinfo("gw.example", 443)
    except socket.gaierror:
        pass
check(hung.calls == 1, "six retries against one hung resolve made ONE underlying call")
check(threading.active_count() - before <= 1,
      "and spawned no further resolver threads")

hung.release.set()
time.sleep(0.2)  # let the outstanding call finish and clear its slot
check(resolver.getaddrinfo("gw.example", 443) == [V4],
      "once the resolver recovers, the next call answers normally")
check(hung.calls == 2, "the completed call cleared its slot — the retry did not attach to it")

# Concurrent askers share one call rather than each starting their own.
concurrent = Hung()
shared = BoundedResolver(timeout=0.3, getaddrinfo=concurrent)
errors = []


def ask():
    try:
        shared.getaddrinfo("gw.example", 443)
    except socket.gaierror as exc:
        errors.append(exc)


askers = [threading.Thread(target=ask) for _ in range(5)]
for t in askers:
    t.start()
for t in askers:
    t.join(10)
check(concurrent.calls == 1, "five concurrent callers made ONE underlying resolve")
check(len(errors) == 5, "and all five were released by the bound")
concurrent.release.set()

# --- A1/A2: a helper thread that will not START must not poison the slot.
# The single-flight slot is claimed before the thread runs, and only the
# thread's own `finally` clears it — so a `start()` that raises (thread or
# memory pressure, or "can't create new thread at interpreter shutdown" on
# 3.12+) leaves a slot nothing will ever clear. Every later resolve for that key
# would then wait out the whole bound and fail with the underlying resolver
# asked zero times, forever, long after threads recovered: a permanent version
# of the 21-hour wedge.
class RefusingThread:
    """A thread that cannot be started, the way a loaded machine has one."""

    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        raise RuntimeError("can't start new thread")


answers = []


def answering(host, port, *args, **kwargs):
    answers.append(host)
    return [V4]


pressured = BoundedResolver(timeout=5, getaddrinfo=answering)
saved_threading = resolver_module.threading
resolver_module.threading = types.SimpleNamespace(
    Thread=RefusingThread, Event=threading.Event, Lock=threading.Lock)
started = time.monotonic()
refused_start = None
try:
    pressured.getaddrinfo("gw.example", 443)
except Exception as exc:  # noqa: BLE001 — the type is the assertion
    refused_start = exc
finally:
    resolver_module.threading = saved_threading
check(isinstance(refused_start, OSError),
      "a resolver thread that will not start raises an OSError — urllib wraps "
      "those as URLError, and the reconnect path only knows those")
check(isinstance(refused_start, socket.gaierror),
      "and specifically a gaierror, the same class a resolution failure raises")
check(time.monotonic() - started < 1,
      "it fails at once rather than waiting out a bound nothing will end")
check(answers == [], "the underlying resolver was never reached, as expected")
check(pressured.getaddrinfo("gw.example", 443) == [V4],
      "and the NEXT call — threads recovered — resolves normally")
check(answers == ["gw.example"],
      "the failed start released its slot instead of poisoning the key forever")

# --- A3: v4 preferred, v6 kept as the fallback it is
def both(host, port, *args, **kwargs):
    return [V6, V4]


def only_v6(host, port, *args, **kwargs):
    return [V6]


check(BoundedResolver(getaddrinfo=both).getaddrinfo("gw.example", 443) == [V4],
      "with both records, only the v4 address is offered")
check(BoundedResolver(getaddrinfo=only_v6).getaddrinfo("gw.example", 443) == [V6],
      "a genuinely v6-only destination still resolves")
check(BoundedResolver(getaddrinfo=both, prefer_ipv4=False).getaddrinfo("gw.example", 443)
      == [V6, V4], "the preference is opt-out, for hosts with working v6")

# --- A4: scoped to this client, not bolted onto the interpreter
original = socket.getaddrinfo
asked = []


def counting(host, port, *args, **kwargs):
    asked.append(host)
    return socket.getaddrinfo(host, port, *args, **kwargs)


r = BoundedResolver(getaddrinfo=counting)
r.getaddrinfo("localhost", 80)
check(asked == ["localhost"],
      "this object resolves through the resolver it was handed, not the module's")
check(socket.getaddrinfo is original,
      "socket.getaddrinfo is left alone — the bound is per-connection, not process-global")

# A bound of zero is the escape hatch for someone who wants the OS behaviour.
check(BoundedResolver(timeout=0, getaddrinfo=both).getaddrinfo("gw.example", 443) == [V4],
      "timeout=0 resolves inline, without the bounding thread")

# --- the connection path actually connects
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
port = listener.getsockname()[1]
sock = r.create_connection(("127.0.0.1", port), timeout=5)
check(sock is not None, "create_connection reaches a listening socket")
check(asked[-1] == "127.0.0.1",
      "and it opened it through THIS object's bounded resolver — the whole point "
      "of handing one in rather than patching the module")
sock.close()
listener.close()

refused = None
try:
    BoundedResolver().create_connection(("127.0.0.1", port), timeout=1)
except OSError as exc:
    refused = exc
check(refused is not None, "a refused connection raises OSError, as the caller expects")

print("\n" + ("PASS — resolver green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
