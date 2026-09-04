#!/usr/bin/env sh
# install.test.sh — smoke tests for install.sh that don't mutate the system.
# Covers the decision-stable surface: syntax, arg parsing, required-flag gate,
# prereq detection, and the --no-start dry-run (the install stubbed via a fake
# pipx and fake bins on PATH). The load-bearing assertion is section 5's: the
# launcher starts ONE process. Fully offline — the PyPI flip removed the
# sparse-fetch step, so no network is needed.
#
#   sh install.test.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/install.sh"
fails=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fails=$((fails+1)); }

# One process, asserted the same way wherever a launcher is asserted: no line
# ends in a backgrounding `&`. Comments are stripped first — every launcher here
# explains at its head why it stopped starting a second process, and a sentence
# ending in "&" would otherwise read as the thing it is describing. `${n:-0}`
# because a missing file leaves `grep -c` with nothing on stdout, and `[ "" -eq
# 0 ]` is an error rather than an answer.
BACKGROUNDED='(^|[^&0-9>])&[[:space:]]*$'
no_background() {  # <file> <label>
  _n="$(grep -v '^[[:space:]]*#' "$1" | grep -cE "$BACKGROUNDED" || true)"
  if [ "${_n:-0}" -eq 0 ]; then
    ok "$2 starts nothing in the background"
  else
    grep -nE "$BACKGROUNDED" "$1" | sed 's/^/    /'
    bad "$2 backgrounds ${_n:-?} process(es) beside the worker"
  fi
}

# 1) syntax
if sh -n "$SCRIPT"; then ok "sh -n clean"; else bad "sh -n"; fi
if sh -n "$HERE/run-agent.sh" 2>/dev/null || bash -n "$HERE/run-agent.sh"; then
  ok "run-agent.sh syntax clean"; else bad "run-agent.sh syntax"; fi

# 2) missing --token exits 2
if sh "$SCRIPT" >/dev/null 2>&1; then bad "no-token should exit non-zero"; else
  rc=$?; [ "$rc" -eq 2 ] && ok "missing --token → exit 2" || bad "missing --token → exit $rc (want 2)"
fi

# 3) unknown arg exits 2
if sh "$SCRIPT" --token T --bogus >/dev/null 2>&1; then bad "unknown arg should fail"; else
  rc=$?; [ "$rc" -eq 2 ] && ok "unknown arg → exit 2" || bad "unknown arg → exit $rc"
fi

# 4) --no-start dry-run: stub the install layer with a fake `pipx` that
#    succeeds and a fake `agent-connect` bin on PATH, so the script exercises
#    its real control flow without touching pip or PyPI.
#
#    A fake `ag2-sparrow` goes on PATH too, and deliberately: every assertion
#    below that says the installer does not reach for the relay client is made
#    with the relay client sitting right there, findable by `command -v`.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for fake in agent-connect ag2-sparrow; do
  cat > "$TMP/$fake" <<'FAKE'
#!/bin/sh
exit 0
FAKE
  chmod +x "$TMP/$fake"
done
# pipx records what it was asked to install: "the relay client left the
# install path" is a statement about this log, not about the launcher that
# comes out the far end.
cat > "$TMP/pipx" <<'FAKE'
#!/bin/sh
[ -z "${TMP_PIPX_LOG:-}" ] || echo "pipx $*" >> "$TMP_PIPX_LOG"
exit 0
FAKE
chmod +x "$TMP/pipx"
PIPX_LOG="$TMP/pipx.log"; : > "$PIPX_LOG"
out="$(PATH="$TMP:$PATH" HOME="$TMP" TMP_PIPX_LOG="$PIPX_LOG" \
       sh "$SCRIPT" --token TESTTOK --adapter omnigent --no-start 2>&1)" || {
  printf '%s\n' "$out" | sed 's/^/    /'; bad "--no-start dry-run exited non-zero"; }
grep -q "agent-connect" "$PIPX_LOG" \
  && ok "the installer installs the worker" \
  || { sed 's/^/    /' "$PIPX_LOG"; bad "pipx was never asked for the worker"; }
