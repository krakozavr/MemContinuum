"""Tests for the machine-level layer: memcontinuum-setup.sh, the user-level SessionStart
detector, and the two decision scripts the `memcontinuum` skill drives.

Same conventions as test_repo_init.py: the real scripts run via subprocess with
HOME and MEMCONTINUUM_HOME sandboxed to fresh temp dirs, so nothing here ever
reads or writes the real machine's ~/.claude or ~/.memcontinuum.

The split under test:
  memcontinuum-setup.sh  machine level -- venv/config + the USER-level hook and skill
  scripts/repo-init.sh    repository level -- one store, seven per-project hooks
Only the first is covered here; scripts/repo-init.sh has its own file.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
SETUP_SH = TOOLS_DIR / "memcontinuum-setup.sh"
DETECT_SH = TOOLS_DIR / "hooks" / "memcontinuum-detect.sh"
STATE_SH = TOOLS_DIR / "scripts" / "memcontinuum-state.sh"
DECIDE_SH = TOOLS_DIR / "scripts" / "memcontinuum-decide.sh"

VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
_SKIP_NO_VENV = (
    "set $MEMCONTINUUM_PYTHON to a venv python to run these tests (see README.md)"
)

FOREIGN_HOOK = "echo not-ours"


def clean_env(home, mc_home):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MEMCONTINUUM_"):
            del env[k]
    env["HOME"] = home
    env["MEMCONTINUUM_HOME"] = mc_home
    return env


def run(script, args, home, mc_home, stdin=None, timeout=120):
    return subprocess.run(
        ["bash", str(script)] + args,
        input=stdin, capture_output=True, text=True,
        env=clean_env(home, mc_home), timeout=timeout,
    )


def git_repo(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    return path


class BootstrapCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-setup-test-")
        self.home = str(Path(self.tmp) / "home")
        self.mc_home = str(Path(self.home) / ".memcontinuum")
        self.claude = str(Path(self.home) / ".claude")
        Path(self.claude).mkdir(parents=True)
        self.settings = Path(self.claude) / "settings.json"
        # A pre-existing user settings.json with unrelated content: every
        # merge assertion below is really "did we leave this alone".
        self.settings.write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": FOREIGN_HOOK}]}
            ]},
        }), encoding="utf-8")

    def bootstrap(self, *extra):
        return run(SETUP_SH, [
            "--python", VENV_PYTHON, "--claude-dir", self.claude, "--no-model-warm",
        ] + list(extra), self.home, self.mc_home)

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def our_commands(self):
        cmds = []
        for group in self.read_settings().get("hooks", {}).get("SessionStart", []):
            for item in group.get("hooks", []):
                if "memcontinuum-detect.sh" in item.get("command", ""):
                    cmds.append(item["command"])
        return cmds

    def foreign_commands(self):
        return [
            item.get("command")
            for group in self.read_settings().get("hooks", {}).get("SessionStart", [])
            for item in group.get("hooks", [])
            if item.get("command") == FOREIGN_HOOK
        ]


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestBootstrapInstall(BootstrapCase):
    def test_dry_run_writes_nothing(self):
        before = self.settings.read_text(encoding="utf-8")
        proc = self.bootstrap("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would:", proc.stdout)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), before)
        self.assertFalse(Path(self.mc_home, "config.sh").exists())
        self.assertFalse(Path(self.claude, "skills", "memcontinuum").exists())

    def test_hook_command_is_quoted_and_bash_prefixed(self):
        """`bash '<path>'`, both values single-quoted: survives a space in the
        checkout or home, and works with the executable bit stripped (a zip
        download or core.filemode=false clone drops it) -- round-2 finding 2.
        The execution claim is proven below, not just asserted on the string
        (round-3 finding 10: a string assertion alone proves nothing about
        whether the installed line actually runs)."""
        self.assertEqual(self.bootstrap().returncode, 0)
        (cmd,) = self.our_commands()
        self.assertIn(" bash '", cmd)
        self.assertIn("MEMCONTINUUM_HOME='", cmd)

    def test_installed_command_runs_with_the_exec_bit_stripped(self):
        """Execute the exact installed command line, via a COPY of the engine
        whose scripts carry no executable bit at all, against an undecided
        repo: the detector must still speak."""
        engine_copy = Path(self.tmp) / "engine copy"  # space on purpose
        (engine_copy / "hooks").mkdir(parents=True)
        src = TOOLS_DIR / "hooks" / "memcontinuum-detect.sh"
        dst = engine_copy / "hooks" / "memcontinuum-detect.sh"
        dst.write_bytes(src.read_bytes())
        dst.chmod(0o644)  # bit deliberately stripped

        cmd = (
            f"MEMCONTINUUM_HOME='{self.mc_home}' bash '{dst}'"
        )
        repo = git_repo(str(Path(self.tmp) / "repo"))
        payload = json.dumps({"source": "startup", "cwd": repo})
        proc = subprocess.run(
            ["bash", "-c", cmd], input=payload, capture_output=True,
            text=True, env=clean_env(self.home, self.mc_home),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("hookSpecificOutput", proc.stdout)

    def test_settings_write_is_atomic_no_tmp_left_behind(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        self.assertFalse(Path(str(self.settings) + ".tmp-memcontinuum").exists())

    def test_non_object_hooks_value_is_refused_not_replaced(self):
        self.settings.write_text('{"hooks": "oops"}', encoding="utf-8")
        proc = self.bootstrap()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), '{"hooks": "oops"}')

    def test_installs_config_skill_and_hook_without_disturbing_foreign_content(self):
        proc = self.bootstrap()
        self.assertEqual(proc.returncode, 0, proc.stderr)

        config = Path(self.mc_home, "config.sh").read_text(encoding="utf-8")
        # sh_quote serialization: single-quoted values, python behind an
        # explicit env-override guard (round-3 finding 2).
        self.assertIn(f"MEMCONTINUUM_ENGINE='{TOOLS_DIR}'", config)
        self.assertIn(f"MEMCONTINUUM_PYTHON='{VENV_PYTHON}'", config)
        self.assertIn('if [ -z "${MEMCONTINUUM_PYTHON:-}" ]', config)

        self.assertTrue(Path(self.claude, "skills", "memcontinuum", "SKILL.md").is_file())
        self.assertEqual(len(self.our_commands()), 1)
        self.assertEqual(self.foreign_commands(), [FOREIGN_HOOK])
        self.assertEqual(self.read_settings()["permissions"], {"allow": ["Bash(ls:*)"]})
        self.assertTrue(Path(str(self.settings) + ".bak-memcontinuum").is_file())

    def test_rerunning_never_duplicates_the_hook(self):
        for _ in range(3):
            self.assertEqual(self.bootstrap().returncode, 0)
        self.assertEqual(len(self.our_commands()), 1)
        self.assertEqual(self.foreign_commands(), [FOREIGN_HOOK])

    def test_refuses_malformed_settings_rather_than_clobbering_it(self):
        self.settings.write_text("{not json", encoding="utf-8")
        proc = self.bootstrap()
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), "{not json")

    def test_uninstall_removes_ours_and_keeps_everything_else(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        proc = run(SETUP_SH, ["--claude-dir", self.claude, "--uninstall"],
                   self.home, self.mc_home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.our_commands(), [])
        self.assertEqual(self.foreign_commands(), [FOREIGN_HOOK])
        self.assertFalse(Path(self.claude, "skills", "memcontinuum").exists())
        self.assertFalse(Path(self.mc_home, "config.sh").exists())

    def test_uninstall_keeps_recorded_decisions(self):
        """A decision is the human's answer, not an installer artifact."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        run(DECIDE_SH, ["declined", "--repo", repo], self.home, self.mc_home)
        decisions = Path(self.mc_home, "decisions.tsv")
        self.assertTrue(decisions.is_file())
        run(SETUP_SH, ["--claude-dir", self.claude, "--uninstall"], self.home, self.mc_home)
        self.assertTrue(decisions.is_file())


