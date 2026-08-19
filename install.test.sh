#!/usr/bin/env sh
# install.test.sh — smoke tests for install.sh that don't mutate the system.
# Covers the decision-stable surface: syntax, arg parsing, required-flag gate,
# prereq detection, and the --no-start dry-run (worker + relay installs stubbed
# via fake pipx/bins on PATH; asserts the launcher is written with the
# dir-interface wiring). Fully offline — the PyPI flip removed the sparse-fetch
# step, so no network is needed.
#
#   sh install.test.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$HERE/install.sh"
fails=0
ok()   { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fails=$((fails+1)); }

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
#    succeeds and fake `agent-connect` + `ag2-sparrow` bins on PATH, so the
#    script exercises its real control flow without touching pip or PyPI.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for fake in pipx agent-connect ag2-sparrow; do
  cat > "$TMP/$fake" <<'FAKE'
#!/bin/sh
exit 0
FAKE
  chmod +x "$TMP/$fake"
done
out="$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token TESTTOK --adapter omnigent --no-start 2>&1)" || {
  printf '%s\n' "$out" | sed 's/^/    /'; bad "--no-start dry-run exited non-zero"; }
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
  grep -q "^AGENT_CONNECT_TOKEN=TESTTOK$" "$CONFIG" && ok "config.env carries the token" \
    || bad "config.env missing the token"
  grep -q "^AGENT_CONNECT_ADAPTER=omnigent$" "$CONFIG" && ok "config.env carries the adapter" \
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

# 5) launcher written, executable, and wired correctly
LAUNCHER="$TMP/.agent-connect/launch.sh"
if [ -x "$LAUNCHER" ]; then ok "launch.sh written + executable"; else bad "launch.sh missing"; fi
if [ -f "$LAUNCHER" ]; then
  if sh -n "$LAUNCHER"; then ok "launch.sh syntax clean"; else bad "launch.sh syntax"; fi
  for want in AGENT_CONNECT_TASK_DIR AGENT_CONNECT_RESULT_DIR AGENT_CONNECT_STATE_DIR \
              REMOTE_TASK_TOKEN REMOTE_TASK_URL; do
    grep -q "$want" "$LAUNCHER" && ok "launch.sh wires $want" || bad "launch.sh missing $want"
  done
  # resolved bins are interpolated at install time (absolute paths, not runtime lookups)
  grep -q "$TMP/ag2-sparrow" "$LAUNCHER" && ok "relay bin interpolated" || bad "relay bin not interpolated"
  grep -q "$TMP/agent-connect" "$LAUNCHER" && ok "worker bin interpolated" || bad "worker bin not interpolated"
  # the launcher must start BOTH processes: relay in background, worker via exec
  grep -q 'exec "' "$LAUNCHER" && ok "launcher execs the worker" || bad "launcher missing worker exec"
  # it reads the same config file the worker does, so nothing has to be exported
  grep -q "AGENT_CONNECT_CONFIG:-$CONFIG" "$LAUNCHER" \
    && ok "launch.sh defaults to the installed config file" \
    || bad "launch.sh does not read the config file"
  grep -q "TESTTOK" "$LAUNCHER" && bad "the token was baked into launch.sh" \
    || ok "and holds no token of its own"
fi

# 5a) the launcher's config reader, run: it must agree with the worker's own
#     parser about one file, or the relay and the worker disagree about the token.
LOADER="$TMP/loader.sh"
sed -n '/^export AGENT_CONNECT_CONFIG$/,/^fi$/p' "$LAUNCHER" > "$LOADER"
cat > "$TMP/loader.env" <<'CFG'
# a comment
AGENT_CONNECT_TOKEN=tok-from-the-file
  AGENT_CONNECT_REPO = "/a path/with spaces"
AGENT_CONNECT_ACP_COMMAND=my-agent --acp --flag=1
LD_PRELOAD=/tmp/evil.so
prose that is not a setting
CFG
LOADED="$(AGENT_CONNECT_CONFIG="$TMP/loader.env" AGENT_CONNECT_TOKEN=from-the-env sh -c '
  . "$1"
  echo "TOKEN=$AGENT_CONNECT_TOKEN"
  echo "REPO=$AGENT_CONNECT_REPO"
  echo "CMD=$AGENT_CONNECT_ACP_COMMAND"
  echo "PRELOAD=${LD_PRELOAD:-<unset>}"' sh "$LOADER")"
printf '%s\n' "$LOADED" | grep -q '^TOKEN=from-the-env$' \
  && ok "launcher: the environment wins over the config file" \
  || { printf '%s\n' "$LOADED" | sed 's/^/    /'; bad "launcher: the file overrode the environment"; }