grep -q "ag2-sparrow" "$PIPX_LOG" \
  && { sed 's/^/    /' "$PIPX_LOG"; bad "the installer still installs ag2-sparrow"; } \
  || ok "and installs no relay client beside it — the worker carries its own"
if printf '%s\n' "${out:-}" | grep -q "install complete (not started)"; then
  ok "--no-start prints run command, no launch"
else
  printf '%s\n' "${out:-}" | sed 's/^/    /'; bad "--no-start missing expected output"
fi

# 4b) the config file: every setting in one 0600 file, and the launcher reads it
CONFIG="$TMP/.agent-connect/config.env"
if [ -f "$CONFIG" ]; then ok "config.env written"; else bad "config.env missing"; fi
if [ -f "$CONFIG" ]; then
  MODE="$(ls -l "$CONFIG" | cut -c1-10)"
  case "$MODE" in
    -rw-------) ok "config.env is 0600 (it holds the bearer token)" ;;
    *) bad "config.env is $MODE, want -rw------- : the token would be world-readable" ;;
  esac
  grep -q '^AGENT_CONNECT_TOKEN="TESTTOK"$' "$CONFIG" && ok "config.env carries the token" \
    || bad "config.env missing the token"
  grep -q '^AGENT_CONNECT_ADAPTER="omnigent"$' "$CONFIG" && ok "config.env carries the adapter" \
    || bad "config.env missing the adapter"
  grep -q "^AGENT_CONNECT_REPO=" "$CONFIG" && ok "config.env carries the working directory" \
    || bad "config.env missing the working directory"
fi
printf '%s\n' "${out:-}" | grep -q "AGENT_CONNECT_CONFIG=$CONFIG" \
  && ok "the run command names the config file, not a token" \
  || bad "run command does not point at the config file"
printf '%s\n' "${out:-}" | grep -q "TESTTOK" \
  && bad "the run command still prints the bearer token" \
  || ok "and prints no bearer token at all"

# 5) launcher written, executable, and starting exactly ONE process
LAUNCHER="$TMP/.agent-connect/launch.sh"
if [ -x "$LAUNCHER" ]; then ok "launch.sh written + executable"; else bad "launch.sh missing"; fi
if [ -f "$LAUNCHER" ]; then
  if sh -n "$LAUNCHER"; then ok "launch.sh syntax clean"; else bad "launch.sh syntax"; fi
  # THE assertion of this file. The launcher used to start two long-pollers on
  # one bearer — the worker, which owns its relay client (workspace ADR 0001),
  # and `ag2-sparrow` beside it. They share the bearer without sharing a lease,
  # so whichever wins a given long-poll takes the task; when ag2-sparrow won it
  # wrote the task into a directory the worker no longer reads, and the person
  # who sent the message got no answer and no error. This is not a tidier shape
  # being asked for. It is a message that disappeared.
  no_background "$LAUNCHER" "launch.sh"
  execs="$(grep -c '^exec ' "$LAUNCHER" || true)"
  [ "${execs:-0}" -eq 1 ] && ok "and execs exactly one thing — the launch unit is a process" \
    || bad "launch.sh has $execs exec lines, want exactly 1"
  case "$(grep '^exec ' "$LAUNCHER")" in
    *agent-connect*) ok "and the one thing is the worker" ;;
    *) bad "launch.sh execs something other than the worker: $(grep '^exec ' "$LAUNCHER")" ;;
  esac
  grep -q "ag2-sparrow" "$LAUNCHER" \
    && bad "launch.sh still names ag2-sparrow" \
    || ok "it names no relay process at all — the worker carries the wire"
  # the dir-interface trio was ag2-sparrow's, and is read by nothing here
  for gone in AGENT_CONNECT_TASK_DIR AGENT_CONNECT_RESULT_DIR AGENT_CONNECT_STATE_DIR; do
    grep -q "$gone" "$LAUNCHER" && bad "launch.sh still wires $gone" \
      || ok "and does not wire $gone"
  done
  # resolved bins are interpolated at install time (absolute paths, not runtime lookups)
  grep -q "$TMP/agent-connect" "$LAUNCHER" && ok "worker bin interpolated" || bad "worker bin not interpolated"
  # it reads the same config file the worker does, so nothing has to be exported
  grep -q "AGENT_CONNECT_CONFIG:-$CONFIG" "$LAUNCHER" \
    && ok "launch.sh defaults to the installed config file" \
    || bad "launch.sh does not read the config file"
  grep -q "TESTTOK" "$LAUNCHER" && bad "the token was baked into launch.sh" \
    || ok "and holds no token of its own"
