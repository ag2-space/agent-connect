#!/usr/bin/env sh
# install.sh — one-line installer for agent-connect.
#
# Turns "clone the repo, configure, run" into a single paste: installs the
# worker, writes its config file and a launcher, and starts your local agent as
# a first-class AG2 Space room agent.
#
# ONE PROCESS. The worker carries its own relay client (workspace ADR 0001), so
# the launcher this writes starts the worker and nothing else. It used to start
# `ag2-sparrow` beside it, back when the wire lived in that separate package;
# after the worker took the wire over, the pair became two long-pollers holding
# one bearer with no lease between them, and a task the losing half never saw
# was a message that vanished without an error. The launch unit is a process.
#
#   curl -fsSL <installer-url>/install.sh | sh -s -- --token <TOKEN> [--adapter codex]
#
# Flags (also read from env):
#   --token   AGENT_CONNECT_TOKEN    your agent's relay token from the Agent Portal [required]
#   --adapter AGENT_CONNECT_ADAPTER  codex | omnigent | ollama | cline | acp  [default: codex]
#   --acp-agent AGENT_CONNECT_ACP_AGENT  which ACP agent, when --adapter acp: claude | gemini
#             [default: claude, or `custom` when --acp-command is given]
#   --acp-url AGENT_CONNECT_ACP_URL      dial an ACP agent already running behind
#                                        this ws:// URL instead of spawning one.
#                                        Mutually exclusive with --acp-command.
#   --acp-token AGENT_CONNECT_ACP_TOKEN  bearer for that URL's door
#   --acp-command AGENT_CONNECT_ACP_COMMAND  the ACP agent's command line, run as
#             typed. Overrides any preset. Omitted on a re-run, the stored one is kept.
#   --repo    AGENT_CONNECT_REPO     repo the agent works in       [default: ~/agents]
#   --no-start                       install only; print the run command, don't launch
#
# Retired: --sutando-workspace, which installed `ag2-sparrow` alone against a
# running Sutando's queue. That was this script placing another repo's package;
# ag2-sparrow has its own install line, and its coexistence with this worker is
# permanent-normal rather than something this installer arranges.
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
START=1
# agent-connect source: overridable so this same script serves both the
# private-repo phase (git+ssh for repo-holders) and the public phase (PyPI).
# Worker install source, flipped to PyPI by the v0.2.0 publish this comment
# used to wait for. The git spec it replaced installed the repository, and the
# repository declares a dependency on ag2-relay-client — a name that existed
# nowhere but the working tree until relay-client-v0.1.0, so every fresh
# install resolved to "No matching distribution found" no matter how healthy
# the clone was. Both distributions are on the index now. The floor is repeated
# here rather than left to pyproject because this line is what a fresh host
# resolves: without it an installer run can settle on a pre-transport worker
# that still expects task files.
AC_PIP_SPEC="${AGENT_CONNECT_PIP_SPEC:-ag2-agent-connect>=0.2.0}"
ACP_AGENT="${AGENT_CONNECT_ACP_AGENT:-}"
# Empty means "not asked for". The default is settled after arg parsing,
# because it depends on whether a command was supplied.
ACP_COMMAND="${AGENT_CONNECT_ACP_COMMAND:-}"
ACP_URL="${AGENT_CONNECT_ACP_URL:-}"
ACP_TOKEN="${AGENT_CONNECT_ACP_TOKEN:-}"
# The ACP bridge that makes Claude Code an ACP Agent, PINNED to an exact
# version. It renamed itself once already (the older name is dead — do not
# reintroduce it) and moved through many major versions inside six months; an
# unpinned fetch would eventually drop an
# incompatible release into an install that worked yesterday. This must stay
# equal to BRIDGE_SPEC in agent_connect/adapters/acp.py — install.test.sh
# asserts the two agree, so raising it is a reviewable diff in both places.
ACP_BRIDGE_SPEC="${AGENT_CONNECT_ACP_BRIDGE_SPEC:-@agentclientprotocol/claude-agent-acp@0.64.2}"

# Refused by name, not by falling through to "unknown arg": this flag is
# written down in scripts and support threads, and the operator who typed it
# needs a sentence about where the relay client went, not a parser's shrug.
#
# The env var gets the same treatment for the same reason, and it is the more
# important half: a flag that vanished at least fails, while an ignored
# AGENT_CONNECT_SUTANDO_WORKSPACE would have installed a worker for someone who
# asked for the opposite and told them nothing.
sutando_gone() {
  cat >&2 <<'GONE'
install.sh: --sutando-workspace (and AGENT_CONNECT_SUTANDO_WORKSPACE) is retired.

It installed the ag2-sparrow relay client alone, pointed at a running Sutando's
task/result/state directories. agent-connect no longer places that package: its
worker carries its own relay client, and ag2-sparrow is a separate package in a
separate repo that connects a running Sutando on its own terms.

  To connect a running Sutando:  pipx install ag2-sparrow
                                 (see that package's README for its directory
                                  and token settings)
  To run an agent from here:     install.sh --token <TOKEN> [--adapter ...]
GONE
  exit 2
}

