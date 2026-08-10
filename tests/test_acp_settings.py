"""Every setting the package reads is documented in README's Settings table.

One home for the settings, and this is what keeps it the only one. The failure
this prevents is the ordinary one: an Adapter grows a variable, the person who
added it documents it in the module docstring they were already editing, and the
operator — who reads the README — never learns it exists.

The check is deliberately dumb: find every `AGENT_CONNECT_*` name that appears
in the package source, and require each to appear in the README section headed
`## Settings`. A name in the README that no longer exists in code is *not* a
failure here; a removed setting still deserves a line saying it went away.

Run: python3 tests/test_acp_settings.py   (no dependencies — pure text)
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import re
from pathlib import Path

HERE = _bootstrap.ROOT
fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


def settings_section() -> str:
    text = (HERE / "README.md").read_text()
    start = text.find("\n## Settings\n")
    if start < 0:
        return ""
    rest = text[start + 1:]
    end = rest.find("\n## ", 1)
    return rest if end < 0 else rest[:end]


section = settings_section()
check(bool(section), "README has a `## Settings` section")
check("authoritative" in section,
      "and it says it is the authoritative list, so a reader knows not to hunt")

# Every AGENT_CONNECT_* name mentioned anywhere in the package: the ones read
# from the environment and the ones only named in a message to the operator.
found = set()
for path in sorted(HERE.glob("agent_connect/**/*.py")):
    for name in re.findall(r"AGENT_CONNECT_[A-Z0-9_]+", path.read_text()):
        # A trailing underscore is prose about the family ("the AGENT_CONNECT_*
        # style"), not a setting.
        if not name.endswith("_"):
            found.add(name)

check(len(found) >= 10, f"the scan found the package's settings ({len(found)} names)")

for name in sorted(found):
    check(name in section, f"{name} is documented in README § Settings")

# The settings this ticket introduced, named explicitly: a scan that silently
# found nothing would pass every assertion above.
for name in ("AGENT_CONNECT_ACP_AGENT", "AGENT_CONNECT_ACP_COMMAND",
             "AGENT_CONNECT_ACP_MODE", "AGENT_CONNECT_ACP_SKIP_AUTH_CHECK"):
    check(name in found, f"{name} is read by the package")

check("AGENT_CONNECT_ACP_BRIDGE_SPEC" in section,
      "the installer-only bridge-spec override is documented in the same place")

# The old second list is gone rather than left to rot: worker.py must not carry
# an `Env:` block of its own again.
worker_doc = (HERE / "agent_connect" / "worker.py").read_text()[:2000]
check("Env:" not in worker_doc,
      "worker.py no longer keeps a rival list of environment variables")
check("README.md" in worker_doc,
      "and points at the one place instead")

print("\n" + ("PASS — settings documented in one place" if fails == 0
              else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
