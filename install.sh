#!/usr/bin/env sh
# install.sh — one-line installer for agent-connect.
#
# Turns "clone the repo, configure, run" into a single paste: installs the
# worker, writes its config file and a launcher, and starts your local agent as
# a first-class s.space room agent.
#
# ONE PROCESS. The worker carries its own relay client (workspace ADR 0001), so
# the launcher this writes starts the worker and nothing else. It used to start
# `ag2-sparrow` beside it, back when the wire lived in that separate package;
# after the worker took the wire over, the pair became two long-pollers holding
# one bearer with no lease between them, and a task the losing half never saw
# was a message that vanished without an error. The launch unit is a process.
#
#   curl -fsSL <installer-url>/install.sh | sh -s -- --base <URL> --agent-id <MXID> [--adapter codex]
#
# That asks for the one-time code s.space shows, without echoing it. The token
# the code is exchanged for goes only into the 0600 config file.
#
# Credential, one of (a re-run with none keeps the instance's stored token):
#   --base    AGENT_CONNECT_BASE     s.space backend URL that redeems a one-time code;
#                                    needs --agent-id
#   --code    AGENT_CONNECT_CODE     the acp. (or rot.) code: `-` reads one line from stdin; with
#                                    neither, it is asked for on the terminal. A code
#                                    typed here works once, but shell history keeps it
#   --trust-relay-host HOST          accept a relay the code names on another host
#   --token-stdin                    read the relay token as one line from stdin
#   --token   AGENT_CONNECT_TOKEN    the relay token itself (kept by ps and shell
#                                    history; prefer the two above)
#   --adapter AGENT_CONNECT_ADAPTER  codex | omnigent | ollama | cline | acp  [default: codex]
#   --acp-agent AGENT_CONNECT_ACP_AGENT  which ACP agent, when --adapter acp: claude | gemini | hermes
#             [default: claude, or `custom` when --acp-command is given]
#   --acp-url AGENT_CONNECT_ACP_URL      dial an ACP agent already running behind
#                                        this ws:// URL instead of spawning one.
#                                        Mutually exclusive with --acp-command.
#   --acp-token AGENT_CONNECT_ACP_TOKEN  bearer for that URL's door
#   --acp-command AGENT_CONNECT_ACP_COMMAND  the ACP agent's command line, run as
#             typed. Overrides any preset. Omitted on a re-run, the stored one is kept.
#   --collaborators AGENT_CONNECT_ACP_COLLABORATORS  exact Matrix ids, comma
#             separated, of the agents and people allowed to use an ACP agent
#             besides you (they also need Collaborator access in the room).
#             "all" admits every sender any room grants Collaborator access; a
#             private room with no Agent Native card grants it to every member.
#             "" clears the list. Omitted on a re-run, the stored list is kept.
#   --collaborator-rooms AGENT_CONNECT_ACP_COLLABORATOR_ROOMS  room ids, comma
#             separated, in which every sender with Collaborator access may use
#             an ACP agent. "" clears them. Omitted on a re-run, they are kept.
#   --repo    AGENT_CONNECT_REPO     repo the agent works in       [default: ~/agents]
#   --instance AGENT_CONNECT_INSTANCE  separate config, workspace and service
#                                    [default: default, the legacy single-agent install]
#   --agent-id                        derive a stable instance from the full Matrix id;
#                                     overrides AGENT_CONNECT_INSTANCE; cannot combine
#                                     with --instance (survives token rotation)
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
#              (owner and admitted collaborators only, cooperative confinement — see README)
# You still log into the underlying tool yourself; the token is your s.space
# *identity*, never a model API key.
set -eu

