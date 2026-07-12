#!/usr/bin/env sh
# install.sh — one-line installer for agent-connect.
#
# Turns "clone the repo, place the relay client, configure, run" into a single
# paste. Fetches the worker + the AG2 Space relay client (+ its stdlib-only
# deps, all from the PUBLIC sonichi/sutando repo), then starts your local agent
# as a first-class AG2 Space room agent.
#
#   curl -fsSL <installer-url>/install.sh | sh -s -- --token <TOKEN> [--adapter codex]
#
# Flags (also read from env):
#   --token   AGENT_CONNECT_TOKEN    your agent's relay token from the Agent Portal [required]
#   --adapter AGENT_CONNECT_ADAPTER  codex | omnigent | ollama | cline  [default: codex]
#   --repo    AGENT_CONNECT_REPO     repo the agent works in            [default: cwd]
#   --no-start                       install only; print the run command, don't launch
#
# Adapter → agent quick map:
#   codex    → OpenAI Codex CLI (native)            — your Codex login
#   omnigent → Claude Code, cursor, kimi, qwen, …   — `--adapter omnigent` (default harness: claude)
#   ollama   → local model via Ollama HTTP API      — no provider auth
# You still log into the underlying tool yourself; the token is your AG2 Space
# *identity*, never a model API key.
set -eu

# ── args ────────────────────────────────────────────────────────────────────
TOKEN="${AGENT_CONNECT_TOKEN:-}"
ADAPTER="${AGENT_CONNECT_ADAPTER:-codex}"
REPO="${AGENT_CONNECT_REPO:-$(pwd)}"
START=1
# agent-connect source: overridable so this same script serves both the
# private-repo phase (git+ssh for repo-holders) and the public phase (PyPI).
AC_PIP_SPEC="${AGENT_CONNECT_PIP_SPEC:-git+https://github.com/ag2-space/agent-connect.git}"
# public raw base for the relay client + its deps (these ARE public today).
RELAY_RAW_BASE="${RELAY_RAW_BASE:-https://raw.githubusercontent.com/sonichi/sutando/main/src}"

while [ $# -gt 0 ]; do
  case "$1" in
    --token)   TOKEN="$2"; shift 2 ;;
    --adapter) ADAPTER="$2"; shift 2 ;;
    --repo)    REPO="$2"; shift 2 ;;
    --no-start) START=0; shift ;;
    --token=*)   TOKEN="${1#*=}"; shift ;;
    --adapter=*) ADAPTER="${1#*=}"; shift ;;
    --repo=*)    REPO="${1#*=}"; shift ;;
    *) echo "install.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$TOKEN" ]; then
  echo "install.sh: --token is required (get it from the AG2 Space Agent Portal)." >&2
  exit 2
fi

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

# ── prerequisites ───────────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || {
  echo "install.sh: python3 not found — install Python 3.9+ first." >&2; exit 1; }
command -v curl >/dev/null 2>&1 || {
  echo "install.sh: curl not found." >&2; exit 1; }
PIP="python3 -m pip"
$PIP --version >/dev/null 2>&1 || {
  echo "install.sh: pip not available (python3 -m pip). Install pip first." >&2; exit 1; }

# ── 1) install the worker ───────────────────────────────────────────────────
# Two isolation strategies, both PEP-668-safe (a bare `pip install --user` fails
# on externally-managed envs — Homebrew Python, modern Debian/Ubuntu):
#   pipx (preferred)  → isolated app install; handles git+ AND PyPI specs.
#   dedicated venv    → fallback; deterministic worker path for the service unit.
WORKER_BIN=""
say "installing agent-connect worker"
if command -v pipx >/dev/null 2>&1; then
  pipx install --force "$AC_PIP_SPEC" >/dev/null
  WORKER_BIN="$(command -v agent-connect || echo "$HOME/.local/bin/agent-connect")"
else
  VENV="$HOME/.sutando-relay-client/venv"
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install --upgrade "$AC_PIP_SPEC" >/dev/null
  WORKER_BIN="$VENV/bin/agent-connect"
fi

