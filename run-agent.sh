#!/usr/bin/env bash
# Launch your local agent as an AG2 Space agent: the AG2 relay client (pulls your
# agent's tasks + posts results) + the agent-connect worker (runs the agent).
#
#   AGENT_CONNECT_TOKEN    your agent's relay token (from the Agent Portal) [required]
#   AGENT_CONNECT_ADAPTER  adapter, e.g. codex [required]
#   AGENT_CONNECT_REPO     repo the agent works in [default: cwd]
#   AGENT_CONNECT_WORKSPACE  task/result workspace [default: ~/.agent-connect/workspace]
#   RELAY_CLIENT           path to the AG2 relay client (remote-relay-bridge.py)
#                          [default: try ~/.sutando-relay-client/remote-relay-bridge.py]
set -euo pipefail
: "${AGENT_CONNECT_TOKEN:?set AGENT_CONNECT_TOKEN (from the Agent Portal)}"
: "${AGENT_CONNECT_ADAPTER:?set AGENT_CONNECT_ADAPTER (e.g. codex)}"
export AGENT_CONNECT_WORKSPACE="${AGENT_CONNECT_WORKSPACE:-$HOME/.agent-connect/workspace}"
mkdir -p "$AGENT_CONNECT_WORKSPACE/tasks" "$AGENT_CONNECT_WORKSPACE/results"

RELAY_CLIENT="${RELAY_CLIENT:-$HOME/.sutando-relay-client/remote-relay-bridge.py}"
here="$(cd "$(dirname "$0")" && pwd)"

# 1) relay client: pulls THIS agent's tasks into the workspace + posts results back.
#    (It is transport-only; identifies the agent by AGENT_CONNECT_TOKEN.)
if [ -f "$RELAY_CLIENT" ]; then
  REMOTE_TASK_TOKEN="$AGENT_CONNECT_TOKEN" \
  REMOTE_TASK_URL="${REMOTE_TASK_URL:-https://chat.ag2.space/relay}" \
  SUTANDO_WORKSPACE="$AGENT_CONNECT_WORKSPACE" \
  python3 "$RELAY_CLIENT" &
  RELAY_PID=$!
  trap 'kill $RELAY_PID 2>/dev/null || true' EXIT
else
  echo "note: relay client not found at $RELAY_CLIENT — start it yourself, or set RELAY_CLIENT." >&2
fi

# 2) worker: turns each pulled task into an agent run.
exec python3 -m agent_connect