# ── args ────────────────────────────────────────────────────────────────────
TOKEN="${AGENT_CONNECT_TOKEN:-}"
TOKEN_STDIN=0
TOKEN_ARGV=0
BASE="${AGENT_CONNECT_BASE:-}"
CODE="${AGENT_CONNECT_CODE:-}"
CODE_ARGV=0
TRUST_RELAY_HOST=""
INSTANCE="${AGENT_CONNECT_INSTANCE:-default}"
AGENT_ID=""
AGENT_ID_GIVEN=0
INSTANCE_GIVEN=0
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
AC_PIP_SPEC="${AGENT_CONNECT_PIP_SPEC:-ag2-agent-connect>=0.6.3}"
ACP_AGENT="${AGENT_CONNECT_ACP_AGENT:-}"
# Empty means "not asked for". The default is settled after arg parsing,
# because it depends on whether a command was supplied.
ACP_COMMAND="${AGENT_CONNECT_ACP_COMMAND:-}"
ACP_URL="${AGENT_CONNECT_ACP_URL:-}"
ACP_TOKEN="${AGENT_CONNECT_ACP_TOKEN:-}"
# Tracked apart from its value: rotating a bearer is its own operation, and a
# re-run that only supplies a new one must replace the stored bearer rather than
# leave the worker authenticating with a credential the operator just revoked.
ACP_TOKEN_GIVEN=0
# Given means the flag or the variable was supplied, even empty (which clears).
COLLABORATORS="${AGENT_CONNECT_ACP_COLLABORATORS:-}"
COLLABORATORS_GIVEN=0
[ -z "${AGENT_CONNECT_ACP_COLLABORATORS+x}" ] || COLLABORATORS_GIVEN=1
COLLAB_ROOMS="${AGENT_CONNECT_ACP_COLLABORATOR_ROOMS:-}"
COLLAB_ROOMS_GIVEN=0
[ -z "${AGENT_CONNECT_ACP_COLLABORATOR_ROOMS+x}" ] || COLLAB_ROOMS_GIVEN=1
# Supplied is supplied: the documented interface is a flag *or* the environment
# variable, and an operator who exported a fresh bearer and re-ran the installer
# has rotated it just as much as one who typed the flag. Reading only the flag
# left the revoked credential in the file.
[ -z "$ACP_TOKEN" ] || ACP_TOKEN_GIVEN=1
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
  To run an agent from here:     install.sh --base <URL> --agent-id <MXID> [--adapter ...]
GONE
  exit 2
}

[ -z "${AGENT_CONNECT_SUTANDO_WORKSPACE:-}" ] || sutando_gone

while [ $# -gt 0 ]; do
  case "$1" in
    --token)   TOKEN="$2"; TOKEN_ARGV=1; shift 2 ;;
    --token-stdin) TOKEN_STDIN=1; shift ;;
    --base)    BASE="$2"; shift 2 ;;
    --code)    CODE="$2"; [ "$2" = - ] || CODE_ARGV=1; shift 2 ;;
    --trust-relay-host) TRUST_RELAY_HOST="$2"; shift 2 ;;
    --adapter) ADAPTER="$2"; shift 2 ;;
    --acp-agent) ACP_AGENT="$2"; shift 2 ;;
    --acp-command) ACP_COMMAND="$2"; shift 2 ;;
    --acp-url) ACP_URL="$2"; shift 2 ;;
    --acp-token) ACP_TOKEN="$2"; ACP_TOKEN_GIVEN=1; shift 2 ;;
    --collaborators) COLLABORATORS="${2-}"; COLLABORATORS_GIVEN=1; [ $# -ge 2 ] || { echo 'install.sh: --collaborators needs a list (or "").' >&2; exit 2; }; shift 2 ;;
    --collaborator-rooms) COLLAB_ROOMS="${2-}"; COLLAB_ROOMS_GIVEN=1; [ $# -ge 2 ] || { echo 'install.sh: --collaborator-rooms needs a list (or "").' >&2; exit 2; }; shift 2 ;;
    --repo)    REPO="$2"; shift 2 ;;
    --instance) INSTANCE="${2:-}"; INSTANCE_GIVEN=1; [ $# -ge 2 ] || { echo 'install.sh: --instance needs a name.' >&2; exit 2; }; shift 2 ;;
    --agent-id) AGENT_ID="${2:-}"; AGENT_ID_GIVEN=1; [ $# -ge 2 ] || { echo 'install.sh: --agent-id needs a Matrix id.' >&2; exit 2; }; shift 2 ;;
    --sutando-workspace|--sutando-workspace=*) sutando_gone ;;
    --no-start) START=0; shift ;;
    --token=*)   TOKEN="${1#*=}"; TOKEN_ARGV=1; shift ;;
    --base=*)    BASE="${1#*=}"; shift ;;
    --code=*)    CODE="${1#*=}"; [ "$CODE" = - ] || CODE_ARGV=1; shift ;;
    --trust-relay-host=*) TRUST_RELAY_HOST="${1#*=}"; shift ;;
    --adapter=*) ADAPTER="${1#*=}"; shift ;;
    --acp-agent=*) ACP_AGENT="${1#*=}"; shift ;;
    --acp-command=*) ACP_COMMAND="${1#*=}"; shift ;;
    --acp-url=*) ACP_URL="${1#*=}"; shift ;;
    --acp-token=*) ACP_TOKEN="${1#*=}"; ACP_TOKEN_GIVEN=1; shift ;;
    --collaborators=*) COLLABORATORS="${1#*=}"; COLLABORATORS_GIVEN=1; shift ;;
    --collaborator-rooms=*) COLLAB_ROOMS="${1#*=}"; COLLAB_ROOMS_GIVEN=1; shift ;;
    --repo=*)    REPO="${1#*=}"; shift ;;
    --instance=*) INSTANCE="${1#*=}"; INSTANCE_GIVEN=1; shift ;;
    --agent-id=*) AGENT_ID="${1#*=}"; AGENT_ID_GIVEN=1; shift ;;
    *) echo "install.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# An explicit identity wins over an exported instance default. Only two
