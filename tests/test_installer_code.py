"""install.sh's one-time code: redeemed once, checked, and written only to the config."""
from __future__ import annotations

import _bootstrap
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = _bootstrap.ROOT
AGENT = "@owner-openclaw.agent:example.org"
CODE = "acp.Zx9-one-time"
SECRET = "s3cret-bearer"


class FakeBackend:
    """Answers POST /api/connect/poll from a script of (status, body) replies."""

    def __init__(self):
        self.replies, self.calls = [], []
        backend = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                backend.calls.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                status, body, headers = backend.replies.pop(0) if backend.replies else (500, {}, {})
                data = json.dumps(body).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/api"
        self.relay = f"http://127.0.0.1:{self.port}/relay"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def ok(self, **over):
        body = {"relay_url": self.relay, "bearer": SECRET, "agent_id": AGENT, **over}
        self.replies.append((200, body, {}))

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class InstallerCode(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="agent-code-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pipx_log = self.root / "pipx.log"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("AGENT_CONNECT_", "REMOTE_TASK_"))}
        self.env.update(HOME=str(self.home), PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                        PIPX_LOG=str(self.pipx_log))
        self.write_bin("pipx", '#!/bin/sh\necho "$*" >> "$PIPX_LOG"\nexit "${PIPX_RC:-0}"\n')
        self.write_bin("agent-connect", "#!/bin/sh\nexit 0\n")
        self.backend = FakeBackend()
        self.addCleanup(self.backend.close)
        instance = "agent-" + hashlib.sha256(AGENT.encode()).hexdigest()[:24]
        self.config = self.home / ".agent-connect" / "instances" / instance / "config.env"

    def write_bin(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def run_install(self, *args, stdin="", env=None, script_on_stdin=False):
        flags = ["--adapter", "acp", "--acp-command", "openclaw acp", "--no-start", *args]
        cmd = ["sh", "-s", "--", *flags] if script_on_stdin else ["sh", str(ROOT / "install.sh"), *flags]
        if script_on_stdin:
            stdin = (ROOT / "install.sh").read_text()
        return subprocess.run(cmd, input=stdin, env={**self.env, **(env or {})},
                              capture_output=True, text=True, timeout=60)

    def with_code(self, *extra, stdin=CODE + "\n", **kw):
        return self.run_install("--base", self.backend.base, "--agent-id", AGENT, "--code", "-",
                                *extra, stdin=stdin, **kw)

    def stored_token(self):
        for line in self.config.read_text().splitlines():
            if line.startswith("AGENT_CONNECT_TOKEN="):
                return line.partition("=")[2].strip('"')
        return None

    def test_code_is_redeemed_once_and_the_token_only_reaches_the_config(self):
        self.backend.ok()
        result = self.with_code()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backend.calls, [("/api/connect/poll", {"device_code": CODE})])
        self.assertEqual(self.stored_token(), f"{self.backend.relay}|{SECRET}")
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.assertNotIn(CODE, result.stdout + result.stderr)
        self.assertFalse((self.config.parent / ".redeem.py").exists())
        self.assertIn('AGENT_CONNECT_ACP_COMMAND="openclaw acp"', self.config.read_text())

    def test_a_code_for_another_agent_is_refused_and_nothing_is_written(self):
        self.backend.ok(agent_id="@someone-else.agent:example.org")
        result = self.with_code()
        self.assertEqual(result.returncode, 1)
        self.assertIn("different agent", result.stderr)
        self.assertFalse(self.config.exists())
        self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_a_relay_on_another_host_needs_trust(self):
        other = f"http://localhost:{self.backend.port}/relay"
        self.backend.ok(relay_url=other)
        result = self.with_code()
        self.assertEqual(result.returncode, 1)
        self.assertIn("--trust-relay-host localhost", result.stderr)
        self.assertFalse(self.config.exists())
        self.backend.ok(relay_url=other)
        result = self.with_code("--trust-relay-host", "localhost")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stored_token(), f"{other}|{SECRET}")

    def test_a_used_code_says_so(self):
        self.backend.replies.append((404, {"error": "unknown"}, {}))
        result = self.with_code()
        self.assertEqual(result.returncode, 1)
        self.assertIn("already used or has expired", result.stderr)
        self.assertFalse(self.config.exists())

    def test_still_redeeming_is_retried(self):
        self.backend.replies.append((202, {"retry_after": 1}, {}))
        self.backend.ok()
        result = self.with_code()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.backend.calls), 2)

    def test_a_redirect_is_not_followed(self):
        self.backend.replies.append((302, {}, {"Location": "http://127.0.0.1:1/elsewhere"}))
        result = self.with_code()
        self.assertEqual(result.returncode, 1)
        self.assertIn("redirected", result.stderr)
        self.assertEqual(len(self.backend.calls), 1)

    def test_a_hostile_relay_url_is_refused_and_nothing_is_written(self):
        good = self.backend.relay
        hostile = {
            "pipe in host": f"http://evil.example|x.127.0.0.1:{self.backend.port}/relay",
            "LF": good + '\nAGENT_CONNECT_ACP_COLLABORATORS="*"\nAGENT_CONNECT_TOKEN="' + good,
            "CR": good + '\rAGENT_CONNECT_ACP_COMMAND="sh -c id"',
            "tab": good + "\tx",
            "vertical tab": good + "\x0bAGENT_CONNECT_ACP_COMMAND=x",
            "NEL": good + "\x85AGENT_CONNECT_ACP_COMMAND=x",
            "line separator": good + "\u2028AGENT_CONNECT_ACP_COMMAND=x",
            "quote": good + '"x',
            "trailing space": good + " ",
        }
        for name, relay_url in hostile.items():
            with self.subTest(name):
                self.backend.ok(relay_url=relay_url)
                result = self.with_code()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertFalse(self.config.exists())
                self.assertNotIn(SECRET, result.stdout + result.stderr)
        self.backend.ok()
        result = self.with_code()
        self.assertEqual(result.returncode, 0, result.stderr)
        from agent_connect import config as cfg
        values = cfg.parse(self.config.read_text()).values
        self.assertEqual(values["AGENT_CONNECT_TOKEN"], f"{good}|{SECRET}")
        self.assertNotIn("AGENT_CONNECT_ACP_COLLABORATORS", values)

    def test_an_agent_id_with_a_space_does_not_spend_the_code(self):
        self.backend.ok()
        result = self.run_install("--base", self.backend.base, "--agent-id", AGENT + " ",
                                  "--code", "-", stdin=CODE + "\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.backend.calls, [])

    def test_a_malformed_bearer_is_refused(self):
        self.backend.ok(bearer="a|b")
        result = self.with_code()
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.config.exists())

    def test_the_code_is_not_spent_when_the_machine_cannot_install(self):
        result = self.with_code(env={"PIPX_RC": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.backend.calls, [])

    def test_not_a_code_is_refused_before_anything_runs(self):
        result = self.with_code(stdin="REMOTE_TASK_TOKEN=x\n")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not an agent code", result.stderr)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(self.pipx_log.exists())

    def test_a_rerun_without_a_credential_keeps_the_stored_token(self):
        self.backend.ok()
        self.assertEqual(self.with_code().returncode, 0)
        result = self.run_install("--agent-id", AGENT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("keeping the token already in", result.stdout)
        self.assertEqual(self.stored_token(), f"{self.backend.relay}|{SECRET}")
        self.assertEqual(len(self.backend.calls), 1)

    def test_a_bare_run_never_rewrites_the_default_install(self):
        default = self.home / ".agent-connect" / "config.env"
        default.parent.mkdir(parents=True)
        default.write_text('AGENT_CONNECT_TOKEN="https://r.example.org|t"\nAGENT_CONNECT_ADAPTER="acp"\n')
        before = default.read_text()
        result = subprocess.run(["sh", str(ROOT / "install.sh")], env=self.env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(default.read_text(), before)
        self.assertFalse(self.pipx_log.exists())

    def test_no_credential_and_nothing_stored_is_refused(self):
        result = self.run_install("--agent-id", AGENT)
        self.assertEqual(result.returncode, 2)
        self.assertIn("no credential", result.stderr)

    def test_token_stdin(self):
        result = self.run_install("--agent-id", AGENT, "--token-stdin",
                                  stdin=f"https://relay.example.org|{SECRET}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stored_token(), f"https://relay.example.org|{SECRET}")
        self.assertNotIn("command line", result.stderr)

    def test_a_token_on_the_command_line_still_works_with_a_note(self):
        result = self.run_install("--agent-id", AGENT, "--token", f"https://relay.example.org|{SECRET}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kept by ps and shell history", result.stderr)

    def test_stdin_is_never_read_when_it_is_the_script(self):
        for flags in (["--token-stdin"], ["--base", self.backend.base, "--code", "-"]):
            result = self.run_install("--agent-id", AGENT, *flags, script_on_stdin=True)
            self.assertEqual(result.returncode, 2, flags)
            self.assertIn("stdin is this script", result.stderr)
        self.assertEqual(self.backend.calls, [])

    def test_flag_combinations_are_refused(self):
        cases = [
            (["--agent-id", AGENT, "--code", "-"], "--code needs --base"),
            (["--base", self.backend.base, "--code", "-"], "--base needs --agent-id"),
            (["--agent-id", AGENT, "--token", "t", "--base", self.backend.base], "pass one of"),
        ]
        for flags, message in cases:
            result = self.run_install(*flags, stdin=CODE + "\n")
            self.assertEqual(result.returncode, 2, flags)
            self.assertIn(message, result.stderr)
        self.assertEqual(self.backend.calls, [])


if __name__ == "__main__":
    unittest.main()
