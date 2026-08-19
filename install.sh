#!/usr/bin/env sh
# install.sh — one-line installer for agent-connect.
#
# Turns "clone the repo, place the relay client, configure, run" into a single
# paste. Installs the worker + the AG2 Space relay client (the `ag2-sparrow`
# package, from PyPI), then starts your local agent as a first-class AG2 Space
# room agent.
#
#   curl -fsSL <installer-url>/install.sh | sh -s -- --token <TOKEN> [--adapter codex]
#
# Flags (also read from env):
#   --token   AGENT_CONNECT_TOKEN    your agent's relay token from the Agent Portal [required]
#   --adapter AGENT_CONNECT_ADAPTER  codex | omnigent | ollama | cline | acp  [default: codex]
#   --acp-agent AGENT_CONNECT_ACP_AGENT  which ACP agent, when --adapter acp: claude | gemini
#             [default: claude]  Any other ACP agent: set AGENT_CONNECT_ACP_COMMAND
#             yourself — it overrides the preset.
#   --repo    AGENT_CONNECT_REPO     repo the agent works in       [default: ~/agents]
#   --sutando-workspace PATH         connect an ALREADY-RUNNING Sutando instead of
#             (AGENT_CONNECT_SUTANDO_WORKSPACE)  installing a worker: relay-only mode,
#             task/result/state dirs point at that Sutando's workspace — its own
#             core session processes the tasks. --adapter/--repo are ignored.
#   --no-start                       install only; print the run command, don't launch
#
# Adapter → agent quick map:
#   codex    → OpenAI Codex CLI (native)            — your Codex login
#   omnigent → Claude Code, cursor, kimi, qwen, …   — `--adapter omnigent` (default harness: claude)
#   ollama   → local model via Ollama HTTP API      — no provider auth
#   acp      → any Agent Client Protocol agent       — `--adapter acp --acp-agent claude`
#              (owner-tier only, cooperative confinement — see README)
# You still log into the underlying tool yourself; the token is your AG2 Space
# *identity*, never a model API key.
set -eu

# ── args ────────────────────────────────────────────────────────────────────
TOKEN="${AGENT_CONNECT_TOKEN:-}"
ADAPTER="${AGENT_CONNECT_ADAPTER:-codex}"
# Working dir default is ~/agents (NOT pwd): a pwd default silently bakes
# wherever the user happened to run the installer into the service config —
# including TCC-protected folders like ~/Documents where a launchd-run agent
# cannot write (live-caught 2026-07-13). Explicit --repo (or env) still wins.
REPO="${AGENT_CONNECT_REPO:-$HOME/agents}"
SUTANDO_WS="${AGENT_CONNECT_SUTANDO_WORKSPACE:-}"
START=1
# agent-connect source: overridable so this same script serves both the
# private-repo phase (git+ssh for repo-holders) and the public phase (PyPI).
# Worker install source. Default stays the git spec until the PyPI publish
# (name pending owner confirmation) actually completes — flipping the default
# to an unpublished package would break every fresh install in the gap. The
# one-line flip to "ag2-agent-connect>=0.2.0" (or the confirmed name) lands as
# the publish commit.
AC_PIP_SPEC="${AGENT_CONNECT_PIP_SPEC:-git+https://github.com/ag2-space/agent-connect.git}"
# relay client: the ag2-sparrow package on PyPI (transport-only; long-polls YOUR
# agent's tasks and posts results back). Overridable for pre-release testing.
RELAY_PIP_SPEC="${RELAY_PIP_SPEC:-ag2-sparrow>=0.2.0}"
ACP_AGENT="${AGENT_CONNECT_ACP_AGENT:-claude}"
# The ACP bridge that makes Claude Code an ACP Agent, PINNED to an exact
# version. It renamed itself once already (the older name is dead — do not
# reintroduce it) and moved through many major versions inside six months; an
# unpinned fetch would eventually drop an
# incompatible release into an install that worked yesterday. This must stay
# equal to BRIDGE_SPEC in agent_connect/adapters/acp.py — install.test.sh
# asserts the two agree, so raising it is a reviewable diff in both places.
ACP_BRIDGE_SPEC="${AGENT_CONNECT_ACP_BRIDGE_SPEC:-@agentclientprotocol/claude-agent-acp@0.64.2}"