[ -z "${AGENT_CONNECT_SUTANDO_WORKSPACE:-}" ] || sutando_gone

while [ $# -gt 0 ]; do
  case "$1" in
    --token)   TOKEN="$2"; shift 2 ;;
    --adapter) ADAPTER="$2"; shift 2 ;;
    --acp-agent) ACP_AGENT="$2"; shift 2 ;;
    --acp-command) ACP_COMMAND="$2"; shift 2 ;;
    --acp-url) ACP_URL="$2"; shift 2 ;;
    --acp-token) ACP_TOKEN="$2"; shift 2 ;;
    --repo)    REPO="$2"; shift 2 ;;
    --sutando-workspace|--sutando-workspace=*) sutando_gone ;;
    --no-start) START=0; shift ;;
    --token=*)   TOKEN="${1#*=}"; shift ;;
    --adapter=*) ADAPTER="${1#*=}"; shift ;;
    --acp-agent=*) ACP_AGENT="${1#*=}"; shift ;;
    --acp-command=*) ACP_COMMAND="${1#*=}"; shift ;;
    --acp-url=*) ACP_URL="${1#*=}"; shift ;;
    --acp-token=*) ACP_TOKEN="${1#*=}"; shift ;;
    --repo=*)    REPO="${1#*=}"; shift ;;
    *) echo "install.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TOKEN" ]; then
  echo "install.sh: --token is required (get it from the AG2 Space Agent Portal)." >&2
  exit 2
fi

# Captured before any default fills the variable in; the defaults are settled
# later, once the existing config has been read.
ACP_AGENT_GIVEN=0
[ -z "$ACP_AGENT" ] || ACP_AGENT_GIVEN=1

# A flag that silently does nothing is the bug class this file keeps fixing.
if [ -n "$ACP_URL" ] && [ "$ADAPTER" != "acp" ]; then
  echo "install.sh: WARNING — --acp-url applies only to --adapter acp; ignoring it for adapter '$ADAPTER'." >&2
  ACP_URL=""
  ACP_TOKEN=""
fi
if [ -n "$ACP_COMMAND" ] && [ "$ADAPTER" != "acp" ]; then
  echo "install.sh: WARNING — --acp-command applies only to --adapter acp; ignoring it for adapter '$ADAPTER'." >&2
  ACP_COMMAND=""
fi

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

# One setting per line is the file's whole grammar: no quoting saves a value
# with a newline in it, so refuse it up front.
# `$(printf '\n')` is the wrong spelling: command substitution strips trailing
# newlines, so the pattern would match every value.
NL='
'
CR="$(printf '\r')"
reject_multiline() {  # <flag> <value>
  case "$2" in
    *"$NL"*|*"$CR"*)
      echo "install.sh: $1 contains a newline, which a config file of KEY=value lines cannot carry. Remove it." >&2
      exit 2 ;;
  esac
}
reject_multiline --token "$TOKEN"
reject_multiline --repo "$REPO"
reject_multiline --adapter "$ADAPTER"
[ -z "$ACP_COMMAND" ] || reject_multiline --acp-command "$ACP_COMMAND"
[ -z "$ACP_URL" ] || reject_multiline --acp-url "$ACP_URL"
[ -z "$ACP_TOKEN" ] || reject_multiline --acp-token "$ACP_TOKEN"

# Refused here rather than resolved, for the same reason the adapter refuses it:
# a URL and a command name two different agents, and a Worker that picked one
# would answer rooms as something nobody chose.
if [ -n "$ACP_URL" ] && [ -n "$ACP_COMMAND" ]; then
  echo "install.sh: --acp-url and --acp-command name two different ACP agents; pass one." >&2
  exit 2
fi
[ -z "$ACP_AGENT" ] || reject_multiline --acp-agent "$ACP_AGENT"

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
# Named here rather than at 1c: the ACP decisions below need to read it.
CONFIG="$APP_DIR/config.env"