# explicit selectors are ambiguous; a shell's default must not break the web
# setup command or move that identity into the shell's instance directory.
if [ "$AGENT_ID_GIVEN" -eq 1 ]; then
  if [ "$INSTANCE_GIVEN" -eq 1 ]; then
    echo 'install.sh: pass --agent-id or --instance, not both.' >&2
    exit 2
  fi
  INSTANCE=default
fi

# Names become paths and service labels. Lowercase avoids aliases on the
# case-insensitive filesystems macOS commonly uses; never sanitise two names
# into one. Spell out ASCII characters: [a-z] also matches capitals/accented
# letters under macOS UTF-8 locales. Keep the Worker's 32-character bound.
case "$INSTANCE" in
  ''|*[!abcdefghijklmnopqrstuvwxyz0123456789_-]*) echo 'install.sh: --instance must use 1-32 lowercase letters, digits, _ or -.' >&2; exit 2 ;;
esac
if [ "${#INSTANCE}" -gt 32 ]; then
  echo 'install.sh: --instance must use 1-32 lowercase letters, digits, _ or -.' >&2
  exit 2
fi

# Credential source. Exactly one; none means "keep the stored token" and is
# settled once the config path is known.
_sources=0
[ -z "$TOKEN" ] || _sources=$((_sources+1))
[ "$TOKEN_STDIN" -eq 0 ] || _sources=$((_sources+1))
[ -z "$BASE$CODE" ] || _sources=$((_sources+1))
if [ "$_sources" -eq 0 ] && [ "$AGENT_ID_GIVEN" -eq 0 ] && [ "$INSTANCE_GIVEN" -eq 0 ]; then
  # Keeping a stored token is a re-run of one named install, never a bare run:
  # a bare run would rewrite the default install with this script's defaults.
  echo "install.sh: no credential: pass --base and --agent-id to use the one-time code s.space shows, --token-stdin, or --agent-id/--instance to re-run an install with its stored token." >&2
  exit 2
fi
if [ "$_sources" -gt 1 ]; then
  echo "install.sh: pass one of --token, --token-stdin or --base/--code." >&2
  exit 2
fi
if [ -n "$CODE" ] && [ -z "$BASE" ]; then
  echo "install.sh: --code needs --base, the s.space backend URL shown beside the code." >&2
  exit 2
fi
if [ -n "$BASE" ] && [ "$AGENT_ID_GIVEN" -eq 0 ]; then
  echo "install.sh: --base needs --agent-id, the Matrix id shown beside the code." >&2
  exit 2
fi
# Under `curl … | sh`, stdin is this script: reading a line from it would eat
# the installer's own text.
stdin_is_script() { [ ! -f "$0" ]; }
if { [ "$TOKEN_STDIN" -eq 1 ] || [ "$CODE" = - ]; } && stdin_is_script; then
  echo "install.sh: stdin is this script here (curl | sh). Download install.sh and run it with sh, or leave out --code to be asked for the code." >&2
  exit 2
fi
case $- in
  *x*) set +x; echo "install.sh: tracing is off from here: it would print the code and the token." >&2 ;;
