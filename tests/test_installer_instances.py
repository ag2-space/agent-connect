"""Exercise the real installer/launchers with offline package/service boundaries."""
from __future__ import annotations

import _bootstrap
import hashlib
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = _bootstrap.ROOT


class InstallerInstances(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-instances-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "services.log"
        self.env = {
            k: v for k, v in os.environ.items()
            if not k.startswith(("AGENT_CONNECT_", "REMOTE_TASK_"))
        }
        self.env.update(HOME=str(self.home), PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                        TEST_SERVICE_LOG=str(self.log))
        self.write_bin("pipx", "#!/bin/sh\nexit 0\n")
        self.write_bin("npm", "#!/bin/sh\nexit 0\n")
        for command in ["launchctl", "systemctl"]:
            self.write_bin(command, '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_SERVICE_LOG"\n')
        self.write_bin("uname", '#!/bin/sh\nprintf "%s\\n" "${TEST_OS:-Darwin}"\n')
        # Use the real settings parser; the execution boundary merely records
        # which credential/workspace this live process received, then waits.
        self.write_bin("agent-connect", f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, {str(ROOT)!r})
from agent_connect.config import read
config = read(Path(os.environ['AGENT_CONNECT_CONFIG']))
if '--export-config' in sys.argv:
    print('\\n'.join(config.exports()))
else:
    config.apply()
    ws = Path(os.environ['AGENT_CONNECT_WORKSPACE'])
    ws.mkdir(parents=True, exist_ok=True)
    (ws / 'observed.json').write_text(json.dumps({{
        'token': os.environ['AGENT_CONNECT_TOKEN'],
        'instance': os.environ.get('AGENT_CONNECT_INSTANCE', 'default'),
        'adapter': os.environ['AGENT_CONNECT_ADAPTER'], 'pid': os.getpid()}}))
    while True: time.sleep(1)
''')

    def write_bin(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def install(self, name=None, token="token-a", start=False, adapter="codex", os_name="Darwin", agent_id=None):
        args = ["sh", str(ROOT / "install.sh"), "--token", token, "--adapter", adapter]
        if name is not None:
            args += ["--instance", name]
        if agent_id is not None:
            args += ["--agent-id", agent_id]
        if adapter == "acp":
            args += ["--acp-agent", "claude"]
        if not start:
            args += ["--no-start"]
        result = subprocess.run(args, env={**self.env, "TEST_OS": os_name},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def directory(self, name):
        base = self.home / ".agent-connect"
        return base if name == "default" else base / "instances" / name

    def start(self, name):
        directory = self.directory(name)
        proc = subprocess.Popen(["sh", str(directory / "launch.sh")], env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def stop():
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        self.addCleanup(stop)
        path = directory / "workspace/observed.json"
        until = time.monotonic() + 5
        while time.monotonic() < until:
            if path.exists():
                data = json.loads(path.read_text())
                if data["pid"] == proc.pid:
                    return proc, data
            if proc.poll() is not None:
                self.fail(f"launcher exited {proc.returncode}")
            time.sleep(0.02)
        self.fail("launcher did not start its worker")

    def test_two_codex_launchers_stay_alive_and_reconnect_only_the_same_agent(self):
        self.install("first")
        first, a = self.start("first")
        self.install("second", "token-b")
        second, b = self.start("second")
        self.assertIsNone(first.poll(), "starting another agent killed the first")
        self.assertEqual((a["token"], b["token"]), ("token-a", "token-b"))
        self.assertEqual((a["instance"], b["instance"]), ("first", "second"))
        self.install("second", "rotated-token-b")
        replacement, updated = self.start("second")
        second.wait(timeout=5)
        self.assertEqual(updated["token"], "rotated-token-b")
        self.assertIsNone(first.poll(), "reconnecting second killed first")
        self.assertIsNone(replacement.poll())
        self.assertIn('"token-b"', (self.directory("second") / "config.env.bak").read_text())
        self.assertEqual((self.directory("first") / "config.env").stat().st_mode & 0o777, 0o600)

    def test_codex_and_claude_do_not_share_settings(self):
        self.install("codex-a")
        self.install("claude-a", "claude-token", adapter="acp")
        codex, a = self.start("codex-a")
        claude, b = self.start("claude-a")
        self.assertEqual((a["adapter"], b["adapter"]), ("codex", "acp"))
        self.assertIsNone(codex.poll())
        self.assertIsNone(claude.poll())

    def test_agent_identity_is_stable_across_rotation_and_separate_across_homeservers(self):
        first_id = "@owner-codex.agent:dev.example.org"
        second_id = "@owner-codex.agent:example.org"
        name = "agent-" + hashlib.sha256(first_id.encode()).hexdigest()[:24]
        self.install(agent_id=first_id)
        first, data = self.start(name)
        self.assertEqual(data["instance"], name)
        self.install(token="rotated-a", agent_id=first_id)
        replacement, updated = self.start(name)
        first.wait(timeout=5)
        self.assertEqual(updated["token"], "rotated-a")
        self.install(token="token-b", agent_id=second_id)
        dirs = list((self.home / ".agent-connect/instances").iterdir())
        self.assertEqual(len(dirs), 2)
        self.assertIsNone(replacement.poll())
        self.assertLessEqual(len(name), 32)

    def test_agent_id_is_data_and_not_executed(self):
        identity = "@owner-$(touch injected)'&.agent:example.org"
        self.install(agent_id=identity)
        self.assertFalse((ROOT / "injected").exists())
        self.assertEqual(len(list((self.home / ".agent-connect/instances").iterdir())), 1)

    def test_ambiguous_or_empty_identity_selection_is_refused(self):
        for args in [["--agent-id", ""], ["--agent-id=not-matrix"], ["--agent-id"],
                     ["--agent-id", "@:server"], ["--agent-id", "@name:"], ["--agent-id", "@:"],
                     ["--agent-id", "@x:test", "--instance", "x"]]:
            result = subprocess.run(["sh", str(ROOT / "install.sh"), "--token", "test", *args],
                                    env=self.env, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_agent_id_overrides_exported_instance_default(self):
        identity = "@owner-codex.agent:example.org"
        name = "agent-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        # Even an invalid exported default is irrelevant to an explicit ID.
        for exported in ["shell-default", "Codex", "../unused"]:
            with self.subTest(exported=exported):
                self.env["AGENT_CONNECT_INSTANCE"] = exported
                self.install(agent_id=identity)
                config = (self.directory(name) / "config.env").read_text()
                self.assertIn(f'AGENT_CONNECT_INSTANCE="{name}"', config)
        self.assertEqual([p.name for p in (self.home / ".agent-connect/instances").iterdir()], [name])
        self.assertFalse((self.directory("default") / "config.env").exists())

    def test_exported_instance_is_used_without_an_explicit_id(self):
        self.env["AGENT_CONNECT_INSTANCE"] = "shell-default"
        self.install()
        self.assertTrue((self.directory("shell-default") / "config.env").exists())
        self.install("explicit", "token-b")
        self.assertIn('AGENT_CONNECT_TOKEN="token-b"',
                      (self.directory("explicit") / "config.env").read_text())

    def test_launchd_only_reloads_selected_named_service(self):
        self.install(start=True)
        legacy = self.home / "Library/LaunchAgents/space.ag2.agent-connect.plist"
        original = legacy.read_bytes()
        self.log.write_text("")
        self.install("second", "token-b", start=True)
        named = legacy.with_name("space.ag2.agent-connect.second.plist")
        data = plistlib.loads(named.read_bytes())
        self.assertEqual(data["Label"], "space.ag2.agent-connect.second")
        self.assertEqual(data["EnvironmentVariables"]["AGENT_CONNECT_CONFIG"],
                         str(self.directory("second") / "config.env"))
        self.assertNotIn("token-b", named.read_text())
        self.assertNotIn(str(legacy), self.log.read_text())
        self.assertEqual(legacy.read_bytes(), original)

    def test_matching_legacy_credential_is_migrated_without_duplicate_service(self):
        self.install(start=True)
        legacy = self.home / "Library/LaunchAgents/space.ag2.agent-connect.plist"
        self.install("first", start=True)
        self.assertFalse(legacy.exists())
        self.assertTrue(legacy.with_suffix(".plist.migrated").exists())
        self.assertIn(f"unload {legacy}", self.log.read_text())
        self.assertIn('"token-a"', (self.directory("default") / "config.env").read_text())

    def test_legacy_migration_compares_the_unquoted_trimmed_last_token(self):
        self.install(start=True)
        config = self.directory("default") / "config.env"
        config.write_text(config.read_text() + '\n AGENT_CONNECT_TOKEN = "token-a"  \t\n')
        self.install("first", start=True)
        legacy = self.home / "Library/LaunchAgents/space.ag2.agent-connect.plist"
        self.assertFalse(legacy.exists())
        self.assertTrue(legacy.with_suffix(".plist.migrated").exists())

    def test_failed_legacy_unload_does_not_activate_another_worker(self):
        self.install(start=True)
        self.write_bin("launchctl", '''#!/bin/sh
printf "%s\\n" "$*" >> "$TEST_SERVICE_LOG"
case "$1:$2" in unload:*/space.ag2.agent-connect.plist) exit 1 ;; esac
''')
        self.log.write_text("")
        result = subprocess.run(["sh", str(ROOT / "install.sh"), "--token", "token-a",
                                 "--instance", "first"], env=self.env,
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("load " + str(self.home / "Library/LaunchAgents/space.ag2.agent-connect.first.plist"),
                         self.log.read_text())
        self.assertTrue((self.home / "Library/LaunchAgents/space.ag2.agent-connect.plist").exists())
        self.assertFalse((self.home / "Library/LaunchAgents/space.ag2.agent-connect.first.plist").exists(),
                         "failed migration left a second worker scheduled for the next login")

    def test_failed_systemd_migration_does_not_leave_a_named_unit(self):
        self.install(start=True, os_name="Linux")
        self.write_bin("systemctl", '''#!/bin/sh
