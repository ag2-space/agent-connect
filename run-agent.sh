#!/usr/bin/env bash
# Launch your local agent as an AG2 Space agent, from a checkout of this repo.
#
# ONE PROCESS. The worker carries its own relay client (workspace ADR 0001), so
# it long-polls the gateway itself and this script starts nothing beside it.
# It used to start two — `ag2-sparrow` in the background, then the worker —
# back when the wire lived in that separate package. After the worker took the
# wire over, the pair became two long-pollers holding one bearer with no lease
# between them: whichever won a given long-poll took the task, and the one
# ag2-sparrow won was written into a directory the worker no longer reads.
# No error, no reply, nothing in the room. The launch unit is a process.
#
# Settings — exported, or written in the config file the worker reads
# (~/.agent-connect/config.env, or wherever AGENT_CONNECT_CONFIG points). An
# exported value wins over the file, per setting.
#
#   AGENT_CONNECT_TOKEN    your agent's relay token (from the Agent Portal) [required]
#   AGENT_CONNECT_ADAPTER  adapter, e.g. codex [required]
#   AGENT_CONNECT_REPO     repo the agent works in [default: ~/agents]
#   AGENT_CONNECT_WORKSPACE  this agent's workspace [default: ~/.agent-connect/workspace]
set -euo pipefail

# Settings may be written down instead of exported: the config file the worker
# reads itself (README.md § The config file). This asks the worker for it rather
# than parsing it here — one parser, so a launcher and the worker cannot end up
# disagreeing about what the file says. The environment still wins, per setting,
# and that decision is made in there too.
export AGENT_CONNECT_CONFIG="${AGENT_CONNECT_CONFIG:-$HOME/.agent-connect/config.env}"
_cfg="$(python3 -m agent_connect --export-config)" || exit 1
eval "$_cfg"
unset _cfg
: "${AGENT_CONNECT_TOKEN:?set AGENT_CONNECT_TOKEN — export it, or write it in $AGENT_CONNECT_CONFIG}"
: "${AGENT_CONNECT_ADAPTER:?set AGENT_CONNECT_ADAPTER (e.g. codex)}"
export AGENT_CONNECT_WORKSPACE="${AGENT_CONNECT_WORKSPACE:-$HOME/.agent-connect/workspace}"
mkdir -p "$AGENT_CONNECT_WORKSPACE"

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

# $$ survives the exec — the worker keeps this shell's PID — so the pidfile
# above lets the next relaunch kill this instance.
echo "$$" > "$PIDFILE"
exec python3 -m agent_connect
