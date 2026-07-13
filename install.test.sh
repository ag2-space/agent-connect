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
fi

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

# 7) the sparse-fetch path is gone for good (PyPI is the single source)
if grep -q "raw.githubusercontent.com" "$SCRIPT"; then
  bad "install.sh still sparse-fetches from raw.githubusercontent.com"
else
  ok "no sparse-fetch remains (PyPI single-source)"
fi

printf '\n%s\n' "$( [ "$fails" -eq 0 ] && echo 'PASS — all install.sh smoke tests green' || echo "FAIL — $fails failing" )"
[ "$fails" -eq 0 ]