esac
if [ "$TOKEN_STDIN" -eq 1 ]; then
  IFS= read -r TOKEN || true
  [ -n "$TOKEN" ] || { echo "install.sh: --token-stdin read no token." >&2; exit 2; }
fi
if [ "$CODE" = - ]; then
  IFS= read -r CODE || true
  [ -n "$CODE" ] || { echo "install.sh: --code - read no code." >&2; exit 2; }
elif [ -n "$BASE" ] && [ -z "$CODE" ]; then
  if ! { : </dev/tty; } 2>/dev/null; then
    echo "install.sh: no terminal to ask for the code on; pipe it in with --code -." >&2
    exit 2
  fi
  printf 's.space code: ' >/dev/tty
  trap 'stty echo </dev/tty 2>/dev/null; exit 130' INT TERM
  stty -echo </dev/tty 2>/dev/null || true
  IFS= read -r CODE </dev/tty || true
  stty echo </dev/tty 2>/dev/null || true
  trap - INT TERM
  printf '\n' >/dev/tty
  [ -n "$CODE" ] || { echo "install.sh: no code was entered." >&2; exit 2; }
fi
[ "$TOKEN_ARGV" -eq 0 ] || echo "install.sh: note: a token on the command line is kept by ps and shell history; next time use --base/--code or --token-stdin." >&2
[ "$CODE_ARGV" -eq 0 ] || echo "install.sh: note: a code on the command line is kept by shell history; it works once, but next time use --code - or the prompt." >&2
CODE="$(printf '%s' "$CODE" | tr -d '[:space:]')"
if [ -n "$BASE" ]; then
  case "$CODE" in
    acp.?*|rot.?*) : ;;
    *) echo "install.sh: that is not an agent code (it starts with acp. or rot.); copy it from s.space again. The code was not used." >&2; exit 2 ;;
  esac
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
[ -z "$TOKEN" ] || reject_multiline --token "$TOKEN"
[ -z "$BASE" ] || reject_multiline --base "$BASE"
reject_multiline --repo "$REPO"
reject_multiline --adapter "$ADAPTER"
[ -z "$AGENT_ID" ] || reject_multiline --agent-id "$AGENT_ID"
if [ "$AGENT_ID_GIVEN" -eq 1 ]; then
  case "$AGENT_ID" in
    @:*|@*:) echo 'install.sh: --agent-id needs a full Matrix id (@name:server).' >&2; exit 2 ;;
    @*:*) : ;;
    *) echo 'install.sh: --agent-id needs a full Matrix id (@name:server).' >&2; exit 2 ;;
  esac
  if [ "${#AGENT_ID}" -gt 512 ]; then
    echo 'install.sh: --agent-id is too long.' >&2
    exit 2
  fi
fi
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

if [ "$AGENT_ID_GIVEN" -eq 1 ]; then
  # Hash public identity, not the bearer: rotating a token must reuse the same
  # config and conversation state. Include the homeserver to isolate dev/prod.
  INSTANCE="agent-$(printf '%s' "$AGENT_ID" | python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:24])')"
fi

APP_DIR="$HOME/.agent-connect"
SERVICE_LABEL="space.ag2.agent-connect"
SERVICE_UNIT="agent-connect.service"
if [ "$INSTANCE" != default ]; then
  APP_DIR="$APP_DIR/instances/$INSTANCE"
  SERVICE_LABEL="$SERVICE_LABEL.$INSTANCE"
  SERVICE_UNIT="agent-connect-$INSTANCE.service"
fi
mkdir -p "$APP_DIR"
# Exact Matrix ids only: a typo here would silently admit nobody, or the wrong id.
_all_collab=0
case "$(printf '%s' "$COLLABORATORS" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')" in all|'*') _all_collab=1 ;; esac
if [ "$COLLABORATORS_GIVEN" -eq 1 ] && [ "$_all_collab" -eq 1 ]; then
  COLLABORATORS='*'
  echo "install.sh: --collaborators all: every sender any room grants Collaborator access can use this agent with its normal tools, in every room it is in. A private room with no Agent Native card grants that to every member, and room moderators can change it. It does not make anyone owner." >&2