printf "%s\\n" "$*" >> "$TEST_SERVICE_LOG"
case "$*" in '--user disable --now agent-connect.service') exit 1 ;; esac
''')
        self.log.write_text("")
        result = subprocess.run(["sh", str(ROOT / "install.sh"), "--token", "token-a",
                                 "--instance", "first"], env={**self.env, "TEST_OS": "Linux"},
                                capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("--user enable --now agent-connect-first.service", self.log.read_text())
        units = self.home / ".config/systemd/user"
        self.assertTrue((units / "agent-connect.service").exists())
        self.assertFalse((units / "agent-connect-first.service").exists())

    def test_different_legacy_credential_is_preserved_with_cleanup_guidance(self):
        self.install(start=True)
        legacy = self.home / "Library/LaunchAgents/space.ag2.agent-connect.plist"
        original = legacy.read_bytes()
        self.log.write_text("")
        output = self.install("first", "rotated-token", start=True)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertNotIn(str(legacy), self.log.read_text())
        self.assertIn("legacy default", output)
        self.assertIn("revoked", output)
        self.assertIn("README", output)
        self.assertNotIn("token-a", output)
        self.assertNotIn("rotated-token", output)

    def test_systemd_uses_separate_units_and_restarts_on_reconnect(self):
        self.install(start=True, os_name="Linux")
        self.log.write_text("")
        self.install("second", "token-b", start=True, os_name="Linux")
        self.assertTrue((self.home / ".config/systemd/user/agent-connect-second.service").exists())
        calls = self.log.read_text().splitlines()
        self.assertIn("--user restart agent-connect-second.service", calls)
        self.assertNotIn("--user disable --now agent-connect.service", calls)
        self.install("first", start=True, os_name="Linux")
        self.assertIn("--user disable --now agent-connect.service", self.log.read_text())

    def test_names_cannot_escape_paths_or_alias_on_case_insensitive_filesystems(self):
        for name in ["", "../escape", "first/second", "Codex", "a" * 33, "a\nb", "$(touch injected)"]:
            with self.subTest(name=name):
                result = subprocess.run(["sh", str(ROOT / "install.sh"), "--token", "test",
                                         "--instance", name, "--no-start"], env=self.env,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
        self.assertFalse((self.home / ".agent-connect").exists())

    def test_instance_names_remain_ascii_in_utf8_locales(self):
        available = subprocess.check_output(["locale", "-a"], text=True).splitlines()
        locales = ["C"] + [loc for loc in available
                           if loc.lower().startswith("en_us.") and "utf" in loc.lower()]
        for loc in locales:
            for name in ["Codex", "CODEX", "codéx", "codex١"]:
                with self.subTest(locale=loc, name=name):
                    result = subprocess.run(["sh", str(ROOT / "install.sh"), "--token", "test",
                                             "--instance", name, "--no-start"],
                                            env={**self.env, "LC_ALL": loc},
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse((self.home / ".agent-connect").exists())

    def test_no_start_and_legacy_layout_remain_compatible(self):
        self.install()
        legacy = (self.directory("default") / "config.env").read_bytes()
        output = self.install("second", "token-b")
        self.assertIn("install complete (not started)", output)
        self.assertFalse(self.log.exists())
        self.assertEqual((self.directory("default") / "config.env").read_bytes(), legacy)


if __name__ == "__main__":
    unittest.main()
