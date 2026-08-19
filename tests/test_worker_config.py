"""The config file, asserted where an operator meets it: starting the Worker.

Most of this drives the real entry point as a child process — `python3 -m
agent_connect` with an environment scrubbed of every `AGENT_CONNECT_*` name —
because the claim being tested is exactly "it starts from a config file alone,
with no env setup", and a claim about starting is not testable by calling a
parser. What is asserted is what the operator sees: the startup lines on stdout,
the warnings on stderr, and the exit status.

The rest is the precedence rule, at the seam where it lives: the environment
wins over the file, and the file says so out loud rather than leaving someone to
wonder why the value they wrote did nothing.

Run: python3 tests/test_worker_config.py   (no dependencies — the `ollama`
adapter is a shim and nothing here contacts it)
"""
from __future__ import annotations

import _bootstrap  # noqa: F401 — puts the repo root on sys.path

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_connect.config import CONFIG_ENV, Config, accepts, locate, parse

ROOT = _bootstrap.ROOT
STARTED = "agent-connect worker:"

fails = 0


def check(cond, name):
    global fails
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        fails += 1


tmp = Path(tempfile.mkdtemp())
home = tmp / "home"
(home / ".agent-connect").mkdir(parents=True)
work = tmp / "work"
work.mkdir()

SETTINGS = f"""# written by hand, the way an operator would
AGENT_CONNECT_TOKEN=tok-from-the-file
AGENT_CONNECT_ADAPTER=ollama
AGENT_CONNECT_REPO={work}
AGENT_CONNECT_WORKSPACE={tmp}/ws
AGENT_CONNECT_POLL=0.05
"""

config = tmp / "config.env"
config.write_text(SETTINGS)
config.chmod(0o600)

_runs = 0


def start(*args, env=None, timeout=20.0):
    """Start the real Worker, wait until it is serving, and stop it.

    Returns `(exit status, everything it printed)`. A Worker that gets as far as
    serving Tasks never exits on its own, so it is stopped as soon as it says
    so; one that refuses to start is left to exit and report why.
    """
    global _runs
    _runs += 1
    log = tmp / f"run-{_runs}.log"
    # Unbuffered, because the child is stopped rather than allowed to exit and
    # a buffered `print` would die in the buffer with it.
    child = {"PATH": os.environ.get("PATH", ""), "HOME": str(home),
             "PYTHONUNBUFFERED": "1"}
    child.update(env or {})
    with open(log, "w") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_connect", *args],
            cwd=str(ROOT), env=child, stdout=sink, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if STARTED in log.read_text() or proc.poll() is not None:
                break
            time.sleep(0.05)
        if proc.poll() is None:
            proc.terminate()
        code = proc.wait(timeout=20)
    return code, log.read_text()


print("\n-- the Worker starts from a config file alone --")

code, said = start("--config", str(config))
check(STARTED in said, "with no environment at all, the Worker starts and serves")
check("adapter=ollama" in said, "on the adapter the file named")
check(f"repo={work}" in said, "in the working directory the file named")
check(f"ws={tmp}/ws" in said, "using the workspace the file named")
check(str(config) in said and "5 setting(s) applied" in said,
      "and it says which file it read and how much of it took effect")
check("tok-from-the-file" not in said,
      "without printing the token back out: a log is not a place for a credential")


print("\n-- the environment wins over the file, and the file says so --")

code, said = start("--config", str(config),
                   env={"AGENT_CONNECT_ADAPTER": "codex"})
check("adapter=codex" in said,
      "a setting exported in the shell is the setting that runs")
check("already set in the environment (AGENT_CONNECT_ADAPTER)" in said,
      "and the file names what it offered and did not get, so 'the file I "
      "edited had no effect' is answered before it is asked")
check("4 setting(s) applied" in said,
      "the rest of the file still applies — precedence is per setting, not per file")

# Precedence again at the seam itself, where it is cheap to be exhaustive.
env = {"AGENT_CONNECT_ADAPTER": "codex", "AGENT_CONNECT_REPO": "   "}
applied, overridden = parse(SETTINGS).apply(env)
check(env["AGENT_CONNECT_ADAPTER"] == "codex", "the environment's value is untouched")
check(overridden == ["AGENT_CONNECT_ADAPTER"], "and reported as overridden")
check(env["AGENT_CONNECT_REPO"] == str(work),
      "a variable set to whitespace is not a decision the operator made, so the "
      "file fills it")