fi

# 5a) there is exactly ONE config parser, and the launcher asks it. The shell
#     loop that used to live here disagreed with agent_connect/config.py about
#     duplicated keys, CRLF and whitespace-only variables — the first of those
#     handed the relay client and the worker different tokens. The agreement is
#     now structural, and `tests/test_worker_config.py` runs both paths over one
#     fixture set to keep it that way.
grep -q -- "--export-config" "$LAUNCHER" \
  && ok "launch.sh gets its settings from the worker's own parser" \
  || bad "launch.sh does not use --export-config"
if grep -qE '^\s*while read .*_line|\$\{_line' "$LAUNCHER"; then
  bad "launch.sh has grown a config parser of its own again"
else
  ok "and carries no parser of its own"
fi

# 5b) the service definitions carry a path, never a bearer token — the whole
#     point of the config file, since a launchd plist is world-readable. Read
#     out of install.sh rather than produced: writing a real plist means
#     `launchctl load`, and these tests do not touch the machine they run on.
PLIST_BLOCK="$(sed -n '/<key>EnvironmentVariables<\/key>/,/<\/dict>/p' "$SCRIPT")"
case "$PLIST_BLOCK" in
  *AGENT_CONNECT_CONFIG*) ok "the launchd plist passes AGENT_CONNECT_CONFIG" ;;
  *) bad "the launchd plist does not point at the config file" ;;
esac
case "$PLIST_BLOCK" in
  *TOKEN*) bad "the launchd plist still carries the bearer token" ;;
  *) ok "and carries no token (a plist is world-readable by default)" ;;
esac
UNIT_BLOCK="$(sed -n '/^\[Service\]/,/^ExecStart=/p' "$SCRIPT")"
case "$UNIT_BLOCK" in
  *AGENT_CONNECT_CONFIG*) ok "the systemd unit passes AGENT_CONNECT_CONFIG" ;;
  *) bad "the systemd unit does not point at the config file" ;;
esac
case "$UNIT_BLOCK" in
  *TOKEN*) bad "the systemd unit still carries the bearer token" ;;
  *) ok "and carries no token either" ;;
esac

# 5c) a re-run must not eat a setting somebody added by hand. This is the
#     documented `curl … | sh` path and people re-run it.
printf '\nAGENT_CONNECT_TURN_TIMEOUT=1800\n' >> "$CONFIG"
out="$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token TESTTOK2 --adapter codex --no-start 2>&1)" || \
  bad "re-run dry-run exited non-zero"
grep -q "AGENT_CONNECT_TURN_TIMEOUT=1800" "$CONFIG" \
  && ok "a re-run keeps a hand-added setting" \
  || { sed 's/^/    /' "$CONFIG"; bad "a re-run discarded a hand-added setting"; }
grep -q '^AGENT_CONNECT_TOKEN="TESTTOK2"$' "$CONFIG" \
  && ok "while the settings the installer manages are updated" \
  || bad "re-run did not update the managed settings"
[ -f "$CONFIG.bak" ] && ok "and the previous config is kept beside it" \
  || bad "no backup of the previous config"
case "$(ls -l "$CONFIG.bak" | cut -c1-10)" in
  -rw-------) ok "the backup is 0600 too — it holds the old token" ;;
  *) bad "the config backup is world-readable" ;;
esac

# 6) --sutando-workspace is gone, and says so by name.
#    It existed to install ag2-sparrow against an already-running Sutando's
#    workspace: relay only, no worker, dirs pointed at that Sutando's queue.
#    With the wire inside the Worker there is nothing in this repository to
#    install for that — ag2-sparrow is another package in another repo, with
#    its own install line, and coexistence stays permanent-normal (the
#    transport-seam spec calls it that).
#    The flag is refused BY NAME rather than falling through to "unknown arg",
#    because it is written down in scripts and support threads and the operator
#    who typed it needs the sentence, not the parser's shrug.
# No fixture workspace is made: the flag is refused before any path is looked
# at, and building one would imply a shape requirement that no longer exists.
if out=$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --sutando-workspace "$TMP/anything" --no-start 2>&1); then
  bad "--sutando-workspace should be refused, not accepted"
