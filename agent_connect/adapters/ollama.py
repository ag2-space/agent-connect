"""Ollama adapter — runs a local Ollama model via its HTTP API.

Fully local + private: the model runs on the user's own machine (`ollama serve`),
so nothing leaves the box — this is the "you own even the model" story. `sandbox`
and `cwd` are unused here: a chat model has no filesystem side effects, it just
answers the prompt. Host + model are env-configurable so the SAME adapter serves
any local model you've pulled.

  OLLAMA_HOST                  ollama server URL   [default http://localhost:11434]
  AGENT_CONNECT_OLLAMA_MODEL   model tag           [default qwen2.5:3b]
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("AGENT_CONNECT_OLLAMA_MODEL", "qwen2.5:3b")


def run(task: str, sandbox: str, cwd: str, timeout: int = 600) -> str:
    body = json.dumps({"model": MODEL, "prompt": task, "stream": False}).encode()
    req = urllib.request.Request(
        HOST + "/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        return (
            f"agent-connect: could not reach Ollama at {HOST} ({e}). "
            "Is `ollama serve` running and the model pulled?"
        )
    except Exception as e:  # noqa: BLE001
        return f"agent-connect: ollama error: {e}"
    out = (data.get("response") or "").strip()
    return out or "(ollama produced no output)"