check("AGENT_CONNECT_TOKEN" in applied and env["AGENT_CONNECT_TOKEN"] == "tok-from-the-file",
      "everything the environment was silent about comes from the file")


print("\n-- how the file is found --")

code, said = start(env={CONFIG_ENV: str(config)})
check(STARTED in said and "adapter=ollama" in said,
      f"{CONFIG_ENV} points at it, so a service unit needs no command line")

default = home / ".agent-connect" / "config.env"
default.write_text(SETTINGS)
default.chmod(0o600)
code, said = start()
check(STARTED in said and str(default) in said,
      "and with nothing said at all the default location is read, so "
      "`agent-connect` on its own is a complete start")

found, named = locate(explicit="/flag/wins", env={CONFIG_ENV: "/env/loses"})
check(str(found) == "/flag/wins" and named, "the flag beats the variable")
found, named = locate(env={CONFIG_ENV: "/env/wins"})
check(str(found) == "/env/wins" and named, "the variable beats the default")

code, said = start("--config", str(tmp / "nowhere.env"))
check(code != 0 and "no config file at" in said,
      "a config file named and missing stops the Worker: it holds the token, and "
      "starting without it produces something that runs, pulls nothing and looks "
      "healthy")

code, said = start("--help")
check(code == 0 and "--config PATH" in said, "--help says what the one flag is")
code, said = start("--nope")
check(code != 0 and "unknown argument" in said, "and an unknown argument is refused")


print("\n-- a config file is a settings file, not an environment --")

sneaky = tmp / "sneaky.env"
sneaky.write_text(SETTINGS + "PATH=/tmp/evil\nEDITOR=vim\n")
sneaky.chmod(0o600)
code, said = start("--config", str(sneaky))
check(STARTED in said, "an unknown key does not stop the Worker")
check("EDITOR" in said and "PATH" in said and "does not read" in said,
      "but is named on the way past — a misspelt setting looks exactly like a "
      "setting that did not work")
check(parse("PATH=/tmp/evil\n").values == {},
      "and it is not applied: a file that could set PATH would decide which "
      "`codex` binary runs")
check(accepts("AGENT_CONNECT_TOKEN") and accepts("REMOTE_TASK_URL")
      and accepts("OLLAMA_HOST") and not accepts("LD_PRELOAD"),
      "the three key families the Settings table documents, and nothing else")


print("\n-- the file holds a bearer token, so its permissions are checked --")

loose = tmp / "loose.env"
loose.write_text(SETTINGS)
loose.chmod(0o644)
code, said = start("--config", str(loose))
check(STARTED in said, "a world-readable config file is still loaded")
check("readable by other users" in said and "chmod 600" in said,
      "and complained about, with the command that fixes it")
code, said = start("--config", str(config))
check("readable by other users" not in said, "a 0600 file is complained about not at all")


print("\n-- the format --")

parsed = parse('# a comment\n\nAGENT_CONNECT_REPO = "/a path/with spaces" \n'
               'AGENT_CONNECT_ACP_COMMAND=my-agent --acp --flag=1\n'
               'not a setting line\n')
check(parsed.values["AGENT_CONNECT_REPO"] == "/a path/with spaces",
      "one matching pair of surrounding quotes comes off, and whitespace with it")
check(parsed.values["AGENT_CONNECT_ACP_COMMAND"] == "my-agent --acp --flag=1",
      "everything after the first `=` is the value, verbatim — a command line is "
      "a perfectly ordinary setting")
check(len(parsed.values) == 2, "comments, blank lines and prose are not settings")
check(Config().values == {} and parse("").values == {},
      "and an empty file is an empty file, not an error")


print("\n-- every documented setting can be written in it --")

readme = (ROOT / "README.md").read_text()
section = readme[readme.find("\n## Settings\n") + 1:]
section = section[:section.find("\n## ", 1)]
documented = sorted(set(re.findall(r"`(AGENT_CONNECT_[A-Z0-9_]+|OLLAMA_HOST)`", section)))
check(len(documented) >= 20, f"the Settings table was found ({len(documented)} keys)")
for name in documented:
    check(accepts(name), f"{name} may be written in the config file")

print("\n" + ("PASS — the config file green" if fails == 0 else f"FAIL — {fails} failing"))
raise SystemExit(1 if fails else 0)