else
  rc=$?
  [ "$rc" -eq 2 ] && ok "--sutando-workspace → exit 2" || bad "--sutando-workspace → exit $rc (want 2)"
fi
printf '%s\n' "${out:-}" | grep -q "ag2-sparrow" \
  && ok "and the refusal names where a relay client comes from now" \
  || { printf '%s\n' "${out:-}" | sed 's/^/    /'; bad "the refusal does not say what to do instead"; }
printf '%s\n' "${out:-}" | grep -q -- "--sutando-workspace" \
  && ok "and names the flag the operator actually typed" \
  || bad "the refusal does not name --sutando-workspace"
# The env half matters more than the flag: a removed flag at least fails, while
# an ignored AGENT_CONNECT_SUTANDO_WORKSPACE would install a worker for somebody
# who asked for the opposite and say nothing about it.
if out=$(PATH="$TMP:$PATH" HOME="$TMP" AGENT_CONNECT_SUTANDO_WORKSPACE="$TMP/anything" \
         sh "$SCRIPT" --token T --no-start 2>&1); then
  bad "AGENT_CONNECT_SUTANDO_WORKSPACE is silently ignored"
else
  rc=$?
  [ "$rc" -eq 2 ] && ok "AGENT_CONNECT_SUTANDO_WORKSPACE → exit 2 as well" \
    || bad "AGENT_CONNECT_SUTANDO_WORKSPACE → exit $rc (want 2)"
fi

# 6b) the retired env surface is retired everywhere this repo ships it, not
#     merely unused by the launcher. A documented variable that nothing reads is
#     an afternoon: the README said in one place that the trio is "read by
#     nothing in this package" while install.sh wired it in another.
#
#     The scripts must not name it at all, comments aside — a comment is where
#     they explain why they stopped. (That ag2-sparrow is not INSTALLED is section
#     4's pipx log rather than a grep here: install.sh still says the package's
#     name, in the sentence that tells a --sutando-workspace operator where it
#     went.) The README must not SET the trio — no export line, no config-file
#     example — and must still name it once, as retired: a variable that
#     vanishes from the docs without a word leaves whoever has it in a launcher
#     of their own with nothing to search for.
for f in install.sh run-agent.sh; do
  still_wired="$(grep -v '^[[:space:]]*#' "$HERE/$f" \
          | grep -oE 'AGENT_CONNECT_(TASK|RESULT|STATE)_DIR|RELAY_PIP_SPEC|RELAY_BIN' \
          | sort -u | tr '\n' ' ' || true)"
  [ -z "$still_wired" ] && ok "$f wires none of the retired dir/relay surface" \
    || bad "$f still wires: $still_wired"
done
set_in_readme="$(grep -oE 'AGENT_CONNECT_(TASK|RESULT|STATE)_DIR=' "$HERE/README.md" | sort -u | tr '\n' ' ' || true)"
[ -z "$set_in_readme" ] && ok "README.md sets none of the retired dir variables" \
  || bad "README.md still shows a value for: $set_in_readme"
# The heading is "**Retired: \`AGENT_CONNECT_TASK_DIR\`, …**"; -F because the
# backtick and the asterisks are otherwise a small quoting puzzle for no gain.
grep -qF 'Retired: `AGENT_CONNECT_TASK_DIR`' "$HERE/README.md" \
  && ok "and says outright that they are retired" \
  || bad "README.md drops the retired variables without telling anyone"

# 6c) run-agent.sh — the from-a-checkout launcher — is the same one process.
#     It is the script the README's Quick start had to warn people away from,
#     which is a warning that only exists because the script started two.
no_background "$HERE/run-agent.sh" "run-agent.sh"
grep -v '^[[:space:]]*#' "$HERE/run-agent.sh" | grep -q "ag2-sparrow" \
  && bad "run-agent.sh still starts ag2-sparrow" \
  || ok "and starts no relay client of its own"