class TestDetectorStates(BootstrapCase):
    """The detector speaks in exactly one state and stays silent in the rest."""

    def detect(self, cwd, source="startup"):
        payload = json.dumps({"source": source, "cwd": str(cwd)})
        return run(DETECT_SH, [], self.home, self.mc_home, stdin=payload)

    def test_undecided_repo_is_the_only_state_that_speaks(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        proc = self.detect(repo)
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("memcontinuum", ctx)
        # It must ask, never act.
        self.assertIn("Ask the user", ctx)
        self.assertIn("Do not run anything before they answer", ctx)

    def test_silent_outside_a_git_repo(self):
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        self.assertEqual(self.detect(plain).stdout, "")

    def test_silent_on_resume_and_compact(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        for source in ("resume", "compact"):
            self.assertEqual(self.detect(repo, source=source).stdout, "", source)

    def test_silent_once_a_decision_exists_either_way(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        run(DECIDE_SH, ["declined", "--repo", repo], self.home, self.mc_home)
        self.assertEqual(self.detect(repo).stdout, "", "declined")
        # `wired` requires actual wiring to record -- which also makes the
        # detector silent via the settings check itself.
        TestDecisionRegistry._wire(repo)
        run(DECIDE_SH, ["forget", "--repo", repo], self.home, self.mc_home)
        run(DECIDE_SH, ["wired", "--repo", repo], self.home, self.mc_home)
        self.assertEqual(self.detect(repo).stdout, "", "wired")

    def test_silent_under_the_machine_wide_never_ask(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        run(DECIDE_SH, ["never-ask"], self.home, self.mc_home)
        self.assertEqual(self.detect(repo).stdout, "")
        run(DECIDE_SH, ["ask-again"], self.home, self.mc_home)
        self.assertNotEqual(self.detect(repo).stdout, "")

    def test_silent_when_the_repo_is_already_wired(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude = Path(repo, ".claude")
        claude.mkdir()
        (claude / "settings.local.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [
                {"hooks": [{"type": "command", "command": "bash …/ledger-post-edit.sh"}]}
            ]}}), encoding="utf-8")
        self.assertEqual(self.detect(repo).stdout, "")

    def test_junk_payload_is_silent_even_with_an_undecided_repo_as_cwd(self):
        """Not just rc=0: stdout must be EMPTY. A junk payload must never fall
        back to classifying $PWD -- a wrong-cwd fallback could re-ask in a repo
        the human already settled (round-2 review finding 1: the earlier
        version of this test asserted only the exit code, and passed for the
        wrong reason because the test process's own cwd was a wired repo)."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        for payload in ("", "not json", "{}", '{"cwd":', '{"source":"startup"}'):
            proc = subprocess.run(
                ["bash", str(DETECT_SH)], input=payload, capture_output=True,
                text=True, env=clean_env(self.home, self.mc_home), cwd=repo,
            )
            self.assertEqual(proc.returncode, 0, payload)
            self.assertEqual(proc.stdout, "", payload)

    def test_duplicate_keys_in_compact_json_are_first_wins(self):
        """grep -o | head -1, not greedy sed: compact and pretty-printed JSON
        must agree on which value wins (round-2 finding 4)."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        payload = '{"cwd":"%s","source":"startup","cwd":"/nonexistent"}' % repo
        proc = run(DETECT_SH, [], self.home, self.mc_home, stdin=payload)
        self.assertIn("hookSpecificOutput", proc.stdout)


class TestDecisionRegistry(BootstrapCase):
    @staticmethod
    def _wire(repo):
        """Give a test repo real-looking hook wiring so `decide wired` accepts."""
        claude = Path(repo, ".claude")
        claude.mkdir(exist_ok=True)
        # decide.sh's wired check requires ALL FIVE write-side hooks on
        # "command" lines (round-3 finding 3), so the fixture wires them all.
        items = [
            {"type": "command", "command": f"bash x/{b}"}
            for b in (
                "ledger-post-edit.sh", "precompact-persist.sh",
                "sessionstart-remind.sh", "userprompt-remind.sh",
                "sessionend-stamp.sh",
            )
        ]
        (claude / "settings.local.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [{"hooks": items}]}}),
            encoding="utf-8")

    def state_of(self, repo):
        out = run(STATE_SH, [str(repo)], self.home, self.mc_home).stdout
        for line in out.splitlines():
            if line.startswith("state="):
                return line.split("=", 1)[1]
        return None

    def test_no_config_before_bootstrap(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self.assertEqual(self.state_of(repo), "no-config")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_states_track_the_recorded_answer(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self.assertEqual(self.state_of(repo), "undecided")
        run(DECIDE_SH, ["declined", "--repo", repo], self.home, self.mc_home)
        self.assertEqual(self.state_of(repo), "declined")
        run(DECIDE_SH, ["forget", "--repo", repo], self.home, self.mc_home)
        self.assertEqual(self.state_of(repo), "undecided")

    def test_wired_is_refused_when_the_repo_has_no_hook_wiring(self):
        """A wired row silences the detector forever whether or not scripts/repo-init.sh
        ever succeeded -- the one write that can lie (round-2 finding 5)."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        proc = run(DECIDE_SH, ["wired", "--repo", repo], self.home, self.mc_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stderr)
        self.assertFalse(Path(self.mc_home, "decisions.tsv").exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_reversal_replaces_the_row_rather_than_shadowing_it(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire(repo)
        run(DECIDE_SH, ["declined", "--repo", repo], self.home, self.mc_home)
        run(DECIDE_SH, ["wired", "--repo", repo, "--store", "/tmp/s", "--project", "p"],
            self.home, self.mc_home)
        rows = [
            ln for ln in Path(self.mc_home, "decisions.tsv").read_text(encoding="utf-8").splitlines()
            if ln.startswith(str(repo) + "\t")
        ]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn("wired", rows[0])

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_key_is_the_remote_when_there_is_one(self):
        """Remote-keyed so a repo that moves on disk keeps its answer -- a
        path key evaporates on a move and a settled decision looks unmade."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        subprocess.run(["git", "remote", "add", "origin", "https://example.invalid/x.git"],
                       cwd=repo, check=True)
        out = run(STATE_SH, [str(repo)], self.home, self.mc_home).stdout
        self.assertIn("key=https://example.invalid/x.git", out)

    def test_decide_refuses_outside_a_repo(self):
        plain = Path(self.tmp) / "plain"
        plain.mkdir()
        proc = run(DECIDE_SH, ["declined", "--repo", str(plain)], self.home, self.mc_home)
        self.assertNotEqual(proc.returncode, 0)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestMemlibReadsConfig(BootstrapCase):
    """config.sh exists so a hook resolves python without every hook line
    carrying MEMCONTINUUM_PYTHON by hand -- the omission that left a real
    project's hooks silently dead."""

    def test_memlib_resolves_python_from_config_when_env_is_unset(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        probe = (
            f'MEMCONTINUUM_HOME={self.mc_home} '
            f'. {TOOLS_DIR}/hooks/memlib.sh; printf "%s" "$MC_PY"'
        )
        env = clean_env(self.home, self.mc_home)
        del env["MEMCONTINUUM_HOME"]
        proc = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, env=env)
        self.assertEqual(proc.stdout.strip(), VENV_PYTHON, proc.stderr)

    def test_explicit_env_still_wins_over_config(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        probe = (
            f'MEMCONTINUUM_HOME={self.mc_home} MEMCONTINUUM_PYTHON=/explicit/python '
            f'. {TOOLS_DIR}/hooks/memlib.sh; printf "%s" "$MC_PY"'
        )
        env = clean_env(self.home, self.mc_home)
        del env["MEMCONTINUUM_HOME"]
        proc = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, env=env)
        self.assertEqual(proc.stdout.strip(), "/explicit/python", proc.stderr)


if __name__ == "__main__":
    unittest.main()
