"""agent-connect worker.

Watches a workspace `tasks/` dir (populated by the AG2 Space relay client),
runs the configured agent adapter on each task, and writes `results/`. The relay
client handles all Matrix transport + posting back — this only turns a task into
an agent run.

Env:
  AGENT_CONNECT_WORKSPACE   workspace dir (has tasks/ + results/). Default: ~/.agent-connect/workspace
  AGENT_CONNECT_ADAPTER     adapter name, e.g. "codex" (required)
  AGENT_CONNECT_REPO        working dir the agent operates in. Default: cwd
  AGENT_CONNECT_POLL        seconds between scans (default 1.0)

Task files are the AG2 Space convention: `tasks/task-<id>.txt` with `id:`,
`task:` and `access_tier:` lines. Results go to `results/task-<id>.txt`.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .adapters import get as get_adapter


def _ws() -> Path:
    return Path(
        os.environ.get("AGENT_CONNECT_WORKSPACE")
        or (Path.home() / ".agent-connect" / "workspace")
    ).expanduser()


def parse_task(text: str) -> dict:
    """Parse an AG2 Space task file. `task:` may span the rest of the file."""
    fields: dict = {"access_tier": "other", "task": ""}
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("task:"):
            # task body is the rest of the file from here (may be multi-line).
            body = [line[len("task:") :].lstrip()]
            body.extend(lines[i + 1 :])
            fields["task"] = "\n".join(body).strip()
            break
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields


def tier_to_sandbox(access_tier: str) -> str:
    return "workspace-write" if access_tier == "owner" else "read-only"


def process_one(task_path: Path, adapter, repo: str, results_dir: Path) -> None:
    task_id = task_path.stem  # "task-<id>"
    result_path = results_dir / f"{task_id}.txt"
    if result_path.exists():
        return
    fields = parse_task(task_path.read_text(errors="replace"))
    task = fields.get("task", "").strip()
    if not task:
        result_path.write_text("[no-send] empty task\n")
        return
    sandbox = tier_to_sandbox(fields.get("access_tier", "other"))
    output = adapter.run(task, sandbox, repo)
    result_path.write_text(output + "\n")


def main() -> None:
    adapter_name = os.environ.get("AGENT_CONNECT_ADAPTER")
    if not adapter_name:
        raise SystemExit("set AGENT_CONNECT_ADAPTER (e.g. codex)")
    adapter = get_adapter(adapter_name)
    repo = os.environ.get("AGENT_CONNECT_REPO") or os.getcwd()
    poll = float(os.environ.get("AGENT_CONNECT_POLL", "1.0"))

    ws = _ws()
    tasks_dir = ws / "tasks"
    results_dir = ws / "results"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    print(f"agent-connect worker: adapter={adapter_name} repo={repo} ws={ws}")
    seen: set = set()
    while True:
        for task_path in sorted(tasks_dir.glob("task-*.txt")):
            if task_path.name in seen:
                continue
            try:
                process_one(task_path, adapter, repo, results_dir)
            except Exception as e:  # noqa: BLE001 — never die on one bad task
                (results_dir / f"{task_path.stem}.txt").write_text(
                    f"agent-connect: worker error: {e}\n"
                )
            seen.add(task_path.name)
        time.sleep(poll)


if __name__ == "__main__":
    main()