while [ $# -gt 0 ]; do
  case "$1" in
    --token)   TOKEN="$2"; shift 2 ;;
    --adapter) ADAPTER="$2"; shift 2 ;;
    --acp-agent) ACP_AGENT="$2"; shift 2 ;;
    --repo)    REPO="$2"; shift 2 ;;
    --sutando-workspace) SUTANDO_WS="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    --token=*)   TOKEN="${1#*=}"; shift ;;
    --adapter=*) ADAPTER="${1#*=}"; shift ;;
    --acp-agent=*) ACP_AGENT="${1#*=}"; shift ;;
    --repo=*)    REPO="${1#*=}"; shift ;;
    --sutando-workspace=*) SUTANDO_WS="${1#*=}"; shift ;;
    *) echo "install.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TOKEN" ]; then
  echo "install.sh: --token is required (get it from the AG2 Space Agent Portal)." >&2
  exit 2
fi

if [ -n "$SUTANDO_WS" ]; then
  if [ ! -d "$SUTANDO_WS" ]; then
    echo "install.sh: --sutando-workspace '$SUTANDO_WS' does not exist — run \`sutando whoami\` (or \`bash scripts/sutando-config.sh workspace\` in the Sutando repo) to get the right path." >&2
    exit 2
  fi
  # Canonicalize to an absolute path BEFORE it is persisted into launch.sh —
  # a relative path would be interpreted against whatever cwd launchd/systemd
  # runs the launcher from later, silently wiring the relay to a wrong queue.
  SUTANDO_WS="$(cd "$SUTANDO_WS" && pwd)"
  # Shape check: a running Sutando workspace always has tasks/ (the resolver's
  # bootstrap creates the canonical subdirs). Rejecting anything else catches
  # typos like \$HOME that would otherwise install "successfully" while the
  # running Sutando never sees a task.
  if [ ! -d "$SUTANDO_WS/tasks" ]; then
    echo "install.sh: '$SUTANDO_WS' does not look like a Sutando workspace (no tasks/ dir) — run \`sutando whoami\` on the Sutando instance and use its \"workspace\" value." >&2
    exit 2
  fi
fi

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

# Working directory: state it loudly (invisible defaults are how agents end up
# in the wrong folder), create it if it's the default, and warn on macOS
# TCC-protected paths where a service-run agent gets EPERM on writes.
mkdir -p "$REPO" 2>/dev/null || true
say "agent working directory: $REPO   (change with --repo <path>)"
case "$REPO" in
  "$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    echo "install.sh: WARNING — '$REPO' is in a macOS privacy-protected folder; the agent may get 'operation not permitted' on writes when running as a service. Prefer a path like \$HOME/agents." >&2 ;;
esac

# ── prerequisites ───────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || {
  echo "install.sh: python3 not found — install Python 3.9+ first." >&2; exit 1; }
PIP="python3 -m pip"
$PIP --version >/dev/null 2>&1 || {
  echo "install.sh: pip not available (python3 -m pip). Install pip first." >&2; exit 1; }

APP_DIR="$HOME/.agent-connect"
mkdir -p "$APP_DIR"

# ── 1) install the worker + the relay client ────────────────────────────────
# Two isolation strategies, both PEP-668-safe (a bare `pip install --user` fails
# on externally-managed envs — Homebrew Python, modern Debian/Ubuntu):
#   pipx (preferred)  → isolated app installs; handles git+ AND PyPI specs.
#   dedicated venv    → fallback; deterministic bin paths for the service unit.
WORKER_BIN=""
RELAY_BIN=""
if [ -n "$SUTANDO_WS" ]; then
  say "installing ag2-sparrow relay client (relay-only: the running Sutando is the worker)"
else
  say "installing agent-connect worker + ag2-sparrow relay client"
fi
if command -v pipx >/dev/null 2>&1; then
  [ -n "$SUTANDO_WS" ] || pipx install --force "$AC_PIP_SPEC" >/dev/null
  pipx install --force "$RELAY_PIP_SPEC" >/dev/null
  WORKER_BIN="$(command -v agent-connect || echo "$HOME/.local/bin/agent-connect")"
  RELAY_BIN="$(command -v ag2-sparrow || echo "$HOME/.local/bin/ag2-sparrow")"