# 7) working-dir defaults + warnings
out=$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --no-start 2>&1) || bad "default-repo dry-run failed"
printf '%s\n' "$out" | grep -q "agent working directory: $TMP/agents" && ok "default working dir = ~/agents, printed loudly" || bad "missing ~/agents default print"
[ -d "$TMP/agents" ] && ok "default working dir created" || bad "~/agents not created"
out=$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --repo "$TMP/Documents/proj" --no-start 2>&1) || true
printf '%s\n' "$out" | grep -q "privacy-protected" && ok "TCC-protected --repo warns" || bad "missing TCC warning"
out=$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --repo "$TMP/elsewhere" --no-start 2>&1) || true
printf '%s\n' "$out" | grep -q "privacy-protected" && bad "false TCC warning on safe path" || ok "no TCC warning on safe path"

# 8) the ACP bridge is PINNED, to the same version the package believes in.
#    This is the review point: the bridge renamed itself and moves through major
#    versions fast, so an unpinned or drifting spec is the failure to catch here
#    rather than in a room six months from now.
SPEC="$(sed -n 's/^ACP_BRIDGE_SPEC="\${AGENT_CONNECT_ACP_BRIDGE_SPEC:-\(.*\)}"$/\1/p' "$SCRIPT")"
case "$SPEC" in
  @agentclientprotocol/claude-agent-acp@[0-9]*.[0-9]*.[0-9]*)
    ok "installer pins the bridge to an exact version ($SPEC)" ;;
  *) bad "bridge spec is not an exact pin: '${SPEC:-<not found>}'" ;;
esac
case "$SPEC" in
  *latest*|*"^"*|*"~"*|*">"*) bad "bridge spec is a range, not a pin: $SPEC" ;;
  *) ok "no range/latest in the bridge spec" ;;
esac
# the package name that was renamed away from must not come back
if grep -q "claude-code-acp" "$SCRIPT"; then
  bad "install.sh still names the OLD bridge package (claude-code-acp)"
else
  ok "installer uses the current bridge package name"
fi
# one pinned version, agreed by the installer and the adapter that runs it
PY_SPEC="$(sed -n 's/^BRIDGE_SPEC = f"{BRIDGE_PACKAGE}@{BRIDGE_VERSION}"$/&/p' "$HERE/agent_connect/adapters/acp.py")"
PY_PKG="$(sed -n 's/^BRIDGE_PACKAGE = "\(.*\)"$/\1/p' "$HERE/agent_connect/adapters/acp.py")"
PY_VER="$(sed -n 's/^BRIDGE_VERSION = "\(.*\)"$/\1/p' "$HERE/agent_connect/adapters/acp.py")"
if [ -n "$PY_SPEC" ] && [ "$SPEC" = "$PY_PKG@$PY_VER" ]; then
  ok "installer and adapter pin the SAME bridge version ($PY_VER)"
else
  bad "pin drift: install.sh has '$SPEC', acp.py has '$PY_PKG@$PY_VER'"
fi

# 9) --adapter acp: the preset name is wired through to the worker, and the
#    bridge install is attempted with the pinned spec (npm stubbed, logged).
cat > "$TMP/npm" <<'FAKE'
#!/bin/sh
echo "npm $*" >> "$TMP_NPM_LOG"
exit 0
FAKE
chmod +x "$TMP/npm"
NPM_LOG="$TMP/npm.log"; : > "$NPM_LOG"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --no-start 2>&1) || bad "acp dry-run failed"
grep -q '^AGENT_CONNECT_ACP_AGENT="claude"$' "$TMP/.agent-connect/config.env" \
  && ok "the config file names the preset agent" || bad "config.env missing AGENT_CONNECT_ACP_AGENT"
grep -q "install -g $SPEC" "$NPM_LOG" \
  && ok "installer installs the pinned bridge via npm" || {
    sed 's/^/    /' "$NPM_LOG"; bad "npm was not asked for the pinned bridge"; }
