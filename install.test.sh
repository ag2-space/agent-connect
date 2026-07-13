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

# 6) the sparse-fetch path is gone for good (PyPI is the single source)
if grep -q "raw.githubusercontent.com" "$SCRIPT"; then
  bad "install.sh still sparse-fetches from raw.githubusercontent.com"
else
  ok "no sparse-fetch remains (PyPI single-source)"
fi

printf '\n%s\n' "$( [ "$fails" -eq 0 ] && echo 'PASS — all install.sh smoke tests green' || echo "FAIL — $fails failing" )"
[ "$fails" -eq 0 ]