else
  VENV="$APP_DIR/venv"
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  if [ -n "$SUTANDO_WS" ]; then
    "$VENV/bin/python" -m pip install --upgrade "$RELAY_PIP_SPEC" >/dev/null
  else
    "$VENV/bin/python" -m pip install --upgrade "$AC_PIP_SPEC" "$RELAY_PIP_SPEC" >/dev/null
  fi
  WORKER_BIN="$VENV/bin/agent-connect"
  RELAY_BIN="$VENV/bin/ag2-sparrow"
fi

if [ -z "$SUTANDO_WS" ]; then
  [ -x "$WORKER_BIN" ] || {
    echo "install.sh: worker binary not found at '$WORKER_BIN' after install." >&2; exit 1; }
fi
[ -x "$RELAY_BIN" ] || {
  echo "install.sh: relay binary not found at '$RELAY_BIN' after install." >&2; exit 1; }
# Make the pipx bin dir reachable this session even if not yet on PATH.
case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
export PATH

# ── 1b) the ACP bridge, pinned ──────────────────────────────────────────────
# Only for --adapter acp. The pin is the point: see ACP_BRIDGE_SPEC above.
ACP_KV=""
if [ "$ADAPTER" = "acp" ]; then
  ACP_KV="AGENT_CONNECT_ACP_AGENT=$ACP_AGENT"
  case "$ACP_AGENT" in
    claude)
      if command -v npm >/dev/null 2>&1; then
        say "installing the ACP bridge, pinned: $ACP_BRIDGE_SPEC"
        npm install -g "$ACP_BRIDGE_SPEC" >/dev/null 2>&1 || {
          echo "install.sh: WARNING — could not install $ACP_BRIDGE_SPEC. Install it yourself: npm install -g $ACP_BRIDGE_SPEC" >&2; }
      else
        echo "install.sh: WARNING — npm not found, so the ACP bridge was not installed. Install Node.js 18+, then: npm install -g $ACP_BRIDGE_SPEC" >&2
      fi
      say "log in to Claude Code yourself before starting (claude, then /login) — the worker never opens a terminal for you"
      ;;
    *)
      say "ACP agent '$ACP_AGENT': install and log into it yourself; agent-connect only pins the Claude Code bridge"
      ;;
  esac
fi

# ── 1c) write the config file ───────────────────────────────────────────────
# Every setting in one 0600 file that agent-connect reads itself. The point is
# what is NOT elsewhere: the service definition below carries only a path to
# this file, so the bearer token stops living in plaintext in a launchd plist
# (world-readable by default) or a systemd unit. Same keys as README's Settings
# table; environment variables still win over the file.
#
# Relay-only mode writes none: there is no worker there to read one, and the
# only process that needs the token is the relay client, which gets it from a
# launcher this script writes at mode 0700.
CONFIG=""
if [ -z "$SUTANDO_WS" ]; then
  CONFIG="$APP_DIR/config.env"
  # This installer is the documented `curl … | sh` path and people re-run it.
  # A re-run must not silently eat a setting somebody added by hand, so the old
  # file is kept beside the new one and every key this script does not manage is
  # carried across.
  KEPT=""
  if [ -f "$CONFIG" ]; then
    BACKUP="$CONFIG.bak"
    cp "$CONFIG" "$BACKUP" && chmod 600 "$BACKUP"
    say "existing config kept as $BACKUP"
    KEPT="$(grep -v -E '^[[:space:]]*(AGENT_CONNECT_TOKEN|AGENT_CONNECT_ADAPTER|AGENT_CONNECT_REPO|AGENT_CONNECT_ACP_AGENT)[[:space:]]*=' "$CONFIG" \
            | grep -v -E '^[[:space:]]*(#|$)' || true)"
  fi
  say "writing config $CONFIG (mode 0600 — it holds your token)"
  (
    umask 077
    {
      echo "# agent-connect settings, written by install.sh."
      echo "# Keep this file to yourself: it holds your agent identity's token."
      echo "# Every key in README.md's Settings table may be written here."
      # Values are quoted on the way out so that a token with edge whitespace
      # or quotes of its own survives the round trip: the reader strips one
      # matching pair and nothing else.
      echo "AGENT_CONNECT_TOKEN=\"$TOKEN\""
      echo "AGENT_CONNECT_ADAPTER=\"$ADAPTER\""
      echo "AGENT_CONNECT_REPO=\"$REPO\""
      [ -z "$ACP_KV" ] || echo "AGENT_CONNECT_ACP_AGENT=\"$ACP_AGENT\""
      if [ -n "$KEPT" ]; then
        echo ""
        echo "# kept from your previous config.env:"
        printf '%s\n' "$KEPT"
      fi
    } > "$CONFIG"
  )
  chmod 600 "$CONFIG"