printf '%s\n' "$LOADED" | grep -q '^REPO=/a path/with spaces$' \
  && ok "launcher: spaces and quotes are read the way the worker reads them" \
  || bad "launcher: quoted/spaced value not parsed like the worker's parser"
printf '%s\n' "$LOADED" | grep -q '^CMD=my-agent --acp --flag=1$' \
  && ok "launcher: everything after the first = is the value" \
  || bad "launcher: value truncated at a second ="
printf '%s\n' "$LOADED" | grep -q '^PRELOAD=<unset>$' \
  && ok "launcher: a config file cannot set a non-setting (LD_PRELOAD)" \
  || bad "launcher: the config file set a variable that is not a setting"

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

# 6) --sutando-workspace relay-only mode: launcher wired to the given
#    workspace, NO worker exec, worker install skipped
SWS="$TMP/sutando-ws"; mkdir -p "$SWS/tasks"
out=$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token TESTTOK --sutando-workspace "$SWS" --no-start 2>&1) || {
  printf '%s\n' "$out" | sed 's/^/    /'; bad "sutando-mode dry-run exited non-zero"; }
printf '%s\n' "$out" | grep -q "relay-only" && ok "sutando mode announces relay-only" || bad "sutando mode missing relay-only notice"
L="$TMP/.agent-connect/launch.sh"
if sh -n "$L"; then ok "sutando launch.sh syntax clean"; else bad "sutando launch.sh syntax"; fi
grep -q "$SWS/tasks" "$L" && ok "launcher wired to sutando tasks/" || bad "launcher missing sutando tasks dir"
grep -q "$SWS/results" "$L" && ok "launcher wired to sutando results/" || bad "launcher missing sutando results dir"
grep -q 'exec "'"$TMP"'/ag2-sparrow"' "$L" && ok "launcher execs the relay" || bad "launcher missing relay exec"
if grep -q "agent-connect\"$" "$L"; then bad "sutando launcher must NOT exec a worker (double-processing)"; else ok "no worker exec in sutando launcher"; fi
# bogus workspace path is refused early
if PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --sutando-workspace "$TMP/nope" --no-start >/dev/null 2>&1; then
  bad "nonexistent --sutando-workspace should fail"; else ok "nonexistent --sutando-workspace → refused"; fi
# existing dir that is NOT a Sutando workspace (no tasks/) is refused too —
# a typo like \$HOME must not install a relay wired to a dead queue
NOTWS="$TMP/not-a-workspace"; mkdir -p "$NOTWS"
if PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --sutando-workspace "$NOTWS" --no-start >/dev/null 2>&1; then
  bad "non-workspace dir should be refused"; else ok "existing non-workspace dir → refused (no tasks/)"; fi
# a RELATIVE workspace path is canonicalized before being persisted into the
# launcher (a literal relative path would resolve against the service's cwd)
RELWS_ABS="$TMP/rel-sutando/workspace"; mkdir -p "$RELWS_ABS/tasks"
( cd "$TMP/rel-sutando" && PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token T --sutando-workspace "workspace" --no-start >/dev/null 2>&1 )
if grep -q "AGENT_CONNECT_TASK_DIR=\"$RELWS_ABS/tasks\"" "$TMP/.agent-connect/launch.sh"; then
  ok "relative workspace canonicalized to absolute in launcher"
else
  grep "AGENT_CONNECT_TASK_DIR" "$TMP/.agent-connect/launch.sh" | sed 's/^/    /'
  bad "relative workspace persisted non-absolute"
fi

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
grep -q "^AGENT_CONNECT_ACP_AGENT=claude$" "$TMP/.agent-connect/config.env" \
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
grep -q "^AGENT_CONNECT_ACP_AGENT=gemini$" "$TMP/.agent-connect/config.env" \
  && ok "--acp-agent selects the preset" || bad "--acp-agent not wired through"
grep -q "claude-agent-acp" "$NPM_LOG" \
  && bad "the Claude bridge was installed for a non-Claude preset" \
  || ok "no bridge installed for a preset that does not use one"

# 10) the sparse-fetch path is gone for good (PyPI is the single source)
if grep -q "raw.githubusercontent.com" "$SCRIPT"; then
  bad "install.sh still sparse-fetches from raw.githubusercontent.com"
else
  ok "no sparse-fetch remains (PyPI single-source)"
fi

printf '\n%s\n' "$( [ "$fails" -eq 0 ] && echo 'PASS — all install.sh smoke tests green' || echo "FAIL — $fails failing" )"
[ "$fails" -eq 0 ]