# Read, never executed. Last occurrence wins, as the worker's own parser does.
stored() {  # <KEY>
  [ -f "$CONFIG" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$CONFIG" | tail -n 1 \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
ACP_COMMAND_STORED="$(stored AGENT_CONNECT_ACP_COMMAND)"
ACP_AGENT_STORED="$(stored AGENT_CONNECT_ACP_AGENT)"
ACP_URL_STORED="$(stored AGENT_CONNECT_ACP_URL)"

# A stored command outranks any preset at runtime, so each way of arriving has
# to say what happens to one that is already there:
#   named an agent, no command  → preset decides, so the command is CLEARED
#   gave a command              → it runs; the name is bookkeeping
#   neither                     → change nothing, name included
# Scoped to --adapter acp: otherwise `--adapter codex --acp-agent claude` hits
# the first case and deletes a working command on its way past.
# A URL and a command are mutually exclusive *at runtime* — the worker refuses
# to start with both — so choosing one here has to remove the other. Leaving the
# old one behind writes a config file that the worker rejects, which is a worse
# outcome than either setting winning.
ACP_CMD_CLEAR=0
ACP_URL_CLEAR=0
if [ "$ADAPTER" != "acp" ]; then
  if [ "$ACP_AGENT_GIVEN" -eq 1 ]; then
    echo "install.sh: WARNING — --acp-agent applies only to --adapter acp; ignoring it for adapter '$ADAPTER'. Your stored ACP settings are left alone." >&2
  fi
  ACP_AGENT=""
elif [ -n "$ACP_URL" ]; then
  # Dialling: nothing is spawned, so a stored command and any preset name that
  # implies one are both stale.
  ACP_AGENT="custom"
  [ -z "$ACP_COMMAND_STORED" ] || ACP_CMD_CLEAR=1
elif [ "$ACP_AGENT_GIVEN" -eq 1 ]; then
  [ -n "$ACP_COMMAND" ] || ACP_CMD_CLEAR=1
  [ -z "$ACP_URL_STORED" ] || ACP_URL_CLEAR=1
elif [ -n "$ACP_COMMAND" ]; then
  ACP_AGENT="custom"
  [ -z "$ACP_URL_STORED" ] || ACP_URL_CLEAR=1
elif [ -n "$ACP_COMMAND_STORED" ]; then
  ACP_AGENT="${ACP_AGENT_STORED:-custom}"
else
  ACP_AGENT="${ACP_AGENT_STORED:-claude}"
fi

# Whether this install ends up dialling, which decides two things below: the
# WebSocket extra has to reach the environment the worker actually runs in, and
# no bridge is fetched for an agent that will never be spawned.
ACP_DIAL=0
if [ "$ADAPTER" = "acp" ]; then
  if [ -n "$ACP_URL" ]; then
    ACP_DIAL=1
  elif [ "$ACP_URL_CLEAR" -eq 0 ] && [ -z "$ACP_COMMAND" ] && [ -n "$ACP_URL_STORED" ]; then
    ACP_DIAL=1
  fi
fi
# The extra goes on the spec, not into a second `pip install` the reader runs
# afterwards: the worker lives in pipx's own environment or in a private venv,
# and a plain `pip install` reaches neither.
if [ "$ACP_DIAL" -eq 1 ]; then
  case "$AC_PIP_SPEC" in
    *"["*) : ;;                      # the operator already asked for extras
    ag2-agent-connect*)
      AC_PIP_SPEC="$(printf '%s' "$AC_PIP_SPEC" | sed 's/^ag2-agent-connect/ag2-agent-connect[websocket]/')" ;;
    *) say "note: AGENT_CONNECT_PIP_SPEC is custom; add the [websocket] extra yourself for a dialled agent" ;;
  esac
fi

# ── 1) install the worker ───────────────────────────────────────────────────
# One package: the wire is a library inside it, not a second console script to
# place beside it. Two isolation strategies, both PEP-668-safe (a bare
# `pip install --user` fails on externally-managed envs — Homebrew Python,
# modern Debian/Ubuntu):
#   pipx (preferred)  → isolated app installs; handles git+ AND PyPI specs.
#   dedicated venv    → fallback; deterministic bin paths for the service unit.
WORKER_BIN=""
say "installing the agent-connect worker (it carries its own relay client)"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$AC_PIP_SPEC" >/dev/null
  WORKER_BIN="$(command -v agent-connect || echo "$HOME/.local/bin/agent-connect")"
else
  VENV="$APP_DIR/venv"
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install --upgrade "$AC_PIP_SPEC" >/dev/null
  WORKER_BIN="$VENV/bin/agent-connect"
fi

[ -x "$WORKER_BIN" ] || {
  echo "install.sh: worker binary not found at '$WORKER_BIN' after install." >&2; exit 1; }
# Make the pipx bin dir reachable this session even if not yet on PATH.
case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
export PATH

