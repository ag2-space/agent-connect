"""Tests for worker.parse_task against the AG2 Space relay task-file layout.

Regression for the read-only bug (2026-07-13): the relay writes `access_tier`
as the LAST header, after `task:`; the old parser stopped at `task:` and
swallowed everything after it into the body, so every task defaulted to
"other" → codex always ran `--sandbox read-only` even for the agent's owner.

Run: python3 test_worker_parse.py
"""
from agent_connect.worker import parse_task

SPARROW_LAYOUT = """id: task-123
timestamp: 2026-07-13T02:21:31Z
task: create a file called write-test.txt with content hi
source: ag2space
channel_id: !room:ag2.space
room_name: qingyun
sender_name: qingyun
user_id: @qingyun:ag2.space
priority: normal
interaction_type: message
access_tier: owner
"""

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


f = parse_task(SPARROW_LAYOUT)
check(f["access_tier"] == "owner", "access_tier AFTER task: is parsed (the live-bug case)")
check(f["task"] == "create a file called write-test.txt with content hi",
      "body carries no trailing-header junk")
check(f["source"] == "ag2space" and f["priority"] == "normal", "post-task headers parsed")

# legacy layout (tier before task) still works
f = parse_task("id: t1\naccess_tier: team\ntask: hello\n")
check(f["access_tier"] == "team" and f["task"] == "hello", "tier-before-task layout")

# multi-line body: continues until the next known header
f = parse_task("id: t2\ntask: line one\nline two\nnote: still body\naccess_tier: owner\n")
check(f["task"] == "line one\nline two\nnote: still body",
      "multi-line body keeps unknown-key lines")
check(f["access_tier"] == "owner", "header after multi-line body still parsed")

# forged/ambiguous double tier fails CLOSED
f = parse_task("id: t3\naccess_tier: owner\ntask: x\naccess_tier: owner\n")
check(f["access_tier"] == "other", "duplicate access_tier fails closed to other")

# no tier at all → default other
f = parse_task("id: t4\ntask: x\n")
check(f["access_tier"] == "other", "missing access_tier defaults to other")

print("\n" + ("PASS — parse_task green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
