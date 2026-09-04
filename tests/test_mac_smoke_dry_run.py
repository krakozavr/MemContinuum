"""Dry-run unit test for tests/mac_smoke.sh (Anatomy M2b, Task 11, B7).

Asserts the script's argument handling and guard ORDER without ever
sshing anywhere -- the real ssh run against macmini is a coordinator step
(the plan's Step 5), not part of `unittest discover`. See mac_smoke.sh's
own header for what the real run does and why it is a PARSER PROBE, not a
substitute for the macOS arm64 CI job.

The guard process name (what mac_smoke.sh pgrep-guards for on the target
host) is never a literal in this file -- see
tests/test_repo_init.py::TestNoMachineIdentifyingContent. Every test here
supplies a placeholder ("GuardedApp") via $MC_MAC_SMOKE_GUARD_PROCESS or a
temp $MC_MAC_SMOKE_GUARD_FILE, and none of them touch the real, untracked
tests/mac_smoke.local -- a run on the dev machine (where that file exists
with the real name) must see identical results to a fresh CI checkout
(where it does not).
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "tests" / "mac_smoke.sh"


def _env_without_guard_sources():
    """A copy of the current environment with both guard-name sources
    cleared -- MC_MAC_SMOKE_GUARD_FILE repointed at a path that does not
    exist, rather than merely unset, so this is independent of whether
    tests/mac_smoke.local happens to exist on the machine running the
    test."""
    env = dict(os.environ)
    env.pop("MC_MAC_SMOKE_GUARD_PROCESS", None)
    env["MC_MAC_SMOKE_GUARD_FILE"] = str(REPO_ROOT / "tests" / "_no_such_guard_file.local")
    return env


def _env_with_guard_process(name="GuardedApp"):
    env = _env_without_guard_sources()
    env["MC_MAC_SMOKE_GUARD_PROCESS"] = name
    return env


class TestMacSmokeDryRun(unittest.TestCase):
    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT.is_file())
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_pgrep_guard_runs_before_any_ssh_command(self):
        text = SCRIPT.read_text()
        pgrep_idx = text.find("pgrep")
        ssh_idx = text.find("ssh ")
        self.assertNotEqual(pgrep_idx, -1, "mac_smoke.sh must pgrep-guard the resolved process name")
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
            env=_env_with_guard_process(),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Permission denied", proc.stderr)
        self.assertIn("dry run", (proc.stdout + proc.stderr).lower())

    def test_dry_run_resolves_guard_name_from_env_var(self):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=30,
            env=_env_with_guard_process("GuardedApp"),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("GuardedApp", proc.stdout)

    def test_dry_run_resolves_guard_name_from_file_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard_file = Path(tmp) / "guard.local"
            guard_file.write_text("GuardedApp\n", encoding="utf-8")
            env = _env_without_guard_sources()
            env["MC_MAC_SMOKE_GUARD_FILE"] = str(guard_file)
            proc = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run"], capture_output=True, text=True, timeout=30, env=env,
            )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("GuardedApp", proc.stdout)

    def test_unset_guard_name_aborts_before_any_ssh(self):
        """Neither $MC_MAC_SMOKE_GUARD_PROCESS nor a guard file resolves a
        name -> hard abort naming both sources, and -- critically -- run
        WITHOUT --dry-run, so a script that let this slip past the guard
        check would go on to dial the (in this test, unreachable) 'ssh
        macmini' host and hang past the short timeout below instead of
        exiting immediately."""
        proc = subprocess.run(
            ["bash", str(SCRIPT)], capture_output=True, text=True, timeout=10,
            env=_env_without_guard_sources(),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("MC_MAC_SMOKE_GUARD_PROCESS", proc.stderr)
        self.assertIn("_no_such_guard_file.local", proc.stderr)


if __name__ == "__main__":
    unittest.main()
