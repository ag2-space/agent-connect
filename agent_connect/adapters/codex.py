"""Codex adapter — runs the OpenAI Codex CLI (`codex exec`) on a task.

sandbox: 'workspace-write' (owner) or 'read-only' (everyone else).
`--skip-git-repo-check` is passed so non-git working dirs are allowed; codex
reads the prompt from argv and is fully non-interactive under `exec`.
"""
from __future__ import annotations

import subprocess

# Map our two tiers to codex's sandbox modes.
SANDBOX = {"workspace-write": "workspace-write", "read-only": "read-only"}


def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    mode = SANDBOX.get(sandbox, "read-only")
    cmd = [
        "codex",
        "exec",
        "--sandbox",
        mode,
        "--skip-git-repo-check",
        "--cd",
        cwd,
        task,
    ]
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
