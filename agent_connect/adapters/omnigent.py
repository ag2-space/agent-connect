"""Omnigent adapter — drives omnigent's meta-harness as a one-shot.

Omnigent (github.com/omnigent-ai/omnigent, Apache-2.0) is a meta-harness that
wraps many coding agents (claude / codex / cursor / kimi / qwen / goose / ...),
including ones with no clean headless mode of their own (native PTY wrappers).
This ONE adapter therefore unlocks omnigent's entire harness catalog for AG2
Space rooms — and, crucially, boxes the dependency: omnigent is alpha, so if it
churns or breaks, only this file is affected; the codex/ollama/gemini adapters
keep working.

Run model (verified 2026-07-05, omnigent 0.4.0): `omnigent run --harness <H>
[--model <M>] -p "<task>"` with stdin closed runs the prompt once, prints the
result to stdout, and exits (no TTY → one-shot, not an interactive REPL). `cwd`
is the working dir the harness operates in — for coding harnesses this is the
"local context" that makes the agent's output valuable (e.g. the room's vault).

  AGENT_CONNECT_OMNIGENT_HARNESS   harness name (default 'claude'; e.g. codex, kimi, cursor, qwen)
  AGENT_CONNECT_OMNIGENT_MODEL     optional model override (harness default if unset)
  AGENT_CONNECT_OMNIGENT_BIN       omnigent binary (default: 'omnigent' on PATH)
"""
from __future__ import annotations

import os
import re
import subprocess

BIN = os.environ.get("AGENT_CONNECT_OMNIGENT_BIN", "omnigent")
HARNESS = os.environ.get("AGENT_CONNECT_OMNIGENT_HARNESS", "claude")
MODEL = os.environ.get("AGENT_CONNECT_OMNIGENT_MODEL", "").strip()

# Per-message harness selection: a leading "[<harness>]" picks the harness for
# that message, overriding the env default — so ONE @omnigent agent can route to
# any harness ("[kimi] fix the bug", "[cursor] …"). No bracket → env default.
_HARNESS_PREFIX = re.compile(r"^\s*\[([a-zA-Z0-9_-]+)\]\s*(.*)$", re.S)


def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    harness = HARNESS
    m = _HARNESS_PREFIX.match(task)
    if m:
        harness, task = m.group(1), m.group(2)
    cmd = [BIN, "run", "--harness", harness]
    if MODEL:
        cmd += ["--model", MODEL]
    cmd += ["-p", task]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,  # no TTY -> omnigent runs one-shot and exits
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
        )
    except FileNotFoundError:
        return (
            f"agent-connect: `{BIN}` not found — install omnigent first "
            "(`uv tool install omnigent`)."
        )
    except subprocess.TimeoutExpired:
        return f"agent-connect: omnigent ({harness}) timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        return (
            f"agent-connect: omnigent ({harness}) exited {proc.returncode}.\n"
            f"{(proc.stderr or '')[-1000:]}"
        )
    return out or "(omnigent produced no output)"