printf '%s\n' "$out" | grep -q "never opens a terminal" \
  && ok "acp install tells the operator to log in themselves" || bad "missing login-yourself notice"
# a non-default preset does not silently get the Claude bridge installed
: > "$NPM_LOG"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-agent gemini --no-start 2>&1) || bad "acp gemini dry-run failed"
grep -q '^AGENT_CONNECT_ACP_AGENT="gemini"$' "$TMP/.agent-connect/config.env" \
  && ok "--acp-agent selects the preset" || bad "--acp-agent not wired through"
grep -q "claude-agent-acp" "$NPM_LOG" \
  && bad "the Claude bridge was installed for a non-Claude preset" \
  || ok "no bridge installed for a preset that does not use one"

# 9b) --acp-command: written through, preset name defaults to a non-preset so
#     no bridge is fetched, and a re-run without the flag keeps the command.
: > "$NPM_LOG"
rm -f "$TMP/.agent-connect/config.env" "$TMP/.agent-connect/config.env.bak"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-command "my-agent --acp" --no-start 2>&1) \
  || bad "acp --acp-command dry-run failed"
ACPCFG="$TMP/.agent-connect/config.env"
grep -q '^AGENT_CONNECT_ACP_COMMAND="my-agent --acp"$' "$ACPCFG" \
  && ok "--acp-command is written to config.env" \
  || { sed 's/^/    /' "$ACPCFG"; bad "--acp-command not written"; }
grep -q '^AGENT_CONNECT_ACP_AGENT="custom"$' "$ACPCFG" \
  && ok "and the preset name defaults to a non-preset, so no bridge is implied" \
  || bad "--acp-command did not default AGENT_CONNECT_ACP_AGENT to custom"
grep -q "claude-agent-acp" "$NPM_LOG" \
  && { sed 's/^/    /' "$NPM_LOG"; bad "the Claude bridge was installed despite an explicit command"; } \
  || ok "and no bridge is downloaded for a command-supplied agent"

# The regression this flag must not introduce: a re-run without it wiping the
# command, leaving the adapter on the claude preset.
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T2 --adapter acp --no-start 2>&1) \
  || bad "acp re-run without --acp-command failed"
grep -q '^AGENT_CONNECT_ACP_COMMAND="my-agent --acp"$' "$ACPCFG" \
  && ok "a re-run WITHOUT --acp-command keeps the existing command" \
  || { sed 's/^/    /' "$ACPCFG"; bad "a re-run without --acp-command wiped the command"; }
grep -q '^AGENT_CONNECT_TOKEN="T2"$' "$ACPCFG" \
  && ok "while the managed settings still update around it" \
  || bad "re-run did not update the token"

# 9c) --acp-url: the dialled door. Written through with its token, kept across a
#     re-run the same way a command is, and refused outright beside one.
: > "$NPM_LOG"
rm -f "$TMP/.agent-connect/config.env" "$TMP/.agent-connect/config.env.bak"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-url "ws://127.0.0.1:8802/acp" \
         --acp-token "listener-secret" --no-start 2>&1) \
  || bad "acp --acp-url dry-run failed"
grep -q '^AGENT_CONNECT_ACP_URL="ws://127.0.0.1:8802/acp"$' "$ACPCFG" \
  && ok "--acp-url is written to config.env" \
  || { sed 's/^/    /' "$ACPCFG"; bad "--acp-url not written"; }
grep -q '^AGENT_CONNECT_ACP_TOKEN="listener-secret"$' "$ACPCFG" \
  && ok "and its bearer goes with it, into the 0600 file that already holds one" \
  || bad "--acp-token not written"

out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T3 --adapter acp --no-start 2>&1) \
  || bad "acp re-run without --acp-url failed"
grep -q '^AGENT_CONNECT_ACP_URL="ws://127.0.0.1:8802/acp"$' "$ACPCFG" \
  && ok "a re-run WITHOUT --acp-url keeps the dialled door" \
  || { sed 's/^/    /' "$ACPCFG"; bad "a re-run without --acp-url wiped the URL"; }

if PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --adapter acp \
     --acp-url "ws://127.0.0.1:8802/acp" --acp-command "my-agent --acp" \
     --no-start >/dev/null 2>&1; then
  bad "--acp-url beside --acp-command was accepted; they name two different agents"
else
  ok "--acp-url and --acp-command together are refused, not silently resolved"
fi

# A run that DOES supply one rewrites it, exactly once.
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T3 --adapter acp --acp-command "other-agent --acp" --no-start 2>&1) \
  || bad "acp re-run with a new --acp-command failed"
grep -q '^AGENT_CONNECT_ACP_COMMAND="other-agent --acp"$' "$ACPCFG" \
  && ok "a re-run WITH --acp-command replaces it" || bad "--acp-command did not replace the old value"
n_cmd="$(grep -c '^AGENT_CONNECT_ACP_COMMAND=' "$ACPCFG" || true)"
[ "${n_cmd:-0}" -eq 1 ] \
  && ok "and leaves exactly one of it (no duplicate for the reader to disagree over)" \
  || { sed 's/^/    /' "$ACPCFG"; bad "config.env has $n_cmd AGENT_CONNECT_ACP_COMMAND lines"; }

# An explicit --acp-agent still wins over the implied `custom`.
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-command "x --acp" --acp-agent gemini --no-start 2>&1) \
  || bad "acp --acp-command with explicit --acp-agent failed"
grep -q '^AGENT_CONNECT_ACP_AGENT="gemini"$' "$ACPCFG" \
  && ok "an explicit --acp-agent still wins over the implied 'custom'" \
  || bad "explicit --acp-agent was overridden by the implied default"

# A stored command outranks any preset at runtime, so naming one has to clear
# it — otherwise the install reports `claude` and runs the custom agent.
rm -f "$TMP/.agent-connect/config.env" "$TMP/.agent-connect/config.env.bak"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-command "my-agent --acp" --no-start 2>&1) \
  || bad "seed run for the preset-transition case failed"
: > "$NPM_LOG"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-agent claude --no-start 2>&1) \
  || bad "switching from a custom command back to a preset failed"
grep -q '^AGENT_CONNECT_ACP_COMMAND=' "$ACPCFG" \
  && { sed 's/^/    /' "$ACPCFG"; bad "an explicit --acp-agent left the stored command in place — the preset is silently overridden"; } \
  || ok "an explicit --acp-agent clears a stored command, so the preset actually decides"
grep -q '^AGENT_CONNECT_ACP_AGENT="claude"$' "$ACPCFG" \
  && ok "and the named preset is what the config records" || bad "the named preset was not recorded"
grep -q "install -g $SPEC" "$NPM_LOG" \
  && ok "and the bridge that preset needs is installed" || bad "no bridge installed for the newly named preset"
printf '%s\n' "$out" | grep -q "replaces the AGENT_CONNECT_ACP_COMMAND" \
  && ok "and the operator is told the command was dropped, not left to find out" \
  || { printf '%s\n' "$out" | sed 's/^/    /'; bad "clearing the command was silent"; }

# ...while a plain re-run changes nothing.
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-command "my-agent --acp" --no-start 2>&1) \
  || bad "re-seed failed"
: > "$NPM_LOG"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T4 --adapter acp --no-start 2>&1) || bad "plain re-run failed"
grep -q '^AGENT_CONNECT_ACP_COMMAND="my-agent --acp"$' "$ACPCFG" \
  && ok "a re-run naming neither flag keeps the command" || bad "a plain re-run dropped the command"
grep -q '^AGENT_CONNECT_ACP_AGENT="custom"$' "$ACPCFG" \
  && ok "and keeps the agent name with it, rather than reverting it to the claude preset" \
  || { sed 's/^/    /' "$ACPCFG"; bad "a plain re-run re-pointed AGENT_CONNECT_ACP_AGENT"; }
grep -q "claude-agent-acp" "$NPM_LOG" \
  && { sed 's/^/    /' "$NPM_LOG"; bad "a plain re-run downloaded the Claude bridge for a custom-command install"; } \
  || ok "and downloads no bridge it will not use"