fi

# ── 2) write the launcher ────────────────────────────────────────────────────
# One code path for every start mode (launchd / systemd / nohup / by hand):
# starts the relay client wired to the worker's workspace via the dir-interface
# env vars, then execs the worker. Pre-flip, the service units launched ONLY the
# worker — the relay never ran under launchd/systemd, so tasks never arrived.
LAUNCHER="$APP_DIR/launch.sh"
say "writing launcher $LAUNCHER"
if [ -n "$SUTANDO_WS" ]; then
  # Relay-only launcher: the running Sutando's core session consumes the
  # tasks, so there is NO worker here — starting one would double-process
  # the same queue. Dirs point at the Sutando workspace (its watcher + the
  # dashboard already know these paths).
  cat > "$LAUNCHER" <<LAUNCH
#!/bin/sh
# launch.sh — written by install.sh (--sutando-workspace mode): relay only,
# wired to the running Sutando instance's workspace.
#
# This file is mode 0700 because the token is in it. There is no config file in
# this mode and deliberately so: agent-connect is not installed here (the
# running Sutando is the worker), so nothing could read one — and a second
# implementation of the config reader, in shell, is exactly the thing that went
# wrong once already. One private file holds the secret either way.
set -eu
AGENT_CONNECT_TOKEN="\${AGENT_CONNECT_TOKEN:-$TOKEN}"
PIDFILE="$APP_DIR/.relay.pids"
if [ -f "\$PIDFILE" ]; then
  while read -r _old; do
    [ -n "\$_old" ] && kill "\$_old" 2>/dev/null || true
  done < "\$PIDFILE"
  rm -f "\$PIDFILE"
fi
echo "\$\$" > "\$PIDFILE"
AGENT_CONNECT_TASK_DIR="$SUTANDO_WS/tasks" \\
AGENT_CONNECT_RESULT_DIR="$SUTANDO_WS/results" \\
AGENT_CONNECT_STATE_DIR="$SUTANDO_WS/state" \\
REMOTE_TASK_TOKEN="\$AGENT_CONNECT_TOKEN" \\
REMOTE_TASK_URL="\${REMOTE_TASK_URL:-https://chat.ag2.space/relay}" \\
exec "$RELAY_BIN"
LAUNCH
else
  cat > "$LAUNCHER" <<LAUNCH
#!/bin/sh
# launch.sh — written by install.sh; starts relay client + worker as one unit.
# Every setting is read from the config file ($CONFIG by default; point
# AGENT_CONNECT_CONFIG elsewhere to move it). Anything already exported wins
# over the file, so the old "export it first" way still works unchanged.
set -eu
export AGENT_CONNECT_CONFIG="\${AGENT_CONNECT_CONFIG:-$CONFIG}"

# The relay client needs the same settings the worker does, and asking the
# worker for them is the whole design: `--export-config` prints the config file
# as shell, with the environment's own values already deferred to, so there is
# no second parser here to disagree with the first. Warnings go to stderr; only
# `export` lines reach stdout.
_cfg="\$("$WORKER_BIN" --export-config)" || exit 1
eval "\$_cfg"
unset _cfg
: "\${AGENT_CONNECT_TOKEN:?no token — it should be in \$AGENT_CONNECT_CONFIG}"
WS="\${AGENT_CONNECT_WORKSPACE:-\$HOME/.agent-connect/workspace}"
export AGENT_CONNECT_WORKSPACE="\$WS"
mkdir -p "\$WS/tasks" "\$WS/results" "\$WS/state"

# Kill a prior instance for THIS workspace before starting a new one (pidfile
# keyed to the workspace — sibling agents on other workspaces are untouched).
PIDFILE="\$WS/.worker.pids"
if [ -f "\$PIDFILE" ]; then
  while read -r _old; do
    [ -n "\$_old" ] && kill "\$_old" 2>/dev/null || true
  done < "\$PIDFILE"
  rm -f "\$PIDFILE"
