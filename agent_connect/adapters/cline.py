"""Cline adapter — runs the Cline coding agent headlessly.

Cline CLI 2.0 (`npm i -g cline`, Node 22+) has a first-class headless mode:
`cline -y "<task>"` runs fully autonomously, streams the result to stdout, and
exits — the same one-shot shape as `codex exec`. `cwd` is the dir Cline operates
in (its local context). Auth is the tool's own: a provider API key in the env
(ANTHROPIC_API_KEY / OPENAI_API_KEY / …) or a Cline account (`cline` login);
with no credentials it fails fast with an auth message, which we surface.

`-y` grants full autonomy (auto-approves actions) — safe for an owner-tier,
allowFrom-restricted agent. For non-owner tiers, set CLINE_COMMAND_PERMISSIONS
to restrict the command set (follow-up; today's agents are owner-only).

  AGENT_CONNECT_CLINE_BIN   cline binary [default: cline]
"""
from __future__ import annotations

import os
import subprocess

BIN = os.environ.get("AGENT_CONNECT_CLINE_BIN", "cline")


def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    cmd = [BIN, "-y", task]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,  # non-TTY -> headless one-shot
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
        )
    except FileNotFoundError:
        return f"agent-connect: `{BIN}` not found — install Cline (`npm i -g cline`, Node 22+)."
    except subprocess.TimeoutExpired:
        return f"agent-connect: cline timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        return f"agent-connect: cline exited {proc.returncode}.\n{(proc.stderr or '')[-1000:]}"
    return out or "(cline produced no output)"
