"""Kilo Code adapter — runs Kilo's headless autonomous mode.

Kilo Code's CLI (MIT-licensed, built on the OpenCode core) has an autonomous
headless mode: `kilo run --auto "<task>"` runs unattended, auto-answers follow-up
prompts, and exits on completion or timeout — the same one-shot shape as
`codex exec`. `cwd` is the dir Kilo operates in (its local context). Auth is the
tool's own: BYOK (a provider API key) or the Kilo Gateway; with no credentials
it errors, which we surface.

(Kilo shares the OpenCode engine, which omnigent also wraps via its `opencode`
harness — so Kilo is reachable two ways; this direct adapter is the thin path.)

⚠️ UNVERIFIED output capture (2026-07-05): the command path runs (kilo 7.4.1
executes `run --auto`), but with no Kilo auth configured it produced EMPTY stdout
on both default and `--format json` — so the result-capture mechanism could not
be confirmed end-to-end. When Kilo auth is set up, verify that the answer lands
on stdout; if it does NOT, switch to `--format json` and parse the final
assistant event out of the NDJSON stream (that is the documented programmatic
path). Until then this adapter is best-effort scaffold, not verified-working.

  AGENT_CONNECT_KILO_BIN   kilo binary [default: kilo]
"""
from __future__ import annotations

import os
import subprocess

BIN = os.environ.get("AGENT_CONNECT_KILO_BIN", "kilo")


def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    cmd = [BIN, "run", "--auto", task]
    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,  # non-TTY -> autonomous one-shot
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
        )
    except FileNotFoundError:
        return f"agent-connect: `{BIN}` not found — install the Kilo CLI first."
    except subprocess.TimeoutExpired:
        return f"agent-connect: kilo timed out after {timeout}s."
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 and not out:
        return f"agent-connect: kilo exited {proc.returncode}.\n{(proc.stderr or '')[-1000:]}"
    return out or "(kilo produced no output)"
