"""Tests for the sandbox preamble + its wiring through process_one.

Run: python3 test_worker_preamble.py
"""
import tempfile
from pathlib import Path

from agent_connect.worker import process_one, sandbox_preamble

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


p = sandbox_preamble("workspace-write", "owner")
check("'workspace-write'" in p and "access_tier: owner" in p, "owner preamble states sandbox + tier")
check("create/modify files" in p, "owner preamble grants writes")
p2 = sandbox_preamble("read-only", "team")
check("read-only for you" in p2, "read-only preamble denies writes")


class StubAdapter:
    def __init__(self):
        self.calls = []

    def run(self, task, sandbox, cwd):
        self.calls.append((task, sandbox, cwd))
        return "stub-output"


tmp = Path(tempfile.mkdtemp())
tasks, results = tmp / "tasks", tmp / "results"
tasks.mkdir(); results.mkdir()
tf = tasks / "task-p1.txt"
tf.write_text("id: task-p1\ntask: do the thing\nsource: ag2space\naccess_tier: owner\n")
stub = StubAdapter()
process_one(tf, stub, "/repo", results)
sent_task, sent_sandbox, _ = stub.calls[0]
check(sent_sandbox == "workspace-write", "owner task → workspace-write sandbox arg")
check(sent_task.startswith("[agent-connect: this run's sandbox is 'workspace-write'"),
      "prompt starts with the authoritative preamble")
check(sent_task.endswith("do the thing"), "original task body preserved after preamble")
check((results / "task-p1.txt").read_text() == "stub-output\n", "result written")

print("\n" + ("PASS — preamble green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