elif [ "$COLLABORATORS_GIVEN" -eq 1 ]; then
  _clean=""
  set -f
  for _id in $(printf '%s' "$COLLABORATORS" | tr ',' ' '); do
    printf '%s\n' "$_id" | grep -Eq '^@[^@:[:space:]]+:[^@:[:space:]]+(:[0-9]+)?$' || {
      echo "install.sh: --collaborators: not a Matrix id: $_id (expected @name:server)" >&2
      exit 2
    }
    _clean="${_clean:+$_clean,}$_id"
  done
  set +f
  COLLABORATORS="$_clean"
fi
if [ "$COLLAB_ROOMS_GIVEN" -eq 1 ]; then
  _clean=""
  set -f
  for _id in $(printf '%s' "$COLLAB_ROOMS" | tr ',' ' '); do
    printf '%s\n' "$_id" | grep -Eq '^![^!:[:space:]]+:[^!:[:space:]]+(:[0-9]+)?$' || {
      echo "install.sh: --collaborator-rooms: not a room id: $_id (expected !id:server)" >&2
      exit 2
    }
    _clean="${_clean:+$_clean,}$_id"
  done
  set +f
  COLLAB_ROOMS="$_clean"
fi
# Named here rather than at 1c: the ACP decisions below need to read it.
CONFIG="$APP_DIR/config.env"

# Read, never executed. Last occurrence wins, as the worker's own parser does.
stored() {  # <KEY> [config path]
  _source="${2:-$CONFIG}"
  [ -f "$_source" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$_source" | tail -n 1 \
    | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
    | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}
ACP_COMMAND_STORED="$(stored AGENT_CONNECT_ACP_COMMAND)"
ACP_AGENT_STORED="$(stored AGENT_CONNECT_ACP_AGENT)"
ACP_URL_STORED="$(stored AGENT_CONNECT_ACP_URL)"
if [ "$_sources" -eq 0 ]; then
  TOKEN="$(stored AGENT_CONNECT_TOKEN)"
  if [ -z "$TOKEN" ]; then
    echo "install.sh: no credential: pass --base and --agent-id to use the one-time code s.space shows, or --token-stdin." >&2
    exit 2
  fi
  say "no credential given: keeping the token already in $CONFIG"
fi

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
# --no-cache-dir: pip caches the package index, so a run soon after a release
# can miss it ("no matching distribution") while PyPI already serves it.
if command -v pipx >/dev/null 2>&1; then
  pipx install --force --pip-args=--no-cache-dir "$AC_PIP_SPEC" >/dev/null
  WORKER_BIN="$(command -v agent-connect || echo "$HOME/.local/bin/agent-connect")"
else
  VENV="$APP_DIR/venv"
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV/bin/python" -m pip install --no-cache-dir --upgrade "$AC_PIP_SPEC" >/dev/null
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
ACP_TOKEN_KV=""
if [ "$ADAPTER" = "acp" ]; then
  ACP_KV="AGENT_CONNECT_ACP_AGENT=$ACP_AGENT"
  [ -z "$ACP_COMMAND" ] || ACP_CMD_KV="AGENT_CONNECT_ACP_COMMAND=$ACP_COMMAND"
  [ -z "$ACP_URL" ] || ACP_URL_KV="AGENT_CONNECT_ACP_URL=$ACP_URL"
  # A token is written when this run supplied one and the result actually dials,
  # or when a new URL is being written and a token came with it.
  if [ -n "$ACP_TOKEN" ] && { [ -n "$ACP_URL_KV" ] || \
     { [ "$ACP_TOKEN_GIVEN" -eq 1 ] && [ "$ACP_DIAL" -eq 1 ]; }; }; then
    ACP_TOKEN_KV="AGENT_CONNECT_ACP_TOKEN=$ACP_TOKEN"
  fi
  if [ "$ACP_TOKEN_GIVEN" -eq 1 ] && [ "$ACP_DIAL" -eq 0 ]; then
    echo "install.sh: WARNING — --acp-token needs a dialled agent; pass --acp-url too, or leave the stored one in place. Ignoring it." >&2
  fi
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

# ── 1b') redeem the one-time code ───────────────────────────────────────────
# Only now, with the worker installed: a machine that cannot run the agent
# never spends the code. The code goes in on stdin and the token comes back on
# stdout into a variable, so neither is ever an argument of any process.
if [ -n "$BASE" ]; then
  say "redeeming the one-time code with $BASE"
  REDEEM_PY="$APP_DIR/.redeem.py"
  cat > "$REDEEM_PY" <<'PY'
import json, os, re, signal, sys, time, urllib.error, urllib.parse, urllib.request

for _sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_sig, lambda *_: sys.exit(130))

