"""Codex adapter — runs the OpenAI Codex CLI (`codex exec`) on a task.

sandbox: 'workspace-write' (owner) or 'read-only' (everyone else).
`--skip-git-repo-check` is passed so non-git working dirs are allowed; codex
reads the prompt from argv and is fully non-interactive under `exec`.

Owner-tier tasks (workspace-write) also get NETWORK access inside the
sandbox: codex denies network by default even in workspace-write, which
dead-ends anything beyond local-file work (fetching a PR, reading a URL,
`git fetch`). The owner could run the same commands by hand on this machine,
so the tier boundary loses nothing — read-only (team/other) tasks stay fully
network-less. Live-caught on the first real E2E walk ("review PR #110" →
DNS blocked), 2026-07-13.
"""
from __future__ import annotations

import subprocess

# Map our two tiers to codex's sandbox modes.
SANDBOX = {"workspace-write": "workspace-write", "read-only": "read-only"}


def build_cmd(task: str, sandbox: str, cwd: str) -> list:
    mode = SANDBOX.get(sandbox, "read-only")
    cmd = ["codex", "exec", "--sandbox", mode]
    if mode == "workspace-write":
        cmd += ["-c", "sandbox_workspace_write.network_access=true"]
    cmd += ["--skip-git-repo-check", "--cd", cwd, task]
    return cmd


def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    cmd = build_cmd(task, sandbox, cwd)
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return "agent-connect: `codex` CLI not found on PATH — install + auth it first."
    except subprocess.TimeoutExpired:
        return f"agent-connect: codex timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0 and not out:
        return f"agent-connect: codex exited {proc.returncode}.\n{err[:1500]}"
    return out or err or "(codex produced no output)"
