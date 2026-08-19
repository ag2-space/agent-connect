"""Poll-error backoff: 1 s doubling to a 60 s cap, reset by one healthy
round-trip (D1).

Both halves are the requirement. Without the ceiling a client hammers a broker
that is already down; without the reset-on-health it stays slow long after the
broker came back. The cap equals the broker's default lease visibility timeout,
which is deliberate and documented rather than engineered away: a client at max
backoff can lose leases and see re-serves, and redelivery is absorbed by the
dedup journal — an unreachable broker cannot have its leases extended at any cap.

Run: python3 tests/test_backoff.py
"""
import _bootstrap  # noqa: F401 — distribution root on sys.path

from ag2_relay_client.backoff import Backoff

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


b = Backoff()
check(b.seconds == 0.0, "a fresh client is not backing off")

delays = [b.after_error() for _ in range(9)]
check(delays[:6] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0], "the first errors double from 1 s")
check(delays[6:] == [60.0, 60.0, 60.0], "the delay caps at 60 s and stays there")
check(b.seconds == 60.0, "the current delay is readable for the status file")

b.after_success()
check(b.seconds == 0.0, "one healthy round-trip clears the backoff")
check(b.after_error() == 1.0, "and the next error starts again from 1 s")

# A single success mid-climb resets — the requirement is one healthy round-trip,
# not a run of them.
b = Backoff()
b.after_error()
b.after_error()
b.after_success()
check(b.after_error() == 1.0, "a success mid-climb resets the whole climb")

custom = Backoff(start=0.5, cap=2.0)
check([custom.after_error() for _ in range(4)] == [0.5, 1.0, 2.0, 2.0],
      "start and cap are configurable, for tests that cannot wait a minute")

print("\n" + ("PASS — backoff green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
