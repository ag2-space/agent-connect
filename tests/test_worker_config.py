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

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from _taskqueue import child_env
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

# The token is written the way the Agent Portal issues it: the gateway travels
# inside the credential, because there is no default gateway anywhere in this
# system to fall back on.
SETTINGS = f"""# written by hand, the way an operator would
AGENT_CONNECT_TOKEN=http://127.0.0.1:9/relay|tok-from-the-file
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
    # The credential the child needs comes out of the config file under test in
    # most of these, so `child_env`'s own is dropped unless a case wants it.
    child = child_env(HOME=str(home))
    child.pop("AGENT_CONNECT_TOKEN")
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
check("AGENT_CONNECT_TOKEN" in applied
      and env["AGENT_CONNECT_TOKEN"] == "http://127.0.0.1:9/relay|tok-from-the-file",
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


print("\n-- one file, one answer: the shell reads it through the same parser --")

# The launchers used to carry a `KEY=value` loop of their own. Within a day it
# had drifted from this module in three ways, the worst of which handed the
# relay client and the Worker *different tokens* out of one duplicated line.
# There is now one parser and the shell asks it (`--export-config`), so the only
# thing left that could go wrong is the shell quoting of the answer. These
# fixtures are the three divergences plus every shape that quoting can break,
# and each one is put through BOTH paths and compared.

WATCHED = ("AGENT_CONNECT_", "REMOTE_TASK_")
READ_BACK = (
    "import os, json; "
    "print(json.dumps({k: v for k, v in os.environ.items() "
    "if (k.startswith(('AGENT_CONNECT_', 'REMOTE_TASK_')) or k == 'OLLAMA_HOST') "
    "and k != 'AGENT_CONNECT_CONFIG'}))"
)


def through_the_shell(path, base):
    """What a launcher's environment holds after `eval $(--export-config)`."""
    # The fixture under test is what supplies the settings, so `child_env`'s
    # own credential is dropped: it would show up in the read-back and make
    # every comparison disagree about a variable neither reader read.
    child = child_env()
    child.pop("AGENT_CONNECT_TOKEN")
    child.update(base)
    script = (
        '_cfg="$(python3 -m agent_connect --config "$1" --export-config)" || exit 1\n'
        'eval "$_cfg"\n'
        'exec python3 -c "$2"\n'
    )
    out = subprocess.run(
        ["sh", "-c", script, "sh", str(path), READ_BACK],
        cwd=str(ROOT), env=child, capture_output=True, text=True, timeout=60,
    )
    return json.loads(out.stdout or "{}")


def through_python(path, base):
    """What `main()` would leave in the environment, for the same file."""
    env = dict(base)
    parse(Path(path).read_text(), Path(path)).apply(env)
    return {k: v for k, v in env.items()
            if (k.startswith(WATCHED) or k == "OLLAMA_HOST")
            and k != "AGENT_CONNECT_CONFIG"}


FIXTURES = {
    "a key written twice": (
        b"AGENT_CONNECT_TOKEN=first\nAGENT_CONNECT_TOKEN=second\n", {}),
    "CRLF line endings": (
        b"AGENT_CONNECT_TOKEN=tok\r\nAGENT_CONNECT_REPO=/repo\r\n", {}),
    "an environment variable set to whitespace": (
        b"AGENT_CONNECT_REPO=/from-the-file\n", {"AGENT_CONNECT_REPO": "   "}),
    "an environment variable genuinely set": (
        b"AGENT_CONNECT_REPO=/from-the-file\n", {"AGENT_CONNECT_REPO": "/exported"}),
    "spaces around the equals sign": (
        b"  AGENT_CONNECT_REPO = /a/repo  \n", {}),
    "a quoted value with spaces": (
        b'AGENT_CONNECT_REPO="/a path/with spaces"\n', {}),
    "a value containing more equals signs": (
        b"AGENT_CONNECT_ACP_COMMAND=my-agent --acp --flag=1\n", {}),
    "a value containing a single quote": (
        b"AGENT_CONNECT_REPO=/tmp/o'brien\n", {}),
    "a value containing a dollar sign and a backtick": (
        b"AGENT_CONNECT_TOKEN=$(whoami)`id`\n", {}),
    "a key that is not a setting": (
        b"AGENT_CONNECT_TOKEN=tok\nLD_PRELOAD=/tmp/evil.so\nPATH=/tmp/evil\n", {}),
    "an empty file": (b"", {}),
    "comments and prose only": (b"# nothing here\n\nnot a setting line\n", {}),
}

for name, (raw, base) in FIXTURES.items():
    fixture = tmp / ("fixture-" + name.replace(" ", "-") + ".env")
    fixture.write_bytes(raw)
    fixture.chmod(0o600)
    shell = through_the_shell(fixture, base)
    python = through_python(fixture, base)
    check(shell == python,
          f"both readers agree on {name}"
          + ("" if shell == python else f" — shell {shell!r} vs python {python!r}"))

# The three that actually bit, asserted by value as well as by agreement, so a
# future regression cannot make both readers wrong in the same direction.
fixture = tmp / "fixture-a-key-written-twice.env"
check(through_python(fixture, {})["AGENT_CONNECT_TOKEN"] == "second",
      "a key written twice takes the last line — one token, not two")
fixture = tmp / "fixture-CRLF-line-endings.env"
check(through_python(fixture, {})["AGENT_CONNECT_TOKEN"] == "tok",
      "a file saved with CRLF endings does not smuggle a carriage return into "
      "the bearer token")
fixture = tmp / "fixture-an-environment-variable-set-to-whitespace.env"
check(through_python(fixture, {"AGENT_CONNECT_REPO": "   "})["AGENT_CONNECT_REPO"]
      == "/from-the-file",
      "a variable set to whitespace is not a decision, so the file fills it")
check(parse(b"AGENT_CONNECT_TOKEN=a\nAGENT_CONNECT_TOKEN=b\n".decode()).duplicated
      == ("AGENT_CONNECT_TOKEN",),
      "and the repetition is named, because last-wins is not obvious and the "
      "setting is a credential")


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