# ── 1b) the ACP bridge, pinned ──────────────────────────────────────────────
# Only for --adapter acp. The pin is the point: see ACP_BRIDGE_SPEC above.
ACP_KV=""
ACP_CMD_KV=""
ACP_URL_KV=""
if [ "$ADAPTER" = "acp" ]; then
  ACP_KV="AGENT_CONNECT_ACP_AGENT=$ACP_AGENT"
  [ -z "$ACP_COMMAND" ] || ACP_CMD_KV="AGENT_CONNECT_ACP_COMMAND=$ACP_COMMAND"
  [ -z "$ACP_URL" ] || ACP_URL_KV="AGENT_CONNECT_ACP_URL=$ACP_URL"
  if [ "$ACP_URL_CLEAR" -eq 1 ]; then
    say "this run selects an ACP agent to spawn, so the dialled door already in your config is removed"
    say "  (was: $ACP_URL_STORED) — the worker refuses to start with both, so keeping it would have"
    say "  produced a config that will not run. Previous file: $CONFIG.bak"
  fi
  if [ "$ACP_CMD_CLEAR" -eq 1 ] && [ -n "$ACP_COMMAND_STORED" ] && [ -n "$ACP_URL" ]; then
    say "--acp-url replaces the AGENT_CONNECT_ACP_COMMAND already in your config"
    say "  (was: $ACP_COMMAND_STORED) — the worker refuses to start with both. Previous file: $CONFIG.bak"
  fi
  if [ "$ACP_CMD_CLEAR" -eq 1 ] && [ -n "$ACP_COMMAND_STORED" ]; then
    say "--acp-agent '$ACP_AGENT' replaces the AGENT_CONNECT_ACP_COMMAND already in your config"
    say "  (was: $ACP_COMMAND_STORED) — a command overrides every preset, so keeping it would have"
    say "  gone on running that agent while this install reported '$ACP_AGENT'. Previous file: $CONFIG.bak"
  fi
  if [ "$ACP_DIAL" -eq 1 ]; then
    say "dialling ${ACP_URL:-$ACP_URL_STORED} — no bridge is installed, because nothing is spawned"
    say "start that listener yourself, and keep its token: the worker only dials it"
  else
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
fi

# ── 1c) write the config file ───────────────────────────────────────────────
# Every setting in one 0600 file that agent-connect reads itself. The point is
# what is NOT elsewhere: the service definition below carries only a path to
# this file, so the bearer token stops living in plaintext in a launchd plist
# (world-readable by default) or a systemd unit. Same keys as README's Settings
# table; environment variables still win over the file.
# This installer is the documented `curl … | sh` path and people re-run it.
# A re-run must not silently eat a setting somebody added by hand, so the old
# file is kept beside the new one and every key this script does not manage is
# carried across.
KEPT=""
if [ -f "$CONFIG" ]; then
  BACKUP="$CONFIG.bak"
  cp "$CONFIG" "$BACKUP" && chmod 600 "$BACKUP"
  say "existing config kept as $BACKUP"
  # Managed keys are rewritten; everything else carried across verbatim.
  # ACP_COMMAND joins the set only when this run set or cleared one — otherwise
  # a re-run would wipe it and the adapter would fall back to the claude preset.
  MANAGED='AGENT_CONNECT_TOKEN|AGENT_CONNECT_ADAPTER|AGENT_CONNECT_REPO|AGENT_CONNECT_ACP_AGENT'
  if [ -n "$ACP_CMD_KV" ] || [ "$ACP_CMD_CLEAR" -eq 1 ]; then
    MANAGED="$MANAGED|AGENT_CONNECT_ACP_COMMAND"
  fi
  # Same rule as the command: rewritten only when this run set one, so a re-run
  # without the flag keeps the URL and token already in the file.
  if [ -n "$ACP_URL_KV" ] || [ "$ACP_URL_CLEAR" -eq 1 ]; then
    MANAGED="$MANAGED|AGENT_CONNECT_ACP_URL|AGENT_CONNECT_ACP_TOKEN"
  fi
  KEPT="$(grep -v -E "^[[:space:]]*($MANAGED)[[:space:]]*=" "$CONFIG" \
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
    #
    # printf, never echo: echo's backslash handling is implementation-defined,
    # and under dash a \t or \n in any value is rewritten (a newline splits one
    # setting into two lines).
    printf '%s\n' "AGENT_CONNECT_TOKEN=\"$TOKEN\""
    printf '%s\n' "AGENT_CONNECT_ADAPTER=\"$ADAPTER\""
    printf '%s\n' "AGENT_CONNECT_REPO=\"$REPO\""
    [ -z "$ACP_KV" ] || printf '%s\n' "AGENT_CONNECT_ACP_AGENT=\"$ACP_AGENT\""
    [ -z "$ACP_CMD_KV" ] || printf '%s\n' "AGENT_CONNECT_ACP_COMMAND=\"$ACP_COMMAND\""
    [ -z "$ACP_URL_KV" ] || printf '%s\n' "AGENT_CONNECT_ACP_URL=\"$ACP_URL\""
    [ -z "$ACP_URL_KV" ] || [ -z "$ACP_TOKEN" ] || printf '%s\n' "AGENT_CONNECT_ACP_TOKEN=\"$ACP_TOKEN\""
    if [ -n "$KEPT" ]; then
      printf '\n'
      printf '%s\n' "# kept from your previous config.env:"
      printf '%s\n' "$KEPT"
    fi
  } > "$CONFIG"
)
chmod 600 "$CONFIG"