# ── 2) fetch the relay client + its (stdlib-only) deps ──────────────────────
# The relay client is transport-only; it long-polls YOUR agent's tasks and posts
# results back. It + these deps live in the public sutando repo.
RELAY_DIR="$HOME/.sutando-relay-client"
mkdir -p "$RELAY_DIR"
say "fetching relay client into $RELAY_DIR"
for f in remote-gateway-bridge.py workspace_default.py task_archive.py \
         local_task_protocol.py result_markers.py send_allowlist.py \
         util_paths.py sutando_config.py; do
  curl -fsSL "$RELAY_RAW_BASE/$f" -o "$RELAY_DIR/$f" || {
    echo "install.sh: failed to fetch $f from $RELAY_RAW_BASE" >&2; exit 1; }
done
# Compat: run-agent.sh's default RELAY_CLIENT still points at the old filename.
# Leave a shim so either name works.
if [ ! -f "$RELAY_DIR/remote-relay-bridge.py" ]; then
  cat > "$RELAY_DIR/remote-relay-bridge.py" <<'SHIM'
#!/usr/bin/env python3
# compat shim: relay client was renamed remote-relay-bridge -> remote-gateway-bridge.
import runpy, sys
from pathlib import Path
runpy.run_path(str(Path(__file__).resolve().parent / "remote-gateway-bridge.py"),
               run_name="__main__")
SHIM
fi

# ── 3) resolve the worker entrypoint ────────────────────────────────────────
[ -x "$WORKER_BIN" ] || {
  echo "install.sh: worker binary not found at '$WORKER_BIN' after install." >&2; exit 1; }
# Make the pipx bin dir reachable this session even if not yet on PATH.
case ":$PATH:" in *":$HOME/.local/bin:"*) : ;; *) PATH="$HOME/.local/bin:$PATH" ;; esac
export PATH

RUN_CMD="AGENT_CONNECT_TOKEN=$TOKEN AGENT_CONNECT_ADAPTER=$ADAPTER AGENT_CONNECT_REPO=$REPO \
RELAY_CLIENT=$RELAY_DIR/remote-gateway-bridge.py $WORKER_BIN"

if [ "$START" -eq 0 ]; then
  say "install complete (not started). Run your agent with:"
  printf '\n  %s\n\n' "$RUN_CMD"
  exit 0
fi

# ── 4) start it (persistent) ────────────────────────────────────────────────
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
  <key>ProgramArguments</key><array><string>$WORKER_BIN</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>AGENT_CONNECT_TOKEN</key><string>$TOKEN</string>
    <key>AGENT_CONNECT_ADAPTER</key><string>$ADAPTER</string>
    <key>AGENT_CONNECT_REPO</key><string>$REPO</string>
    <key>RELAY_CLIENT</key><string>$RELAY_DIR/remote-gateway-bridge.py</string>
    <key>PATH</key><string>$PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$RELAY_DIR/agent-connect.log</string>
  <key>StandardErrorPath</key><string>$RELAY_DIR/agent-connect.log</string>
</dict></plist>
PLIST
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  say "loaded. Logs: $RELAY_DIR/agent-connect.log"
elif command -v systemctl >/dev/null 2>&1; then
  say "starting via systemd (per-user unit)"
  UNIT_DIR="$HOME/.config/systemd/user"; mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/agent-connect.service" <<UNIT
[Unit]
Description=AG2 Space agent-connect worker
After=network-online.target
[Service]
Environment=AGENT_CONNECT_TOKEN=$TOKEN
Environment=AGENT_CONNECT_ADAPTER=$ADAPTER
Environment=AGENT_CONNECT_REPO=$REPO
Environment=RELAY_CLIENT=$RELAY_DIR/remote-gateway-bridge.py
ExecStart=$WORKER_BIN
Restart=always
[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now agent-connect.service
  say "enabled. Logs: journalctl --user -u agent-connect -f"
else
  say "no service manager found — starting in the background (nohup)"
  # shellcheck disable=SC2086
  env AGENT_CONNECT_TOKEN="$TOKEN" AGENT_CONNECT_ADAPTER="$ADAPTER" \
      AGENT_CONNECT_REPO="$REPO" RELAY_CLIENT="$RELAY_DIR/remote-gateway-bridge.py" \
      nohup "$WORKER_BIN" >"$RELAY_DIR/agent-connect.log" 2>&1 &
  say "started (pid $!). Logs: $RELAY_DIR/agent-connect.log"
fi

say "done. Your agent should appear in AG2 Space shortly — @-mention it in an allowed room."
