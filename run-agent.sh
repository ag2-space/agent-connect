#!/usr/bin/env bash
# Launch your local agent as an AG2 Space agent: the AG2 relay client (pulls your
# agent's tasks + posts results) + the agent-connect worker (runs the agent).
#
#   AGENT_CONNECT_TOKEN    your agent's relay token (from the Agent Portal) [required]
#   AGENT_CONNECT_ADAPTER  adapter, e.g. codex [required]
#   AGENT_CONNECT_REPO     repo the agent works in [default: cwd]
#   AGENT_CONNECT_WORKSPACE  task/result workspace [default: ~/.agent-connect/workspace]
#   RELAY_BIN              ag2-sparrow console script [default: `command -v ag2-sparrow`;
#                          `pip install ag2-sparrow` if you don't have it]
#   RELAY_CLIENT           legacy: path to a file-based relay client — only used
#                          when explicitly set (pre-PyPI installs)
set -euo pipefail
: "${AGENT_CONNECT_TOKEN:?set AGENT_CONNECT_TOKEN (from the Agent Portal)}"
: "${AGENT_CONNECT_ADAPTER:?set AGENT_CONNECT_ADAPTER (e.g. codex)}"
export AGENT_CONNECT_WORKSPACE="${AGENT_CONNECT_WORKSPACE:-$HOME/.agent-connect/workspace}"
mkdir -p "$AGENT_CONNECT_WORKSPACE/tasks" "$AGENT_CONNECT_WORKSPACE/results" \
         "$AGENT_CONNECT_WORKSPACE/state"

# Kill a prior instance for THIS workspace before starting a new one. Workers all
# share argv ("python3 -m agent_connect"), so a relaunch can't pkill by name
# without hitting sibling agents — a pidfile keyed to the workspace is the only
# safe way. Without this, each relaunch stacks an orphan worker on the same
# workspace (double-processing + stale config, e.g. a model swap left running).
PIDFILE="$AGENT_CONNECT_WORKSPACE/.worker.pids"
if [ -f "$PIDFILE" ]; then
  while read -r _oldpid; do
    [ -n "$_oldpid" ] && kill "$_oldpid" 2>/dev/null || true
  done < "$PIDFILE"
  rm -f "$PIDFILE"
fi

# 1) relay client: pulls THIS agent's tasks into the workspace + posts results back.
#    (It is transport-only; identifies the agent by AGENT_CONNECT_TOKEN.)
#    Canonical path: the ag2-sparrow package (PyPI), wired to this workspace via
#    its dir-interface env vars. Legacy path: an explicitly-set RELAY_CLIENT file
#    (pre-PyPI sparse-fetch installs) keeps its old launch env.
RELAY_BIN="${RELAY_BIN:-$(command -v ag2-sparrow || true)}"
if [ -n "${RELAY_CLIENT:-}" ] && [ -f "$RELAY_CLIENT" ]; then
  REMOTE_TASK_TOKEN="$AGENT_CONNECT_TOKEN" \
  REMOTE_TASK_URL="${REMOTE_TASK_URL:-https://chat.ag2.space/relay}" \
  SUTANDO_WORKSPACE="$AGENT_CONNECT_WORKSPACE" \
  python3 "$RELAY_CLIENT" &
  RELAY_PID=$!
  echo "$RELAY_PID" > "$PIDFILE"
  trap 'kill $RELAY_PID 2>/dev/null || true' EXIT
elif [ -n "$RELAY_BIN" ] && [ -x "$RELAY_BIN" ]; then
  AGENT_CONNECT_TASK_DIR="$AGENT_CONNECT_WORKSPACE/tasks" \
  AGENT_CONNECT_RESULT_DIR="$AGENT_CONNECT_WORKSPACE/results" \
  AGENT_CONNECT_STATE_DIR="$AGENT_CONNECT_WORKSPACE/state" \
  REMOTE_TASK_TOKEN="$AGENT_CONNECT_TOKEN" \
  REMOTE_TASK_URL="${REMOTE_TASK_URL:-https://chat.ag2.space/relay}" \
  "$RELAY_BIN" &
  RELAY_PID=$!
  echo "$RELAY_PID" > "$PIDFILE"
  trap 'kill $RELAY_PID 2>/dev/null || true' EXIT
else
  echo "note: ag2-sparrow not found — 'pip install ag2-sparrow' (or set RELAY_BIN), or start the relay yourself." >&2
fi

# 2) worker: turns each pulled task into an agent run. Record this shell's PID
# ($$ survives the exec — the worker keeps the same PID) so the next relaunch
# kills it via the pidfile above.
echo "$$" >> "$PIDFILE"
exec python3 -m agent_connect