base, agent_id, trusted = sys.argv[1], sys.argv[2], sys.argv[3].strip().rstrip(".").lower()
code = sys.stdin.readline().strip()
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def die(msg):
    sys.stderr.write("install.sh: " + msg + "\n")
    sys.exit(1)


def clean(value):
    # One line of the config file, and one `|` between URL and secret: only
    # printable ASCII, no space, quote, backslash or `|`.
    return bool(value) and all(0x21 <= ord(c) <= 0x7E and c not in '|"\\' for c in value)


def url_problem(url, what):
    if not clean(url):
        return what + " has a character a URL here may not carry"
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port
    except ValueError:
        return what + " is not a URL"
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password:
        return what + " is not an https URL"
    if not re.fullmatch(r"[a-z0-9.-]+|[0-9a-f:.]+", parts.hostname):
        return what + " has a host name with characters a host may not carry"
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        return what + " is not https; only a loopback host may use plain http"
    return ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


why = url_problem(base, "--base")
if why:
    die(why + "; the code was not used")
if not clean(agent_id):
    die("--agent-id has a space or a character a Matrix id may not carry; the code was not used")
opener = urllib.request.build_opener(NoRedirect)
url = base.rstrip("/") + "/connect/poll"
body = json.dumps({"device_code": code}).encode()
# A rot. code waits, up to a hard deadline, for the owner to confirm on the
# verify page; any other code gets five backoff retries.
rotating = code.startswith("rot.")
# AGENT_CONNECT_ROTATE_WAIT_S is a test seam, not a supported setting.
try:
    limit = float(os.environ.get("AGENT_CONNECT_ROTATE_WAIT_S") or 180)
except ValueError:
    limit = 180.0
if not 0 < limit <= 180:
    limit = 180.0
deadline = time.monotonic() + limit
backoff = iter((1, 2, 4, 8, 16))
while True:
    left = deadline - time.monotonic()
    # The edge refuses urllib's default User-Agent (Cloudflare 1010, a 403).
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "agent-connect-installer/1.0"})
    try:
        with opener.open(req, timeout=min(15.0, max(1.0, left)) if rotating else 15) as resp:
            status, raw = resp.status, resp.read(65536)
    except urllib.error.HTTPError as e:
        status, raw = e.code, (e.read(65536) or b"")
    except (urllib.error.URLError, OSError, ValueError) as e:
        if rotating and deadline - time.monotonic() <= 1:
            status = 202
            break
        die("could not reach %s (%s); the code is still valid until it expires, run this again" %
            (base, type(e).__name__))
    try:
        answer = json.loads(raw.decode("utf-8"))
    except ValueError:
        answer = {}
    answer = answer if isinstance(answer, dict) else {}
    if status in (202, 503):
        after = answer.get("retry_after")
        ok = status == 202 and isinstance(after, (int, float)) and not isinstance(after, bool) and after == after
        step = 5.0 if rotating else next(backoff, None)
        wait = None if step is None else (min(max(float(after), 1.0), 30.0) if ok else step)
        if rotating:
            left = deadline - time.monotonic()
            wait = min(wait, left) if left > 0 else None
        if wait is not None:
            time.sleep(wait)
            continue
    break
if status == 202 and code.startswith("rot."):
    die("the rotation is not confirmed yet; confirm it on s.space's verify page, then run this again with the same code")
if status == 202:
    die("s.space is still redeeming the code; run this again with the same code in a minute")
if status == 503:
    die("s.space is busy (HTTP 503 on every try); the code is still valid, try again in a minute")
if status == 404:
    if answer.get("error") == "agent_revoked" or answer.get("reason") == "agent_revoked":
        die("the agent was removed in s.space; add it again there for a new code")
    die("the code was already used or has expired; make a new one in s.space")
if 300 <= status < 400:
    die("s.space redirected (HTTP %d); not following it with a code" % status)
if status != 200:
    die("s.space refused the code (HTTP %d)" % status)
got = {k: answer.get(k) for k in ("relay_url", "bearer", "agent_id")}
if not all(isinstance(v, str) and v.strip() for v in got.values()):
    die("s.space's answer had no relay_url, bearer or agent_id; the code is spent, make a new one")
