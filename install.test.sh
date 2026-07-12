#!/usr/bin/env sh
# install.test.sh — smoke tests for install.sh that don't mutate the system.
# Covers the decision-stable surface: syntax, arg parsing, required-flag gate,
# prereq detection, --no-start dry-run, and relay-file URL validity. Does NOT
# install the worker or start a service (those need a clean box / the hosting
# decision).
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

# 2) missing --token exits 2
if sh "$SCRIPT" >/dev/null 2>&1; then bad "no-token should exit non-zero"; else
  rc=$?; [ "$rc" -eq 2 ] && ok "missing --token → exit 2" || bad "missing --token → exit $rc (want 2)"
fi

# 3) unknown arg exits 2
if sh "$SCRIPT" --token T --bogus >/dev/null 2>&1; then bad "unknown arg should fail"; else
  rc=$?; [ "$rc" -eq 2 ] && ok "unknown arg → exit 2" || bad "unknown arg → exit $rc"
fi

# 4) --no-start dry-run: install worker step is stubbed by pointing the pip spec
#    at a local no-op wheel dir that pip will reject fast; instead we validate the
#    dry-run PRINTS the run command and never launches a service. To avoid a real
#    install we intercept by shadowing python3-pip via PATH: provide a fake
#    `pipx` that succeeds and a fake `agent-connect` on PATH.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/pipx" <<'FAKE'
#!/bin/sh
exit 0
FAKE
cat > "$TMP/agent-connect" <<'FAKE'
#!/bin/sh
echo "fake-worker"; exit 0
FAKE
chmod +x "$TMP/pipx" "$TMP/agent-connect"
# RELAY_RAW_BASE stays real so the fetch step is exercised against live URLs.
out="$(PATH="$TMP:$PATH" HOME="$TMP" sh "$SCRIPT" --token TESTTOK --adapter omnigent --no-start 2>&1)" || {
  printf '%s\n' "$out" | sed 's/^/    /'; bad "--no-start dry-run exited non-zero"; }
if printf '%s\n' "${out:-}" | grep -q "install complete (not started)"; then
  ok "--no-start prints run command, no launch"
else
  printf '%s\n' "${out:-}" | sed 's/^/    /'; bad "--no-start missing expected output"
fi
# the 6 relay files should have landed in the fake HOME
missing=0
for f in remote-gateway-bridge.py workspace_default.py task_archive.py \
         local_task_protocol.py result_markers.py send_allowlist.py; do
  [ -f "$TMP/.sutando-relay-client/$f" ] || { missing=$((missing+1)); }
done
[ "$missing" -eq 0 ] && ok "relay client + 5 deps fetched" || bad "$missing relay files missing"
# the compat shim should exist too
[ -f "$TMP/.sutando-relay-client/remote-relay-bridge.py" ] && ok "compat shim written" || bad "compat shim missing"

printf '\n%s\n' "$( [ "$fails" -eq 0 ] && echo 'PASS — all install.sh smoke tests green' || echo "FAIL — $fails failing" )"
[ "$fails" -eq 0 ]