# ── 2) write the launcher ────────────────────────────────────────────────────
# One code path for every start mode (launchd / systemd / nohup / by hand), and
# it starts ONE process. This used to start two — the relay client in the
# background, then the worker via exec — because the wire lived in a separate
# package. It does not any more (workspace ADR 0001), and the pair was not
# merely redundant: both halves long-polled the gateway with the same bearer and
# excluded each other from nothing, so a task the relay client won went into a
# directory the worker no longer reads. No error, no reply, nothing in the room.
LAUNCHER="$APP_DIR/launch.sh"
say "writing launcher $LAUNCHER"
cat > "$LAUNCHER" <<LAUNCH
#!/bin/sh
# launch.sh — written by install.sh. Starts the worker: one process, which is
# the whole launch unit. Every setting is read from the config file ($CONFIG by
# default; point AGENT_CONNECT_CONFIG elsewhere to move it). Anything already
# exported wins over the file, so the old "export it first" way still works
# unchanged.
set -eu
export AGENT_CONNECT_CONFIG="\${AGENT_CONNECT_CONFIG:-$CONFIG}"

# The token check below wants the config file's settings, and asking the worker
# for them is the whole design: \`--export-config\` prints the config file as
# shell, with the environment's own values already deferred to, so there is no
# second parser here to disagree with the first. Warnings go to stderr; only
# \`export\` lines reach stdout.
_cfg="\$("$WORKER_BIN" --export-config)" || exit 1
eval "\$_cfg"
unset _cfg
: "\${AGENT_CONNECT_TOKEN:?no token — it should be in \$AGENT_CONNECT_CONFIG}"
WS="\${AGENT_CONNECT_WORKSPACE:-\$HOME/.agent-connect/workspace}"
export AGENT_CONNECT_WORKSPACE="\$WS"
mkdir -p "\$WS"

# Kill a prior instance for THIS workspace before starting a new one (pidfile
# keyed to the workspace — sibling agents on other workspaces are untouched).
PIDFILE="\$WS/.worker.pids"
if [ -f "\$PIDFILE" ]; then
  while read -r _old; do
    [ -n "\$_old" ] && kill "\$_old" 2>/dev/null || true
  done < "\$PIDFILE"
  rm -f "\$PIDFILE"
fi

# \$\$ survives the exec, so the pidfile lets the next launch kill this instance.
echo "\$\$" > "\$PIDFILE"
exec "$WORKER_BIN"
LAUNCH
chmod +x "$LAUNCHER"    # holds no secret: the config file does

# Everything it needs is in the config file, so the command an operator copies
# out of a terminal — and into a shell history, and a support ticket — is a path
# rather than a bearer token.
RUN_CMD="AGENT_CONNECT_CONFIG=$CONFIG sh $LAUNCHER"

if [ "$START" -eq 0 ]; then
  say "install complete (not started). Run your agent with:"
  printf '\n  %s\n\n' "$RUN_CMD"
  exit 0
fi

# ── 3) start it (persistent) ────────────────────────────────────────────────
# Prefer a per-user service so the agent survives logout/reboot; fall back to a
# nohup background process if no service manager is available.
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
    <key>AGENT_CONNECT_CONFIG</key><string>$CONFIG</string>
    <key>PATH</key><string>$PATH</string>
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
Environment=AGENT_CONNECT_CONFIG=$CONFIG
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
  env AGENT_CONNECT_CONFIG="$CONFIG" \
      nohup sh "$LAUNCHER" >"$APP_DIR/agent-connect.log" 2>&1 &
  say "started (pid $!). Logs: $APP_DIR/agent-connect.log"
fi

say "done. Your agent should appear in AG2 Space shortly — @-mention it in an allowed room."