# An install for another adapter must not rewrite ACP settings on its way past.
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter acp --acp-command "my-agent --acp" --no-start 2>&1) \
  || bad "seed run for the non-acp-adapter case failed"
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter codex --acp-agent claude --no-start 2>&1) \
  || bad "codex re-run with --acp-agent failed"
grep -q '^AGENT_CONNECT_ACP_COMMAND="my-agent --acp"$' "$ACPCFG" \
  && ok "a non-acp adapter leaves a stored ACP command alone" \
  || { sed 's/^/    /' "$ACPCFG"; bad "--adapter codex --acp-agent deleted the stored ACP command"; }
printf '%s\n' "$out" | grep -q -- "--acp-agent applies only to --adapter acp" \
  && ok "and says the flag was ignored rather than dropping it silently" \
  || { printf '%s\n' "$out" | sed 's/^/    /'; bad "--acp-agent on a non-acp adapter was silently ignored"; }

# 9c) values reach config.env byte for byte. echo is implementation-defined for
#     backslashes; under dash \t and \n become a real tab and newline, and a
#     newline splits one setting into two. Run under each shell we can find.
BS_CMD='my-agent --re \n --tab \t --trunc \c --path C:\\tools\\a'
for SHBIN in sh dash bash; do
  command -v "$SHBIN" >/dev/null 2>&1 || continue
  rm -f "$TMP/.agent-connect/config.env" "$TMP/.agent-connect/config.env.bak"
  out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
        "$SHBIN" "$SCRIPT" --token "tok\tone" --adapter acp --acp-command "$BS_CMD" --no-start 2>&1) \
    || bad "[$SHBIN] backslash round-trip run failed"
  grep -Fqx "AGENT_CONNECT_ACP_COMMAND=\"$BS_CMD\"" "$ACPCFG" \
    && ok "[$SHBIN] a command containing backslashes reaches config.env unchanged" \
    || { sed 's/^/    /' "$ACPCFG"; bad "[$SHBIN] backslashes in --acp-command were rewritten"; }
  grep -Fqx 'AGENT_CONNECT_TOKEN="tok\tone"' "$ACPCFG" \
    && ok "[$SHBIN] and so does a token — every value takes the same path, not just the ACP one" \
    || { sed 's/^/    /' "$ACPCFG"; bad "[$SHBIN] backslashes in the token were rewritten"; }
  n="$(grep -c '^AGENT_CONNECT_ACP_COMMAND=' "$ACPCFG" || true)"
  [ "${n:-0}" -eq 1 ] \
    && ok "[$SHBIN] and it is still one line, not split into two settings" \
    || { sed 's/^/    /' "$ACPCFG"; bad "[$SHBIN] the command became $n lines"; }
done

# A value carrying a real newline is refused rather than written and misread.
if PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token "$(printf 'a\nb')" --no-start >/dev/null 2>&1; then
  bad "a token containing a newline should be refused"
else
  rc=$?
  [ "$rc" -eq 2 ] && ok "a value containing a newline is refused, with exit 2" \
                  || bad "newline token → exit $rc (want 2)"
fi

# The flag is meaningless without --adapter acp, and says so rather than vanishing.
out=$(PATH="$TMP:$PATH" HOME="$TMP" TMP_NPM_LOG="$NPM_LOG" \
      sh "$SCRIPT" --token T --adapter codex --acp-command "x --acp" --no-start 2>&1) \
  || bad "codex + --acp-command dry-run failed"
printf '%s\n' "$out" | grep -q "applies only to --adapter acp" \
  && ok "--acp-command with a non-acp adapter warns instead of silently doing nothing" \
  || { printf '%s\n' "$out" | sed 's/^/    /'; bad "no warning for --acp-command on a non-acp adapter"; }

# 10) the sparse-fetch path is gone for good (PyPI is the single source)
if grep -q "raw.githubusercontent.com" "$SCRIPT"; then
  bad "install.sh still sparse-fetches from raw.githubusercontent.com"
else
  ok "no sparse-fetch remains (PyPI single-source)"
fi

printf '\n%s\n' "$( [ "$fails" -eq 0 ] && echo 'PASS — all install.sh smoke tests green' || echo "FAIL — $fails failing" )"
[ "$fails" -eq 0 ]
