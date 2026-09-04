"""Dry-run unit test for tests/mac_smoke.sh (Anatomy M2b, Task 11, B7).

Asserts the script's argument handling and guard ORDER without ever
sshing anywhere -- the real ssh run against macmini is a coordinator step
(the plan's Step 5), not part of `unittest discover`. See mac_smoke.sh's
own header for what the real run does and why it is a PARSER PROBE, not a
substitute for the macOS arm64 CI job.
"""
import os
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tests" / "mac_smoke.sh"


class TestMacSmokeDryRun(unittest.TestCase):
    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_pgrep_guard_runs_before_any_ssh_command(self):
        text = SCRIPT.read_text()
        pgrep_idx = text.find("pgrep")
        ssh_idx = text.find("ssh ")
        self.assertNotEqual(pgrep_idx, -1, "mac_smoke.sh must pgrep-guard ShotPorter")
        self.assertNotEqual(ssh_idx, -1)
        self.assertLess(pgrep_idx, ssh_idx, "the pgrep guard must run before the first real ssh call")

    def test_never_installs_via_requirements_lock_on_the_remote_host(self):
        text = SCRIPT.read_text()
        self.assertNotIn("requirements.lock", text)

    def test_uses_the_explicit_login_shell_python_path(self):
        text = SCRIPT.read_text()
        self.assertIn("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3", text)

    def test_dry_run_flag_skips_the_ssh_call_entirely(self):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Permission denied", proc.stderr)
        self.assertIn("dry run", (proc.stdout + proc.stderr).lower())


if __name__ == "__main__":
    unittest.main()