relay, bearer, got_id = (got[k] for k in ("relay_url", "bearer", "agent_id"))
if got_id != agent_id:
    die("the code is for a different agent than %s; the code is spent and nothing was written: "
        "make a new code for the agent you meant" % agent_id)
why = url_problem(relay, "the relay URL s.space returned")
if why:
    die(why + "; the code is spent and nothing was written")
host = urllib.parse.urlsplit(relay).hostname.rstrip(".").lower()
base_host = urllib.parse.urlsplit(base).hostname.rstrip(".").lower()
if host != base_host and not host.endswith("." + base_host) and host != trusted:
    die("the relay URL s.space returned is on %s, not %s or a host under it; the code is spent and "
        "nothing was written. If that relay is expected, run this again with a new code and "
        "--trust-relay-host %s" % (host, base_host, host))
if not clean(bearer):
    die("s.space's answer held a malformed credential; the code is spent and nothing was written")
sys.stdout.write(relay + "|" + bearer)
PY
  trap 'rm -f "$REDEEM_PY"' EXIT
  trap 'rm -f "$REDEEM_PY"; exit 130' INT TERM
  _rc=0
  TOKEN="$(printf '%s\n' "$CODE" | python3 -I "$REDEEM_PY" "$BASE" "$AGENT_ID" "$TRUST_RELAY_HOST")" || _rc=$?
  rm -f "$REDEEM_PY"
  trap - EXIT INT TERM
  [ "$_rc" -eq 0 ] || exit 1
  # Backstop: exactly one `|`, and nothing that could end the config line.
  reject_multiline --token "$TOKEN"
  case "$TOKEN" in
    *'|'*'|'*|*'"'*|*\\*|*[[:space:]]*|*[![:print:]]*|'|'*|*'|') echo "install.sh: the redeemed token is malformed; nothing was written." >&2; exit 1 ;;
    *'|'*) : ;;
    *) echo "install.sh: the redeemed token is malformed; nothing was written." >&2; exit 1 ;;
  esac
  say "code redeemed for $AGENT_ID; its token is stored only in the config file"
fi

