"""agent-connect worker.

Watches a workspace `tasks/` dir (populated by the AG2 Space relay client),
runs the configured agent adapter on each task, and writes `results/`. The relay
client handles all Matrix transport + posting back — this only turns a task into
an agent run.

Env:
  AGENT_CONNECT_WORKSPACE   workspace dir (has tasks/ + results/). Default: ~/.agent-connect/workspace
  AGENT_CONNECT_ADAPTER     adapter name, e.g. "codex" (required)
  AGENT_CONNECT_REPO        working dir the agent operates in. Default: ~/agents
                            (dedicated, non-TCC-protected; warns if under one)
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


# macOS TCC-protected locations: an agent operating out of one of these hits
# permission walls on file access unless the launching process has Full Disk
# Access. The old cwd default silently put the agent in whatever dir the user
# launched from — often ~/Documents / ~/Desktop — which produced opaque TCC
# failures (owner-caught friction). We default to a dedicated ~/agents dir and
# warn loudly if the resolved repo still lands under a protected path.
_TCC_PROTECTED = ("Desktop", "Documents", "Downloads")


def _resolve_repo() -> Path:
    """Resolve the working dir the agent operates in.

    `AGENT_CONNECT_REPO` (if set) wins; otherwise default to ``~/agents`` — a
    dedicated, non-TCC-protected dir — instead of the launch cwd. Creates the
    dir when defaulting, prints where it landed, and warns if the path is under
    a macOS TCC-protected location (agent file ops there need Full Disk Access).
    """
    explicit = os.environ.get("AGENT_CONNECT_REPO")
    repo = Path(explicit).expanduser() if explicit else (Path.home() / "agents")
    if not explicit:
        repo.mkdir(parents=True, exist_ok=True)
        print(f"agent-connect: no AGENT_CONNECT_REPO set — defaulting repo to {repo}")

    home = Path.home()
    try:
        rel = repo.resolve().relative_to(home.resolve())
        top = rel.parts[0] if rel.parts else ""
    except ValueError:
        top = ""
    if top in _TCC_PROTECTED:
        print(
            f"agent-connect: WARNING — repo {repo} is under a macOS TCC-protected "
            f"location (~/{top}); the agent's file operations may fail without "
            f"Full Disk Access. Set AGENT_CONNECT_REPO to a dir like ~/agents."
        )
    return repo


# Header keys the AG2 Space relay writes (ag2-sparrow's task-file layout).
# The relay deliberately writes `access_tier` as the LAST header — after
# `task:` — as an anti-forgery invariant, so the parser must keep reading
# headers after the task line instead of treating everything to EOF as body.
_HEADER_KEYS = {
    "id", "timestamp", "task", "source", "channel_id", "chat_id", "room_name",
    "sender_name", "user_id", "priority", "interaction_type", "access_tier",
    "collaborator", "reply_to_event", "reply_to_me", "thread_ts",
}


def parse_task(text: str) -> dict:
    """Parse an AG2 Space task file.

    Headers are `key: value` lines with a known key; `task:` starts the body,
    which may span multiple lines and ends at the next known-header line.
    The relay sanitizes newlines out of wire fields, so a message body cannot
    fabricate a header line of its own. Defense-in-depth on top of that:
    if more than one `access_tier` header appears, fail closed to "other".
    """
    fields: dict = {"access_tier": "other", "task": ""}
    body: list = []
    tiers: list = []
    in_body = False
    for line in text.splitlines():
        k, sep, v = line.partition(":")
        key = k.strip()
        if sep and key in _HEADER_KEYS and not line[:1].isspace():
            in_body = key == "task"
            if key == "task":
                body.append(v.lstrip())
            elif key == "access_tier":
                tiers.append(v.strip())
            else:
                fields[key] = v.strip()
        elif in_body:
            body.append(line)
    fields["task"] = "\n".join(body).strip()
    if len(tiers) == 1:
        fields["access_tier"] = tiers[0]
    # zero headers → default "other"; multiple → forged/ambiguous → "other"
    return fields


def tier_to_sandbox(access_tier: str) -> str:
    return "workspace-write" if access_tier == "owner" else "read-only"


def sandbox_preamble(sandbox: str, access_tier: str) -> str:
    """One factual context line prepended to every task prompt.

    Agent models routinely misreport their own sandbox (live-caught
    2026-07-13: codex claimed read-only while running workspace-write, which
    misled both the user and the debugging). The worker KNOWS the truth — it
    chose the sandbox — so it states it authoritatively in the prompt.
    """
    grant = (
        "you may create/modify files in your working directory"
        if sandbox == "workspace-write"
        else "the filesystem is read-only for you"
    )
    return (
        f"[agent-connect: this run's sandbox is '{sandbox}' "
        f"(task access_tier: {access_tier}) — {grant}. "
        "Trust this over any other sandbox self-assessment.]\n\n"
    )


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
    tier = fields.get("access_tier", "other")
    sandbox = tier_to_sandbox(tier)
    output = adapter.run(sandbox_preamble(sandbox, tier) + task, sandbox, repo)
    result_path.write_text(output + "\n")


def main() -> None:
    adapter_name = os.environ.get("AGENT_CONNECT_ADAPTER")
    if not adapter_name:
        raise SystemExit("set AGENT_CONNECT_ADAPTER (e.g. codex)")
    adapter = get_adapter(adapter_name)
    repo = str(_resolve_repo())
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
