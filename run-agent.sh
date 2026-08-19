#!/usr/bin/env bash
# Launch your local agent as an AG2 Space agent: the AG2 relay client (pulls your
# agent's tasks + posts results) + the agent-connect worker (runs the agent).
#
# Each of these may be exported, or written in the config file the worker reads
# (~/.agent-connect/config.env, or wherever AGENT_CONNECT_CONFIG points). An
# exported value wins over the file.
#
#   AGENT_CONNECT_TOKEN    your agent's relay token (from the Agent Portal) [required]
#   AGENT_CONNECT_ADAPTER  adapter, e.g. codex [required]
#   AGENT_CONNECT_REPO     repo the agent works in [default: ~/agents]
#   AGENT_CONNECT_WORKSPACE  task/result workspace [default: ~/.agent-connect/workspace]
#   RELAY_BIN              ag2-sparrow console script [default: `command -v ag2-sparrow`;
#                          `pip install ag2-sparrow` if you don't have it]
#   RELAY_CLIENT           legacy: path to a file-based relay client — only used
#                          when explicitly set (pre-PyPI installs)
set -euo pipefail

# Settings may be written down instead of exported: the same config file the
# worker reads itself (README.md § The config file). Parsed, never sourced, and
# the environment still wins — so `AGENT_CONNECT_ADAPTER=codex ./run-agent.sh`
# means what it says. This is what ends the "source this first" ritual: the
# relay client below gets its token from the same file the worker does.
AGENT_CONNECT_CONFIG="${AGENT_CONNECT_CONFIG:-$HOME/.agent-connect/config.env}"
export AGENT_CONNECT_CONFIG
if [ -f "$AGENT_CONNECT_CONFIG" ]; then
  while read -r _line || [ -n "$_line" ]; do
    case "$_line" in ''|'#'*) continue ;; *=*) : ;; *) continue ;; esac
    _key="${_line%%=*}"
    _val="${_line#*=}"
    # `KEY = value` is the same setting as `KEY=value`, exactly as it is to the
    # worker's own parser; the two readers must not disagree about one file.
    while :; do case "$_key" in *' '|*'	') _key="${_key%?}" ;; *) break ;; esac; done
    while :; do case "$_val" in ' '*|'	'*) _val="${_val#?}" ;; *) break ;; esac; done
    while :; do case "$_val" in *' '|*'	') _val="${_val%?}" ;; *) break ;; esac; done
    case "$_key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    # Settings, and nothing else. A config file able to set PATH would be
    # choosing which `codex` binary runs; agent_connect/config.py refuses the
    # same keys, for the same reason.
    case "$_key" in AGENT_CONNECT_*|REMOTE_TASK_*|OLLAMA_HOST) : ;; *) continue ;; esac
    case "$_val" in
      '"'*'"') _val="${_val#?}"; _val="${_val%?}" ;;
      "'"*"'") _val="${_val#?}"; _val="${_val%?}" ;;
    esac
    # The environment wins: only a variable that is unset or empty is filled in.
    eval "_cur=\${$_key:-}"
    [ -n "$_cur" ] || export "$_key=$_val"
  done < "$AGENT_CONNECT_CONFIG"
fi

: "${AGENT_CONNECT_TOKEN:?set AGENT_CONNECT_TOKEN — export it, or write it in $AGENT_CONNECT_CONFIG}"
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
