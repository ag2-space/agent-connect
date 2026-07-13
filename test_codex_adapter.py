"""Tests for the codex adapter's command construction.

Run: python3 test_codex_adapter.py
"""
from agent_connect.adapters.codex import build_cmd

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


owner = build_cmd("do x", "workspace-write", "/repo")
check(owner[:4] == ["codex", "exec", "--sandbox", "workspace-write"], "owner → workspace-write sandbox")
check("-c" in owner and "sandbox_workspace_write.network_access=true" in owner,
      "owner tier gets network access")
check(owner[-3:] == ["--cd", "/repo", "do x"], "cwd + task positioned last")

ro = build_cmd("do x", "read-only", "/repo")
check("--sandbox" in ro and ro[ro.index("--sandbox") + 1] == "read-only", "non-owner → read-only")
check("sandbox_workspace_write.network_access=true" not in ro,
      "read-only tier stays network-less")

unk = build_cmd("do x", "bogus-mode", "/repo")
check(unk[unk.index("--sandbox") + 1] == "read-only", "unknown sandbox falls back to read-only")
check("network_access" not in " ".join(unk), "fallback gets no network either")

print("\n" + ("PASS — codex adapter green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
