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

Cline is model-agnostic — set the provider/model to run it on any backend
(e.g. Google Gemini): authenticate once with `cline auth google`, or pin per
agent via the env below. Auth (the API key) lives in Cline's own store, not here.

  AGENT_CONNECT_CLINE_BIN        cline binary [default: cline]
  AGENT_CONNECT_CLINE_PROVIDER   provider id, e.g. google (adds -P)
  AGENT_CONNECT_CLINE_MODEL      model id,   e.g. gemini-2.5-pro (adds -m)
"""
from __future__ import annotations

import json
import os
import re
import subprocess

BIN = os.environ.get("AGENT_CONNECT_CLINE_BIN", "cline")
PROVIDER = os.environ.get("AGENT_CONNECT_CLINE_PROVIDER", "").strip()
MODEL = os.environ.get("AGENT_CONNECT_CLINE_MODEL", "").strip()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _extract(stdout: str) -> str:
    """Pull the answer out of Cline's `--json` NDJSON stream. Cline (act mode)
    finishes with a `run_result` event whose text summarizes what it did — the
    cleanest thing to surface. Fall back to ANSI-stripped raw output."""
    result = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "run_result" and ev.get("text"):
            result = ev["text"]
    if result:
        # cline prefixes completion summaries with "Submission recorded (verified): "
        return re.sub(r"^Submission recorded \(verified\):\s*", "", result).strip()
    return _ANSI.sub("", stdout).strip()


def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    cmd = [BIN, "--json", "-y"]  # --json => parseable event stream
    if PROVIDER:
        cmd += ["-P", PROVIDER]
    if MODEL:
        cmd += ["-m", MODEL]
    cmd += [task]
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
    out = _extract(proc.stdout or "")
    if proc.returncode != 0 and not out:
        return f"agent-connect: cline exited {proc.returncode}.\n{(proc.stderr or '')[-1000:]}"
    return out or "(cline produced no output)"