MIGRATE_DEFAULT=0
if [ "$INSTANCE" != default ]; then
  LEGACY_TOKEN="$(stored AGENT_CONNECT_TOKEN "$HOME/.agent-connect/config.env")"
  if [ "$LEGACY_TOKEN" = "$TOKEN" ]; then
    # Only retire the legacy service when it uses this exact credential. A
    # different agent's default service must keep running beside this one.
    MIGRATE_DEFAULT=1
  elif [ -n "$LEGACY_TOKEN" ]; then
    say "legacy default has a different credential; its service is left unchanged"
    say "if that credential was revoked, stop and disable the old service after confirming it is obsolete (README: Legacy migration cleanup)"
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
  if [ "$INSTANCE" != default ]; then
    MANAGED="$MANAGED|AGENT_CONNECT_INSTANCE|AGENT_CONNECT_WORKSPACE"
  fi
  if [ -n "$ACP_CMD_KV" ] || [ "$ACP_CMD_CLEAR" -eq 1 ]; then
    MANAGED="$MANAGED|AGENT_CONNECT_ACP_COMMAND"
  fi
  # Same rule as the command: rewritten only when this run set one, so a re-run
  # without the flag keeps the URL and token already in the file.
  if [ -n "$ACP_URL_KV" ] || [ "$ACP_URL_CLEAR" -eq 1 ]; then
    MANAGED="$MANAGED|AGENT_CONNECT_ACP_URL|AGENT_CONNECT_ACP_TOKEN"
  elif [ -n "$ACP_TOKEN_KV" ]; then
    # Rotating the bearer alone: the URL is kept exactly as it is, and only the
    # credential beside it is replaced.
    MANAGED="$MANAGED|AGENT_CONNECT_ACP_TOKEN"
  fi
  [ "$COLLABORATORS_GIVEN" -eq 0 ] || MANAGED="$MANAGED|AGENT_CONNECT_ACP_COLLABORATORS"
  [ "$COLLAB_ROOMS_GIVEN" -eq 0 ] || MANAGED="$MANAGED|AGENT_CONNECT_ACP_COLLABORATOR_ROOMS"
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
    if [ "$INSTANCE" != default ]; then
      printf '%s\n' "AGENT_CONNECT_INSTANCE=\"$INSTANCE\""
      printf '%s\n' "AGENT_CONNECT_WORKSPACE=\"$APP_DIR/workspace\""
    fi
    [ -z "$ACP_KV" ] || printf '%s\n' "AGENT_CONNECT_ACP_AGENT=\"$ACP_AGENT\""
    [ -z "$ACP_CMD_KV" ] || printf '%s\n' "AGENT_CONNECT_ACP_COMMAND=\"$ACP_COMMAND\""
    [ -z "$ACP_URL_KV" ] || printf '%s\n' "AGENT_CONNECT_ACP_URL=\"$ACP_URL\""
    [ -z "$ACP_TOKEN_KV" ] || printf '%s\n' "AGENT_CONNECT_ACP_TOKEN=\"$ACP_TOKEN\""
    if [ "$COLLABORATORS_GIVEN" -eq 1 ] && [ -n "$COLLABORATORS" ]; then
      printf '%s\n' "AGENT_CONNECT_ACP_COLLABORATORS=\"$COLLABORATORS\""
    fi
    if [ "$COLLAB_ROOMS_GIVEN" -eq 1 ] && [ -n "$COLLAB_ROOMS" ]; then
      printf '%s\n' "AGENT_CONNECT_ACP_COLLABORATOR_ROOMS=\"$COLLAB_ROOMS\""
    fi
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
WS="\${AGENT_CONNECT_WORKSPACE:-$APP_DIR/workspace}"
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
  PLIST="$LA_DIR/$SERVICE_LABEL.plist"
  # Retire the old service before writing any new RunAtLoad/KeepAlive plist.
  # If unload fails, leave no second worker scheduled for the next login.
  if [ "$MIGRATE_DEFAULT" -eq 1 ] && [ -f "$LA_DIR/space.ag2.agent-connect.plist" ]; then
    say "moving this credential from the legacy default service to $INSTANCE"
    if launchctl print "gui/$(id -u)/space.ag2.agent-connect" >/dev/null 2>&1; then
      launchctl unload "$LA_DIR/space.ag2.agent-connect.plist"
    fi
    mv "$LA_DIR/space.ag2.agent-connect.plist" "$LA_DIR/space.ag2.agent-connect.plist.migrated"
  fi
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$SERVICE_LABEL</string>
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
  <!-- launchd SIGKILLs 5 s after SIGTERM without this; a killed worker cannot
       release its poller guard, and the next one stands by about 2.5 min. -->
  <key>ExitTimeOut</key><integer>30</integer>
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
  if [ "$MIGRATE_DEFAULT" -eq 1 ] && [ -f "$UNIT_DIR/agent-connect.service" ]; then
    say "moving this credential from the legacy default service to $INSTANCE"
    systemctl --user disable --now agent-connect.service
  fi
  cat > "$UNIT_DIR/$SERVICE_UNIT" <<UNIT
[Unit]
Description=s.space agent-connect worker ($INSTANCE)
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
  systemctl --user enable --now "$SERVICE_UNIT"
  # enable --now does not restart an already-running unit after a config change.
  systemctl --user restart "$SERVICE_UNIT"
  say "enabled. Logs: journalctl --user -u $SERVICE_UNIT -f"
else
  if [ "$MIGRATE_DEFAULT" -eq 1 ] && [ -f "$HOME/.agent-connect/workspace/.worker.pids" ]; then
    while read -r old_pid; do
      case "$old_pid" in ''|*[!0-9]*) continue ;; esac
      if kill -0 "$old_pid" 2>/dev/null; then
        echo 'install.sh: stop the legacy default worker before starting this same credential as a named instance.' >&2
        exit 1
      fi
    done < "$HOME/.agent-connect/workspace/.worker.pids"
  fi
  say "no service manager found — starting in the background (nohup)"
  env AGENT_CONNECT_CONFIG="$CONFIG" \
      nohup sh "$LAUNCHER" >"$APP_DIR/agent-connect.log" 2>&1 &
  say "started (pid $!). Logs: $APP_DIR/agent-connect.log"
fi

say "done. Your agent should appear in s.space shortly — @-mention it in an allowed room."
