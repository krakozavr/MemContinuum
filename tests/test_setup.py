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

# Same override test_hooks.py/test_write_hooks.py use: tests/run_bash32.sh
# points MC_BASH at a real bash 3.2 binary so these hook-adjacent scripts get
# exercised against the actual port target, not whatever "bash" resolves to
# on this machine.
MC_BASH = os.environ.get("MC_BASH", "bash")


def clean_env(home, mc_home=None):
    # PYTHONPATH: this machine's ~/.bashrc exports it for win_python_site_
    # packages (see ~/.claude/CLAUDE.md); a Linux python inheriting that dies
    # with ModuleNotFoundError on pydantic_core or similar. Every subprocess
    # here is Linux bash + Linux python, so it must never see it.
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MEMCONTINUUM_"):
            del env[k]
    env.pop("PYTHONPATH", None)
    env["HOME"] = home
    if mc_home is not None:
        env["MEMCONTINUUM_HOME"] = mc_home
    return env


def run(script, args, home, mc_home, stdin=None, timeout=120):
    return subprocess.run(
        [MC_BASH, str(script)] + args,
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
        """`bash '<path>'`, single-quoted: survives a space in the checkout
        (round-2 finding 2), and works with the executable bit stripped (a
        zip download or core.filemode=false clone drops it). No
        MEMCONTINUUM_HOME= prefix any more (fix-round-4 F7): a baked
        setup-time value used to disagree with whatever decide.sh/state.sh
        resolved on their own -- two registries, silently; the detector now
        resolves it itself (env -> pointer config -> default), same as they
        do. The execution claim is proven below, not just asserted on the
        string (round-3 finding 10: a string assertion alone proves nothing
        about whether the installed line actually runs)."""
        self.assertEqual(self.bootstrap().returncode, 0)
        (cmd,) = self.our_commands()
        self.assertEqual(cmd, f"bash '{DETECT_SH}'")
        self.assertNotIn("MEMCONTINUUM_HOME=", cmd)

    def test_silent_when_the_shared_lib_is_missing(self):
        """Fail-open, always: a missing scripts/mc-registry-lib.sh sibling
        (a moved/incomplete checkout) must mean "say nothing", the same
        posture as every other error in this hook -- never block or nag."""
        engine_copy = Path(self.tmp) / "engine no lib"
        (engine_copy / "hooks").mkdir(parents=True)
        dst = engine_copy / "hooks" / "memcontinuum-detect.sh"
        dst.write_bytes((TOOLS_DIR / "hooks" / "memcontinuum-detect.sh").read_bytes())
        # deliberately no scripts/ sibling at all

        repo = git_repo(str(Path(self.tmp) / "repo"))
        payload = json.dumps({"source": "startup", "cwd": repo})
        proc = subprocess.run(
            [MC_BASH, str(dst)], input=payload, capture_output=True,
            text=True, env=clean_env(self.home, self.mc_home),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_installed_command_runs_with_the_exec_bit_stripped(self):
        """Execute the exact installed command line, via a COPY of the engine
        (hooks/ AND scripts/, same layout mc_repo_toplevel relies on) whose
        scripts carry no executable bit at all, against an undecided repo:
        the detector must still speak."""
        engine_copy = Path(self.tmp) / "engine copy"  # space on purpose
        (engine_copy / "hooks").mkdir(parents=True)
        (engine_copy / "scripts").mkdir(parents=True)
        for rel in ("hooks/memcontinuum-detect.sh", "scripts/mc-registry-lib.sh"):
            src = TOOLS_DIR / rel
            dst = engine_copy / rel
            dst.write_bytes(src.read_bytes())
            dst.chmod(0o644)  # bit deliberately stripped
        dst = engine_copy / "hooks" / "memcontinuum-detect.sh"

        cmd = (
            f"MEMCONTINUUM_HOME='{self.mc_home}' bash '{dst}'"
        )
        repo = git_repo(str(Path(self.tmp) / "repo"))
        payload = json.dumps({"source": "startup", "cwd": repo})
        proc = subprocess.run(
            [MC_BASH, "-c", cmd], input=payload, capture_output=True,
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
        # F7: recorded here now too -- see test_pointer_config_* below for
        # the non-default-HOME case this exists to fix. (self.mc_home IS the
        # default $HOME/.memcontinuum here, so no separate pointer applies.)
        self.assertIn(f"MEMCONTINUUM_HOME='{self.mc_home}'", config)

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
        # Machine backup rule (fix-round-4 item 4): config.sh and the
        # installed skill copy are backed up before each overwrite, same as
        # settings.json already was. Three runs -> both exist by now.
        self.assertTrue(Path(self.mc_home, "config.sh.bak-memcontinuum").is_file())
        self.assertTrue(Path(
            self.claude, "skills", "memcontinuum", "SKILL.md.bak-memcontinuum",
        ).is_file())

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

    def test_pointer_config_written_when_home_is_custom(self):
        """F7: a non-default MEMCONTINUUM_HOME used to mean two registries --
        the detector's baked-in hook line saw one, decide.sh/state.sh's own
        $HOME/.memcontinuum default saw another, so a decline never silenced
        the ask. config.sh now records the real HOME, and a minimal pointer
        is ALSO written at the FIXED default path recording it."""
        custom_home = str(Path(self.tmp) / "custom-mc")
        proc = run(SETUP_SH, [
            "--python", VENV_PYTHON, "--claude-dir", self.claude, "--no-model-warm",
        ], self.home, custom_home)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        config = Path(custom_home, "config.sh").read_text(encoding="utf-8")
        self.assertIn(f"MEMCONTINUUM_HOME='{custom_home}'", config)

        pointer = Path(self.home, ".memcontinuum", "config.sh")
        self.assertTrue(pointer.is_file())
        self.assertIn(f"MEMCONTINUUM_HOME='{custom_home}'",
                      pointer.read_text(encoding="utf-8"))

    def test_pointer_config_precedence_resolves_hooks_with_no_env_override(self):
        """The whole point of the pointer: a hook/decide.sh/state.sh
        invocation with NO MEMCONTINUUM_HOME in its environment (every
        installed hook line, by construction -- F7 also drops the baked
        value) must still land in the custom home, not the fixed default."""
        custom_home = str(Path(self.tmp) / "custom-mc2")
        self.assertEqual(run(SETUP_SH, [
            "--python", VENV_PYTHON, "--claude-dir", self.claude, "--no-model-warm",
        ], self.home, custom_home).returncode, 0)

        repo = git_repo(str(Path(self.tmp) / "repo"))
        env = clean_env(self.home)  # no MEMCONTINUUM_HOME at all
        proc = subprocess.run(
            [MC_BASH, str(DECIDE_SH), "declined", "--repo", repo],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Landed in the CUSTOM home, resolved purely from the pointer at the
        # fixed default -- not a fresh $HOME/.memcontinuum/decisions.tsv.
        self.assertTrue(Path(custom_home, "decisions.tsv").is_file())
        self.assertFalse(Path(self.home, ".memcontinuum", "decisions.tsv").exists())

        # An explicit env override still wins over the pointer (resolution
        # order: env -> pointer -> default).
        env["MEMCONTINUUM_HOME"] = str(Path(self.tmp) / "explicit-override")
        proc = subprocess.run(
            [MC_BASH, str(DECIDE_SH), "declined", "--repo", repo],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(Path(self.tmp, "explicit-override", "decisions.tsv").is_file())

    def test_uninstall_removes_both_config_artifacts_for_custom_home(self):
        custom_home = str(Path(self.tmp) / "custom-mc3")
        self.assertEqual(run(SETUP_SH, [
            "--python", VENV_PYTHON, "--claude-dir", self.claude, "--no-model-warm",
        ], self.home, custom_home).returncode, 0)
        pointer = Path(self.home, ".memcontinuum", "config.sh")
        self.assertTrue(pointer.is_file())

        proc = run(SETUP_SH, ["--claude-dir", self.claude, "--uninstall"],
                   self.home, custom_home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(Path(custom_home, "config.sh").exists())
        self.assertFalse(pointer.exists())

    def test_uninstall_no_env_follows_pointer_to_custom_home(self):
        """R4 regression, round 4 gate: --uninstall used to resolve
        MEMCONTINUUM_HOME as env-or-default at PARSE time only -- with a
        custom-HOME install and NO MEMCONTINUUM_HOME in the uninstalling
        shell's own environment (the realistic case: nobody exports it just
        to uninstall), MEMCONTINUUM_HOME fell straight to the fixed default
        and --uninstall deleted only the pointer, leaving the real
        config.sh (and the door to re-resolving it) behind. Must follow the
        same env -> pointer -> default chain every other consumer uses."""
        custom_home = str(Path(self.tmp) / "custom-mc4")
        self.assertEqual(run(SETUP_SH, [
            "--python", VENV_PYTHON, "--claude-dir", self.claude, "--no-model-warm",
        ], self.home, custom_home).returncode, 0)
        pointer = Path(self.home, ".memcontinuum", "config.sh")
        self.assertTrue(pointer.is_file())
        self.assertTrue(Path(custom_home, "config.sh").is_file())

        # No MEMCONTINUUM_HOME at all this time (mc_home=None) -- only HOME
        # is sandboxed, exactly like a real uninstall invocation.
        proc = run(SETUP_SH, ["--claude-dir", self.claude, "--uninstall"],
                   self.home, None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(Path(custom_home, "config.sh").exists(),
                          "the real config.sh at the custom home must be removed too")
        self.assertFalse(pointer.exists())


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

    def test_partial_wiring_with_no_row_still_asks(self):
        """F5, updated semantics (was "any ONE of five hooks silences the
        detector" -- a stale fragment left a permanently half-wired repo
        with no route back to health, and enshrined here in a previous
        round). Partial wiring with NO recorded row must ASK, not go quiet:
        partial is the repair path. See test_row_by_wiring_matrix for the
        full grid, including the still-silent full/grandfathered case."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude = Path(repo, ".claude")
        claude.mkdir()
        (claude / "settings.local.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [
                {"hooks": [{"type": "command", "command": "bash …/ledger-post-edit.sh"}]}
            ]}}), encoding="utf-8")
        self.assertIn("hookSpecificOutput", self.detect(repo).stdout)

    def test_row_by_wiring_matrix(self):
        """decision (registry row) and wiring are SEPARATE facts (F5): a row
        is authoritative regardless of what the wiring scan finds, and only
        a FULLY-wired repo with no row is grandfathered silent -- partial
        wiring with no row must always ask."""
        cases = [
            # (row, wiring, expect_silent)
            (None, "none", False),
            (None, "partial", False),
            (None, "full", True),
            ("declined", "none", True),
            ("declined", "partial", True),
            ("declined", "full", True),
            ("wired", "full", True),
        ]
        for row, wiring, expect_silent in cases:
            with self.subTest(row=row, wiring=wiring):
                repo = git_repo(str(Path(self.tmp) / f"repo-{row}-{wiring}"))
                if wiring != "none":
                    TestDecisionRegistry._wire(repo, partial=(wiring == "partial"))
                if row:
                    proc = run(DECIDE_SH, [row, "--repo", repo], self.home, self.mc_home)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                out = self.detect(repo).stdout
                if expect_silent:
                    self.assertEqual(out, "", (row, wiring))
                else:
                    self.assertIn("hookSpecificOutput", out, (row, wiring))

    def test_unterminated_last_tsv_line_is_still_read(self):
        """F9: decisions.tsv rows without a `|| [ -n "$line" ]` read guard
        silently drop a final line with no trailing newline -- and that row
        is always the MOST RECENTLY written one, since decide.sh appends."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        Path(self.mc_home).mkdir(parents=True, exist_ok=True)
        Path(self.mc_home, "decisions.tsv").write_text(
            "# c\n# k\n%s\tdeclined\t2026-01-01\t" % repo,  # no trailing \n
            encoding="utf-8",
        )
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
                [MC_BASH, str(DETECT_SH)], input=payload, capture_output=True,
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
    def _wire(repo, partial=False):
        """Give a test repo real-looking hook wiring so `decide wired`
        accepts (partial=False) or so it reads as wiring=partial
        (partial=True: only the first of the five, matching how a broken or
        half-finished install actually looks)."""
        claude = Path(repo, ".claude")
        claude.mkdir(exist_ok=True)
        # decide.sh's wired check requires ALL FIVE write-side hooks on
        # "command" lines (round-3 finding 3), so the fixture wires them all
        # unless a partial fixture was asked for.
        basenames = (
            "ledger-post-edit.sh", "precompact-persist.sh",
            "sessionstart-remind.sh", "userprompt-remind.sh",
            "sessionend-stamp.sh",
        )
        if partial:
            basenames = basenames[:1]
        items = [{"type": "command", "command": f"bash x/{b}"} for b in basenames]
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

    def fields_of(self, repo):
        out = run(STATE_SH, [str(repo)], self.home, self.mc_home).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def stats_line_of(self, repo):
        out = run(STATE_SH, [str(repo)], self.home, self.mc_home).stdout
        for line in out.splitlines():
            if line.startswith("stats:"):
                return line
        return None

    def update_line_of(self, repo):
        out = run(STATE_SH, [str(repo)], self.home, self.mc_home).stdout
        for line in out.splitlines():
            if line.startswith("update:"):
                return line
        return None

    @staticmethod
    def _wire_stamped(repo, project, rendered):
        """Like _wire, but with a MEMCONTINUUM_PROJECT identity marker and a
        MEMCONTINUUM_RENDERED stamp on every command line -- the shape D1
        (updater workstream) actually renders, needed to exercise D5's
        stamp-vs-engine comparison (the bare `_wire` fixture above predates
        the stamp entirely, which is itself one of the cases below)."""
        claude = Path(repo, ".claude")
        claude.mkdir(exist_ok=True)
        basenames = (
            "ledger-post-edit.sh", "precompact-persist.sh",
            "sessionstart-remind.sh", "userprompt-remind.sh",
            "sessionend-stamp.sh",
        )
        items = [
            {"type": "command",
             "command": f"MEMCONTINUUM_RENDERED={rendered} MEMCONTINUUM_PROJECT={project} bash x/{b}"}
            for b in basenames
        ]
        (claude / "settings.local.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [{"hooks": items}]}}),
            encoding="utf-8")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_stats_hint_line_names_the_liveness_command(self):
        """Deliverable 3 (liveness metric): state.sh's own contract stays
        python-free (it never runs python itself) but must still print the
        exact command a human/agent would run to check this repo's
        read/write liveness -- `<python> <engine>/memidx.py stats
        --project <name> --days 7 [--store <store>]`."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude = Path(repo, ".claude")
        claude.mkdir()
        basenames = (
            "ledger-post-edit.sh", "precompact-persist.sh",
            "sessionstart-remind.sh", "userprompt-remind.sh",
            "sessionend-stamp.sh",
        )
        items = [
            {"type": "command", "command":
                f"MEMCONTINUUM_ROOT=/store/proj-a MEMCONTINUUM_PROJECT=proj-a bash x/{b}"}
            for b in basenames
        ]
        (claude / "settings.local.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [{"hooks": items}]}}, indent=2),
            encoding="utf-8")

        line = self.stats_line_of(repo)
        self.assertIsNotNone(line)
        # python/engine/store/project are all single-quoted (round-2
        # review finding: copy-pasteable even when a path has a space, or
        # the project value is the literal "(unknown)") -- match on the
        # substring, not a bare unquoted equality.
        self.assertIn(VENV_PYTHON, line)
        self.assertIn("memidx.py' stats", line)
        self.assertIn("--project 'proj-a'", line)
        self.assertIn("--days 7", line)
        self.assertIn("--store '/store/proj-a'", line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_stats_hint_line_present_even_when_undecided_and_unwired(self):
        """No wiring at all yet -- must still print SOME usable hint
        (python-free engine fallback via $SCRIPT_DIR/.., project falls back
        to the repo's own basename), never crash or omit the line."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        line = self.stats_line_of(repo)
        self.assertIsNotNone(line)
        self.assertIn("memidx.py' stats", line)
        self.assertIn("--project", line)
        self.assertNotIn("--store", line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_stats_hint_prefers_registry_project_when_wiring_is_unusable(self):
        """Round-2 Codex gate item 13 (finding, MAJOR): a DECIDED but
        currently-UNWIRED repo (hooks missing/broken after the decision
        was recorded -- the state where a liveness check matters most) is
        exactly the case live wiring resolution cannot help with. The
        registry's own recorded project (from `decide.sh wired --project
        NAME`) must win the hint over a basename fallback."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire(repo)  # full wiring, required for `decide wired` to accept
        proc = run(
            DECIDE_SH, ["wired", "--repo", repo, "--store", "/store/registered", "--project", "registered-proj"],
            self.home, self.mc_home,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        # Now break the wiring -- decision=wired persists (the recorded
        # answer is authoritative), but live wiring can no longer resolve
        # ANY project on its own.
        (Path(repo) / ".claude" / "settings.local.json").unlink()

        fields = self.fields_of(repo)
        self.assertEqual(fields.get("decision"), "wired")
        self.assertEqual(fields.get("wiring"), "none")
        self.assertNotIn("project", fields, "live wiring must have nothing to report here")

        line = self.stats_line_of(repo)
        self.assertIn("--project 'registered-proj'", line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_decision_and_wiring_are_reported_as_separate_facts(self):
        """F5: state.sh used to collapse both into one `state=` line. A
        declined row on a still-fully-wired repo (hooks never removed after
        the decline) must show BOTH facts, and `state=` must side with the
        recorded decision, not the wiring."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire(repo)
        run(DECIDE_SH, ["declined", "--repo", repo], self.home, self.mc_home)
        fields = self.fields_of(repo)
        self.assertEqual(fields.get("decision"), "declined")
        self.assertEqual(fields.get("wiring"), "full")
        self.assertEqual(fields.get("state"), "declined")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_partial_wiring_with_no_row_reports_partial_wired(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire(repo, partial=True)
        fields = self.fields_of(repo)
        self.assertEqual(fields.get("decision"), "none")
        self.assertEqual(fields.get("wiring"), "partial")
        self.assertEqual(fields.get("state"), "partial-wired")
        self.assertIn("precompact-persist.sh", fields.get("missing", ""))

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_store_and_project_come_from_one_coherent_hook_entry(self):
        """A repo's .claude carrying more than one project's wiring (the
        two-projects-one-claude-dir topology -- real installs DO add a
        second project's hooks alongside a first rather than replacing them,
        see TestTwoProjectsOneClaudeDir in test_repo_init.py) used to `head
        -1` the STORE and PROJECT fields INDEPENDENTLY, each with its own
        quoted-first/bareword-fallback sed pass. Constructed so the two
        passes pick DIFFERENT entries: beta's line (first in the file) has
        ROOT unquoted (bareword pass) and PROJECT quoted; alpha's line
        (second) has ROOT quoted and PROJECT unquoted. The OLD
        independent-head-1 code would report store=/store/alpha (the only
        QUOTED ROOT, from alpha's line) paired with project=beta (the only
        QUOTED PROJECT, from beta's line) -- an incoherent pair belonging to
        neither project. Real settings files are indent=2 (one "command" per
        line -- scripts/repo-init.sh:612, memcontinuum-setup.sh:189), which
        is what makes the per-line matching in mc_first_wired_command
        meaningful; this fixture matches that shape."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude = Path(repo, ".claude")
        claude.mkdir()
        items = [
            {"type": "command", "command":
                "MEMCONTINUUM_ROOT=/store/beta MEMCONTINUUM_PROJECT='beta' bash x/ledger-post-edit.sh"},
            {"type": "command", "command":
                "MEMCONTINUUM_ROOT='/store/alpha' MEMCONTINUUM_PROJECT=alpha bash x/ledger-post-edit.sh"},
        ]
        (claude / "settings.local.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [{"hooks": items}]}}, indent=2),
            encoding="utf-8")
        fields = self.fields_of(repo)
        # Coherent: both from beta's line, the FIRST entry -- not the
        # cross-project mismatch (store=alpha, project=beta) the old
        # independent-per-field head -1 would have produced.
        self.assertEqual(fields.get("store"), "/store/beta")
        self.assertEqual(fields.get("project"), "beta")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_registry_row_project_wins_over_first_found_in_two_project_claude_dir(self):
        """R6 regression, round 4 gate (Codex): with two projects (alpha,
        beta) wired into the SAME .claude, and a decision row recorded for
        beta specifically (decide.sh wired --store .../beta --project beta),
        state.sh must report BETA's store/project -- not alpha's, even
        though alpha's entries sort first in the settings file (the old
        mc_first_wired_command-only path always returned the first match
        regardless of which project the repo's own row actually names)."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude = Path(repo, ".claude")
        claude.mkdir()
        basenames = (
            "ledger-post-edit.sh", "precompact-persist.sh",
            "sessionstart-remind.sh", "userprompt-remind.sh",
            "sessionend-stamp.sh",
        )
        items = []
        for project, store in (("alpha", "/store/alpha"), ("beta", "/store/beta")):
            for b in basenames:
                items.append({
                    "type": "command",
                    "command": f"MEMCONTINUUM_ROOT={store} MEMCONTINUUM_PROJECT={project} bash x/{b}",
                })
        (claude / "settings.local.json").write_text(
            json.dumps({"hooks": {"PostToolUse": [{"hooks": items}]}}, indent=2),
            encoding="utf-8")

        # Without a row, first-found (alpha, wired first) wins -- sanity
        # check the OTHER branch still behaves as before this fix.
        fields = self.fields_of(repo)
        self.assertEqual(fields.get("project"), "alpha")
        self.assertEqual(fields.get("project_source"), "wiring")

        proc = run(DECIDE_SH, ["wired", "--repo", repo, "--store", "/store/beta", "--project", "beta"],
                   self.home, self.mc_home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        fields = self.fields_of(repo)
        self.assertEqual(fields.get("decision"), "wired")
        self.assertEqual(fields.get("store"), "/store/beta")
        self.assertEqual(fields.get("project"), "beta")
        self.assertEqual(fields.get("project_source"), "registry")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_unterminated_last_tsv_line_state_sh(self):
        """F9, same guard as detect.sh, for state.sh's own reader."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        Path(self.mc_home, "decisions.tsv").write_text(
            "# c\n# k\n%s\tdeclined\t2026-01-01\t" % repo,  # no trailing \n
            encoding="utf-8",
        )
        self.assertEqual(self.state_of(repo), "declined")

    def test_wired_is_refused_when_the_repo_has_no_hook_wiring(self):
        """A wired row silences the detector forever whether or not scripts/repo-init.sh
        ever succeeded -- the one write that can lie (round-2 finding 5)."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        proc = run(DECIDE_SH, ["wired", "--repo", repo], self.home, self.mc_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stderr)
        self.assertFalse(Path(self.mc_home, "decisions.tsv").exists())

    def test_wired_refusal_names_the_missing_hooks_when_partial(self):
        """F5: `wired` requires wiring=full; on refusal it names exactly
        which of the five basenames are absent, not just "no wiring"."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire(repo, partial=True)  # only ledger-post-edit.sh
        proc = run(DECIDE_SH, ["wired", "--repo", repo], self.home, self.mc_home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stderr)
        for base in ("precompact-persist.sh", "sessionstart-remind.sh",
                     "userprompt-remind.sh", "sessionend-stamp.sh"):
            self.assertIn(base, proc.stderr)

    def test_wired_declined_forget_require_an_explicit_repo(self):
        """F2: these three actions used to default to $PWD -- every
        documented SKILL.md command passed no repo, so a shell sitting in
        the engine checkout recorded "wired" against the ENGINE's key while
        the repo actually meant stayed undecided forever. No safe default
        for a write that silences a repo permanently."""
        for action in ("wired", "declined", "forget"):
            proc = run(DECIDE_SH, [action], self.home, self.mc_home)
            self.assertNotEqual(proc.returncode, 0, action)
            self.assertIn("requires an explicit repo", proc.stderr, action)
        # state (read-only) keeps its $PWD default -- unaffected by F2.
        repo = git_repo(str(Path(self.tmp) / "repo"))
        proc = subprocess.run(
            [MC_BASH, str(STATE_SH)], capture_output=True, text=True,
            env=clean_env(self.home, self.mc_home), cwd=repo,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

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

    # --- D5 (updater workstream): state.sh's stamp-vs-engine hint --------

    @staticmethod
    def _engine_sha():
        out = subprocess.run(
            ["git", "-C", str(TOOLS_DIR), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out or "unknown"

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_no_update_hint_when_stamp_matches_engine(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire_stamped(repo, "proj", self._engine_sha())
        run(DECIDE_SH, ["wired", "--repo", repo, "--project", "proj"], self.home, self.mc_home)
        self.assertIsNone(self.update_line_of(repo))

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_update_hint_when_stamp_is_stale(self):
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire_stamped(repo, "proj", "deadbee")
        run(DECIDE_SH, ["wired", "--repo", repo, "--project", "proj"], self.home, self.mc_home)
        line = self.update_line_of(repo)
        self.assertIsNotNone(line)
        self.assertIn("rendered by deadbee", line)
        self.assertIn(f"engine at {self._engine_sha()}", line)
        self.assertIn("scripts/memcontinuum-update.sh", line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_update_hint_when_stamp_is_absent_pre_d1_render(self):
        """A pre-D1 render has no MEMCONTINUUM_RENDERED token at all --
        reads as "unknown", same as scripts/repo-init.sh's own fallback for
        a non-git engine checkout, never a crash."""
        self.assertEqual(self.bootstrap().returncode, 0)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        self._wire(repo)  # the bare, pre-stamp fixture (no MEMCONTINUUM_RENDERED)
        run(DECIDE_SH, ["wired", "--repo", repo], self.home, self.mc_home)
        line = self.update_line_of(repo)
        self.assertIsNotNone(line)
        self.assertIn("rendered by unknown", line)


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


SKILL_MD = TOOLS_DIR / "skills" / "memcontinuum" / "SKILL.md"


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestSkillSnippetResolvesEngineViaPointer(BootstrapCase):
    """R2 regression, round 4 gate: skills/memcontinuum/SKILL.md documents
    a bash snippet (step 1, "Read the current state") that sources
    config.sh and reads MEMCONTINUUM_ENGINE straight off it -- a single
    source with no follow-through left ENGINE empty under a custom-HOME
    install, since the file at the fixed default is only a POINTER there.
    Tests the ACTUAL snippet text extracted from the doc, not a
    reimplementation of it -- a doc/code drift would otherwise go
    undetected exactly like the bug this fixes."""

    def _extract_snippet(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        marker = "## 1. Read the current state before saying anything"
        after = text[text.index(marker):]
        start = after.index("```bash\n") + len("```bash\n")
        end = after.index("```", start)
        return after[start:end]

    def test_snippet_resolves_engine_via_pointer_at_custom_home(self):
        custom_home = str(Path(self.tmp) / "skill-custom-mc")
        self.assertEqual(run(SETUP_SH, [
            "--python", VENV_PYTHON, "--claude-dir", self.claude, "--no-model-warm",
        ], self.home, custom_home).returncode, 0)
        self.assertTrue(Path(self.home, ".memcontinuum", "config.sh").is_file())

        snippet = self._extract_snippet()
        # Drop the final `bash "$ENGINE/scripts/memcontinuum-state.sh" REPO`
        # invocation line (no real REPO in this test) and print ENGINE
        # instead -- everything ABOVE that line is what's under test.
        lines = [ln for ln in snippet.splitlines() if "memcontinuum-state.sh" not in ln]
        probe = "\n".join(lines) + '\nprintf "%s" "$MEMCONTINUUM_ENGINE"\n'

        env = clean_env(self.home)  # no MEMCONTINUUM_HOME at all, like a real assistant shell
        proc = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, env=env)
        self.assertEqual(proc.stdout.strip(), str(TOOLS_DIR), proc.stderr)


if __name__ == "__main__":
    unittest.main()