fi

# Relay client (transport-only): pulls THIS agent's tasks into the workspace
# and posts results back, identified by the token. The AGENT_CONNECT_*_DIR
# trio is ag2-sparrow's dir interface — it aligns the relay's queue with the
# worker's \$WS/tasks + \$WS/results convention.
AGENT_CONNECT_TASK_DIR="\$WS/tasks" \\
AGENT_CONNECT_RESULT_DIR="\$WS/results" \\
AGENT_CONNECT_STATE_DIR="\$WS/state" \\
REMOTE_TASK_TOKEN="\$AGENT_CONNECT_TOKEN" \\
REMOTE_TASK_URL="\${REMOTE_TASK_URL:-https://chat.ag2.space/relay}" \\
"$RELAY_BIN" &
echo "\$!" > "\$PIDFILE"

# Worker: turns each pulled task into an agent run. \$\$ survives the exec, so
# the pidfile lets the next launch kill this instance too.
echo "\$\$" >> "\$PIDFILE"
exec "$WORKER_BIN"
LAUNCH
fi
if [ -n "$SUTANDO_WS" ]; then
  chmod 700 "$LAUNCHER"   # the token is in it
else
  chmod +x "$LAUNCHER"    # holds no secret: the config file does
fi

# Everything it needs is in the config file (or, in relay-only mode, in the 0700
# launcher), so the command an operator copies out of a terminal — and into a
# shell history, and a support ticket — is a path rather than a bearer token.
if [ -n "$CONFIG" ]; then
  RUN_CMD="AGENT_CONNECT_CONFIG=$CONFIG sh $LAUNCHER"
else
  RUN_CMD="sh $LAUNCHER"
fi

if [ "$START" -eq 0 ]; then
  say "install complete (not started). Run your agent with:"
  printf '\n  %s\n\n' "$RUN_CMD"
  exit 0
fi

# ── 3) start it (persistent) ────────────────────────────────────────────────
# Prefer a per-user service so the agent survives logout/reboot; fall back to a
# nohup background process if no service manager is available.
# Adapter-specific env, carried into whichever start mode is used. Empty for
# every adapter but acp, which is why these expand to nothing rather than to a
# variable set to the empty string.
OS="$(uname -s)"
if [ "$OS" = "Darwin" ]; then
  say "starting via launchd (per-user LaunchAgent)"
  LA_DIR="$HOME/Library/LaunchAgents"; mkdir -p "$LA_DIR"
  PLIST="$LA_DIR/space.ag2.agent-connect.plist"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>space.ag2.agent-connect</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>$LAUNCHER</string>
  </array>
  <!-- No token here, and no settings: this plist is world-readable by default,
       and everything the worker needs is in a private file the launcher reads. -->
  <key>EnvironmentVariables</key><dict>
${CONFIG:+"    <key>AGENT_CONNECT_CONFIG</key><string>$CONFIG</string>
"}    <key>PATH</key><string>$PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/agent-connect.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/agent-connect.log</string>
</dict></plist>
PLIST
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  say "loaded. Logs: $APP_DIR/agent-connect.log"
elif command -v systemctl >/dev/null 2>&1; then
  say "starting via systemd (per-user unit)"
  UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/agent-connect.service" <<UNIT
[Unit]
Description=AG2 Space agent-connect worker
After=network-online.target
[Service]
# No token in the unit file: it is in a private file the launcher reads.
${CONFIG:+Environment=AGENT_CONNECT_CONFIG=$CONFIG}
ExecStart=/bin/sh $LAUNCHER
Restart=always
[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now agent-connect.service
  say "enabled. Logs: journalctl --user -u agent-connect -f"
else
  say "no service manager found — starting in the background (nohup)"
  env ${CONFIG:+AGENT_CONNECT_CONFIG="$CONFIG"} \
      nohup sh "$LAUNCHER" >"$APP_DIR/agent-connect.log" 2>&1 &
  say "started (pid $!). Logs: $APP_DIR/agent-connect.log"
fi

say "done. Your agent should appear in AG2 Space shortly — @-mention it in an allowed room."
