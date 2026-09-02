"""Tests for the updater workstream: scripts/memcontinuum-update.sh (D3), the
`memcontinuum-decide.sh wired` extensions that feed it (D2), and D2's
old/new registry-row parsing. D1 (stamp) and D4 (rules file) have their own
coverage in test_repo_init.py; D5 (state.sh's drift hint) in test_setup.py.

Same conventions as test_repo_init.py/test_setup.py: real scripts run via
subprocess, HOME and MEMCONTINUUM_HOME sandboxed to fresh temp dirs per
test, nothing here ever touches the real machine's ~/.claude or
~/.memcontinuum.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
DECIDE_SH = TOOLS_DIR / "scripts" / "memcontinuum-decide.sh"
UPDATE_SH = TOOLS_DIR / "scripts" / "memcontinuum-update.sh"
INSTALL_SH = TOOLS_DIR / "scripts" / "repo-init.sh"
SETUP_SH = TOOLS_DIR / "memcontinuum-setup.sh"

# Same seam tests/test_write_hooks.py uses: tests/run_bash32.sh sets MC_BASH to
# a real bash 3.2.57 binary so these scripts are exercised under the actual
# interpreter the macOS port targets. The scripts these invoke shell out to
# each other through "$BASH", so the nested calls follow automatically.
MC_BASH = os.environ.get("MC_BASH", "bash")
VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
_SKIP_NO_VENV = (
    "set $MEMCONTINUUM_PYTHON to a venv python with fastembed/PyYAML "
    "installed to run these tests (see README.md)"
)


def clean_env(home, extra=None):
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MEMCONTINUUM_"):
            del env[k]
    env.pop("PYTHONPATH", None)
    env["HOME"] = home
    if VENV_PYTHON:
        env["MEMCONTINUUM_PYTHON"] = VENV_PYTHON
    if extra:
        env.update(extra)
    return env


def run(script, args, home, timeout=120, stdin=None, cwd=None):
    return subprocess.run(
        [MC_BASH, str(script)] + args,
        input=stdin, capture_output=True, text=True,
        env=clean_env(home), timeout=timeout, cwd=cwd or home,
    )


def git_repo(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                     "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True)
    return path


def engine_sha(root=None, scope="repo"):
    """The stamp this checkout renders with -- asked of the one function that
    computes it (mc_render_fingerprint), never recomputed here. A test that
    re-derives a value it is checking only proves the two copies agree."""
    root = str(root or TOOLS_DIR)
    return subprocess.run(
        [MC_BASH, "-c",
         '. "$1"/scripts/mc-registry-lib.sh; mc_render_fingerprint "$2" "$1"; '
         'printf "%s" "$MC_RENDER_FINGERPRINT"',
         "_", root, scope],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def marked_store(path):
    """The cheapest thing mc_is_marked_store accepts: a git working tree with
    a topics/ directory. Used where a test needs a store that EXISTS but does
    not need a full install to have produced it."""
    Path(path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    Path(path, "topics").mkdir(exist_ok=True)
    return path


def decisions_tsv(home):
    return Path(home, ".memcontinuum", "decisions.tsv")


def write_row(home, key, decision, note=""):
    path = decisions_tsv(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ""
    if not path.exists():
        header = ("# MemContinuum per-repo decisions -- written only by "
                   "memcontinuum-decide.sh\n# key\tdecision\tdate\tnote\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(header)
        f.write(f"{key}\twired\t2026-08-01\t{note}\n" if decision == "wired"
                 else f"{key}\t{decision}\t2026-08-01\t{note}\n")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class UpdateTestBase(unittest.TestCase):
    """Sets up one real, fully-installed-and-decided repo (current-format
    registry row: claude-dirs=/code-roots=/langs=/never= all present)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")
        proc = run(INSTALL_SH, [
            "--project", "proj", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", "python", "--never-ext", ".cs",
            "--non-interactive",
        ], self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        proc = run(DECIDE_SH, [
            "wired", "--repo", self.repo, "--store", self.store, "--project", "proj",
            "--claude-dir", self.claude_dir, "--code-root", self.code_root,
            "--langs", "python", "--never-ext", ".cs",
        ], self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def settings_text(self):
        return Path(self.claude_dir, "settings.local.json").read_text()

    def table_rows(self, out):
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertTrue(lines, out)
        header = lines[0].split("\t")
        self.assertEqual(header, ["repo", "claude-dir", "stamped", "engine", "store-match", "rules", "action"])
        return [dict(zip(header, l.split("\t"))) for l in lines[1:]]


class TestDecideNewFlags(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-decide-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_wired_note_records_all_new_fields_semicolon_joined(self):
        """INC-0104: one project, two claude-dirs -- both real (mustpass the
        wiring check), same store/project."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude_a = str(Path(repo) / ".claude")
        claude_b = str(Path(self.tmp) / "other-claude")
        store = str(Path(self.tmp) / "store")
        code_root = str(Path(self.tmp) / "code")
        os.makedirs(code_root, exist_ok=True)
        for cd in (claude_a, claude_b):
            proc = run(INSTALL_SH, ["--project", "p", "--store", store,
                                     "--claude-dir", cd, "--non-interactive"], self.home)
            assert proc.returncode == 0, proc.stdout + proc.stderr
        proc = run(DECIDE_SH, [
            "wired", "--repo", repo, "--store", store, "--project", "p",
            "--claude-dir", claude_a, "--claude-dir", claude_b,
            "--code-root", "/a", "--code-root", "/b",
            "--langs", "python,swift", "--never-ext", ".cs,.h",
        ], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn(f"claude-dirs={claude_a};{claude_b}", note)
        self.assertIn("code-roots=/a;/b", note)
        self.assertIn("langs=python;swift", note)
        self.assertIn("never=.cs;.h", note)
        # store=/project= stay first, unconditional -- back-compat with the
        # pre-D2 row shape.
        self.assertIn(f"store={Path(self.tmp) / 'store'} project=p", note)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_repeatable_claude_dir_requires_every_one_fully_wired(self):
        """INC-0104: one project, two claude-dirs. `wired` must refuse
        unless BOTH are fully wired -- recording it with only one checked
        would recreate "nothing tracks the set of claude-dirs a project's
        wiring lives in"."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude_a = str(Path(repo) / ".claude")
        claude_b = str(Path(self.tmp) / "other-claude")
        run(INSTALL_SH, ["--project", "p", "--store", str(Path(self.tmp) / "store"),
                          "--claude-dir", claude_a, "--non-interactive"], self.home)
        # claude_b is never installed -- unwired.
        proc = run(DECIDE_SH, [
            "wired", "--repo", repo, "--claude-dir", claude_a, "--claude-dir", claude_b,
        ], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("REFUSED", proc.stdout + proc.stderr)
        self.assertNotIn("wired", decisions_tsv(self.home).read_text() if decisions_tsv(self.home).exists() else "")

    def test_old_format_row_still_parses_via_state_and_update(self):
        """A hand-written pre-D2 row (store=/project= only, no
        claude-dirs=/code-roots=/langs=/never=) must not confuse the
        updater -- it reads as a legacy row (D3's migrate path), not a
        parse error."""
        repo = git_repo(str(Path(self.tmp) / "repo"))
        os.makedirs(str(Path(repo) / ".claude"), exist_ok=True)
        write_row(self.home, repo, "wired", note=f"store=/nowhere project=p")
        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("legacy rows found", proc.stdout + proc.stderr)


class TestRecordDecisionFlag(unittest.TestCase):
    """D2: repo-init.sh's --record-decision (off by default -- recording
    consent is the skill's/human's job, never a hook's or a bare install's).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-record-decision-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_off_by_default_records_nothing(self):
        proc = run(INSTALL_SH, ["--project", "p", "--store", self.store,
                                 "--claude-dir", str(Path(self.repo) / ".claude"),
                                 "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(decisions_tsv(self.home).exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_records_wired_when_given(self):
        proc = run(INSTALL_SH, ["--project", "p", "--store", self.store,
                                 "--claude-dir", str(Path(self.repo) / ".claude"),
                                 "--non-interactive", "--record-decision"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("decision recorded", proc.stdout)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn(f"store={self.store} project=p", note)
        self.assertIn(f"claude-dirs={self.repo}/.claude", note)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_dry_run_records_nothing_even_with_the_flag(self):
        proc = run(INSTALL_SH, ["--project", "p", "--store", self.store,
                                 "--claude-dir", str(Path(self.repo) / ".claude"),
                                 "--non-interactive", "--record-decision", "--dry-run"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(decisions_tsv(self.home).exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_second_claude_dir_unions_rather_than_replaces(self):
        """INC-0104: a project's second claude-dir must not erase the
        registry's memory of the first."""
        claude_a = str(Path(self.repo) / ".claude")
        claude_b = str(Path(self.repo) / "subdir" / ".claude")
        for cd in (claude_a, claude_b):
            proc = run(INSTALL_SH, ["--project", "p", "--store", self.store,
                                     "--claude-dir", cd, "--non-interactive", "--record-decision"], self.home)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn(f"claude-dirs={claude_a};{claude_b}", note)
        # exactly one row for this repo, not two.
        rows = [l for l in decisions_tsv(self.home).read_text().splitlines()
                if l and not l.startswith("#")]
        self.assertEqual(len(rows), 1, rows)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_working_dir_claude_dir_outside_the_repo_notes_but_does_not_fail(self):
        outside_claude = str(Path(self.tmp) / "session-home" / ".claude")
        proc = run(INSTALL_SH, ["--project", "p", "--store", self.store,
                                 "--claude-dir", outside_claude, "--non-interactive", "--record-decision"], self.home)
        self.assertEqual(proc.returncode, 0,
                          "the install itself must still succeed: " + proc.stdout + proc.stderr)
        self.assertIn("not inside a git working tree", proc.stdout + proc.stderr)
        self.assertFalse(decisions_tsv(self.home).exists())


class TestUpdateWalkStaleAndOk(UpdateTestBase):
    def test_freshly_installed_repo_reports_ok(self):
        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["action"], "ok")
        self.assertEqual(rows[0]["stamped"], engine_sha())
        self.assertEqual(rows[0]["store-match"], "yes")
        self.assertEqual(rows[0]["rules"], "ok")

    def test_dry_run_is_the_default_and_writes_nothing(self):
        settings_before = self.settings_text()
        # Drift the stamp by hand so there is something an --apply WOULD fix.
        drifted = self.settings_text().replace(f"MEMCONTINUUM_RENDERED={engine_sha()}", "MEMCONTINUUM_RENDERED=deadbee")
        Path(self.claude_dir, "settings.local.json").write_text(drifted)
        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(rows[0]["action"], "stale")
        self.assertEqual(self.settings_text(), drifted, "dry-run must write nothing")

    def test_apply_re_renders_a_stale_claude_dir(self):
        drifted = self.settings_text().replace(f"MEMCONTINUUM_RENDERED={engine_sha()}", "MEMCONTINUUM_RENDERED=deadbee")
        Path(self.claude_dir, "settings.local.json").write_text(drifted)
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"MEMCONTINUUM_RENDERED={engine_sha()}", self.settings_text())
        # Re-walk: clean now.
        proc2 = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc2.stdout)
        self.assertEqual(rows[0]["action"], "ok")

    def test_store_mismatch_detected_and_fixed_by_apply(self):
        """INC-0104: a stamp match alone cannot catch a store renamed under
        the same engine version -- MEMCONTINUUM_ROOT in the rendered
        settings must be compared against the registry row's own store."""
        text = self.settings_text().replace(self.store, "/some/other/store")
        Path(self.claude_dir, "settings.local.json").write_text(text)
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(rows[0]["store-match"], "no")
        self.assertEqual(rows[0]["action"], "store-mismatch")

        proc2 = run(UPDATE_SH, ["--apply"], self.home)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertIn(f"MEMCONTINUUM_ROOT={self.store}", self.settings_text())

    def test_rules_missing_detected_and_fixed_by_apply(self):
        rules_path = Path(self.claude_dir, "rules", "memcontinuum.md")
        rules_path.unlink()
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(rows[0]["rules"], "missing")
        self.assertEqual(rows[0]["action"], "rules-missing")

        proc2 = run(UPDATE_SH, ["--apply"], self.home)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertTrue(rules_path.is_file())

    def test_rules_stale_detected_and_fixed_by_apply(self):
        rules_path = Path(self.claude_dir, "rules", "memcontinuum.md")
        text = rules_path.read_text().replace(
            f"<!-- memcontinuum-rendered: {engine_sha()} -->",
            "<!-- memcontinuum-rendered: deadbee -->",
        )
        rules_path.write_text(text)
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(rows[0]["rules"], "stale")
        self.assertEqual(rows[0]["action"], "rules-stale")

        proc2 = run(UPDATE_SH, ["--apply"], self.home)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertIn(f"<!-- memcontinuum-rendered: {engine_sha()} -->", rules_path.read_text())

    def test_rules_foreign_is_reported_and_apply_skips_it_without_crashing(self):
        rules_path = Path(self.claude_dir, "rules", "memcontinuum.md")
        rules_path.write_text("# hand-written, not ours\n")
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(rows[0]["rules"], "foreign")
        self.assertEqual(rows[0]["action"], "rules-foreign")

        proc2 = run(UPDATE_SH, ["--apply"], self.home)
        # --apply asked for this dir to be re-rendered and it was not: a skip
        # is still work left undone, and the exit code has to say so.
        self.assertNotEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertIn("SKIPPED", proc2.stdout + proc2.stderr)
        self.assertEqual(rules_path.read_text(), "# hand-written, not ours\n")

    def test_missing_wiring_is_reported_as_no_wiring_and_never_auto_applied(self):
        Path(self.claude_dir, "settings.local.json").unlink()
        proc = run(UPDATE_SH, ["--apply"], self.home)
        # The documented exception to "--apply fails on anything left undone":
        # a claude-dir with no wiring at all is a broken install, which the
        # skill repairs by asking a human. Not this command's work to leave
        # undone, so not this command's failure.
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(rows[0]["action"], "no-wiring")
        self.assertFalse(Path(self.claude_dir, "settings.local.json").exists(),
                          "no-wiring is the skill's repair path, not this command's")

    def test_never_touches_a_declined_or_undecided_repo(self):
        declined_repo = git_repo(str(Path(self.tmp) / "declined-repo"))
        run(DECIDE_SH, ["declined", "--repo", declined_repo], self.home)
        # an undecided repo has no row at all -- nothing to even create here.
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(len(rows), 1, rows)  # only the one wired repo from setUp
        self.assertNotIn(declined_repo, proc.stdout)


class TestLegacyRowMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-legacy-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_legacy_row_recovers_code_roots_langs_never_and_rewrites(self):
        proc = run(INSTALL_SH, [
            "--project", "legacy", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", "python", "--never-ext", ".cs",
            "--non-interactive",
        ], self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # A pre-D2-shaped row, hand-written (as decide.sh wrote before this
        # workstream): store=/project= only.
        write_row(self.home, self.repo, "wired", note=f"store={self.store} project=legacy")

        proc = run(UPDATE_SH, [], self.home)
        rows_out = proc.stdout
        self.assertIn("migrate", rows_out)
        self.assertIn("legacy rows found", proc.stdout + proc.stderr)

        # The claude-dir set is named by the human -- the table only proposes
        # the one dir this command can see.
        proc2 = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                                "--claude-dir", self.claude_dir], self.home)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertIn("migrated", proc2.stdout + proc2.stderr)

        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn(f"claude-dirs={self.claude_dir}", note)
        self.assertIn(f"code-roots={self.code_root}", note)
        self.assertIn("langs=python", note)
        self.assertIn("never=.cs", note)

        # Re-walk: no longer legacy, action=ok.
        proc3 = run(UPDATE_SH, [], self.home)
        self.assertNotIn("legacy rows found", proc3.stdout + proc3.stderr)
        self.assertIn("ok", proc3.stdout)

    def test_legacy_row_with_remote_keyed_key_is_unrecoverable_not_a_crash(self):
        """A remote-keyed legacy row (the shape decide.sh writes when the
        repo has an `origin`) carries no path anywhere in the registry --
        this command must report it, not guess a wrong repo's .claude."""
        write_row(self.home, "https://example.invalid/x.git", "wired",
                  note="store=/somewhere project=p")
        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("unrecoverable", proc.stdout)
        self.assertIn("example.invalid", proc.stdout + proc.stderr)


class TestStoreMissingGuard(UpdateTestBase):
    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_apply_never_reseeds_a_store_whose_path_is_gone(self):
        """INC-0104's exact shape: the registry's store path no longer
        exists. --apply must never re-run repo-init against it (that would
        SEED A FRESH STORE there) -- 'stores never touched' holds even under
        --apply."""
        # Make the row stale too, so action != ok and apply is actually
        # attempted (a plain rename with no other drift leaves store-match
        # correct as a STRING, since nothing else disagrees).
        drifted = self.settings_text().replace(f"MEMCONTINUUM_RENDERED={engine_sha()}", "MEMCONTINUUM_RENDERED=deadbee")
        Path(self.claude_dir, "settings.local.json").write_text(drifted)
        os.rename(self.store, self.store + "-renamed-away")

        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SKIPPED", proc.stdout + proc.stderr)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self.assertFalse(Path(self.store).exists(), "must not have re-seeded a store at the old path")


class TestAddLangAndNeverExt(UpdateTestBase):
    def test_requires_explicit_repo(self):
        proc = run(UPDATE_SH, ["--add-lang", "swift"], self.home)
        self.assertNotEqual(proc.returncode, 0)

    def test_dry_run_previews_without_writing(self):
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo, "--dry-run"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dry run", proc.stdout.lower())
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    def test_add_lang_is_additive_and_reapplies(self):
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("langs=python;swift", note, note)
        # never=.cs from setUp must survive -- additive, never a drop.
        self.assertIn("never=.cs", note)
        settings = self.settings_text()
        self.assertIn("'*.py *.swift'", settings)

    def test_never_ext_is_additive_and_reapplies(self):
        proc = run(UPDATE_SH, ["--never-ext", ".h", "--repo", self.repo], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("never=.cs;.h", note, note)
        self.assertIn("langs=python", note)

    def test_unwired_repo_refused(self):
        other = git_repo(str(Path(self.tmp) / "unwired"))
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", other], self.home)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("no wired row", proc.stdout + proc.stderr)


class TestMachineFlag(UpdateTestBase):
    def test_machine_flag_refreshes_the_machine_layer_and_off_by_default(self):
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("refreshing machine layer", proc.stdout)

        proc2 = run(UPDATE_SH, ["--apply", "--machine"], self.home)
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertIn("refreshing machine layer", proc2.stdout)
        self.assertTrue(Path(self.home, ".memcontinuum", "config.sh").is_file())


class TestStoreMissingIsAFirstClassAction(UpdateTestBase):
    """A row whose store path is no longer an existing, marked store is
    `store-missing` in the ACTION column -- even when everything else agrees.
    "Agreement on a corpse": a current stamp and a matching MEMCONTINUUM_ROOT
    string say nothing about whether the store is still there."""

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_renamed_store_reads_store_missing_even_when_the_stamp_agrees(self):
        os.rename(self.store, self.store + "-renamed-away")
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual([r["action"] for r in rows], ["store-missing"], proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_git_repo_without_this_tools_markers_is_store_missing(self):
        """A path that is a git repo but carries none of this tool's markers
        is not this row's store any more either -- re-rendering against it
        would point wiring at an unrelated repository."""
        os.rename(self.store, self.store + "-renamed-away")
        os.makedirs(self.store)
        subprocess.run(["git", "init", "-q", "."], cwd=self.store, check=True)
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual([r["action"] for r in rows], ["store-missing"], proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_apply_creates_nothing_at_the_vanished_store_path(self):
        os.rename(self.store, self.store + "-renamed-away")
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self.assertFalse(Path(self.store).exists(),
                         "--apply must never seed a store at a vanished path")
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_add_lang_refuses_a_vanished_store_and_writes_nothing(self):
        os.rename(self.store, self.store + "-renamed-away")
        before = decisions_tsv(self.home).read_text()
        settings_before = self.settings_text()
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self.assertFalse(Path(self.store).exists())
        self.assertEqual(decisions_tsv(self.home).read_text(), before)
        self.assertEqual(self.settings_text(), settings_before)


class TestNeverExtsSurviveARerender(unittest.TestCase):
    """The never-extension list must survive a legacy-row migration intact:
    an EMPTY list re-renders as exactly `MEMCONTINUUM_NEVER_EXTS=''` (what a
    fresh install writes), and a real list is read back as extensions -- never
    as whatever files happen to sit in the directory the command was run
    from."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-never-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")

    def _install(self, extra):
        proc = run(INSTALL_SH, [
            "--project", "nev", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", "python", "--non-interactive",
        ] + extra, self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    def _migrate(self, cwd=None):
        return run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_dir], self.home, cwd=cwd)

    def _nudge_command(self):
        settings = json.loads(Path(self.claude_dir, "settings.local.json").read_text())
        nudge = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Write"]
        return nudge[0]["hooks"][0]["command"]

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_empty_never_list_migrates_to_an_empty_value_not_quote_garbage(self):
        self._install([])
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=nev")
        proc = self._migrate()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertNotIn("never=''", note, note)
        self.assertNotIn('never="', note, note)
        # An empty never-list is recorded as empty -- the field is either
        # absent or blank, never a literal pair of quote characters.
        for tok in note.split():
            if tok.startswith("never="):
                self.assertEqual(tok, "never=", note)
        self.assertIn("MEMCONTINUUM_NEVER_EXTS='' ", self._nudge_command(),
                      self._nudge_command())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_recovery_does_not_glob_expand_against_the_working_directory(self):
        """`*.md`/`*.txt` on the rendered hook line are EXTENSION PATTERNS, not
        a shell glob to expand: run from a directory holding README.md and
        requirements.txt, the migration must still record `.md`/`.txt`."""
        self._install(["--never-ext", ".md,.txt"])
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=nev")
        cwd = str(Path(self.tmp) / "cwd")
        os.makedirs(cwd, exist_ok=True)
        Path(cwd, "README.md").write_text("x\n")
        Path(cwd, "requirements.txt").write_text("x\n")
        proc = self._migrate(cwd=cwd)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("never=.md;.txt", note, note)
        self.assertNotIn("README.md", note, note)
        self.assertNotIn("requirements.txt", note, note)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_an_unrecoverable_never_value_is_refused_never_written(self):
        """A hand-edited nudge line whose never-list is not a plain extension
        list cannot be migrated by guessing: the human names it with
        --never-ext, or nothing is written."""
        self._install(["--never-ext", ".cs"])
        path = Path(self.claude_dir, "settings.local.json")
        path.write_text(path.read_text().replace(
            "MEMCONTINUUM_NEVER_EXTS='*.cs'",
            "MEMCONTINUUM_NEVER_EXTS='/etc/passwd'"))
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=nev")
        before = decisions_tsv(self.home).read_text()
        proc = self._migrate()
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("migrate-needs-never-exts", proc.stdout + proc.stderr)
        self.assertIn("--set-never-ext", proc.stdout + proc.stderr)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_set_never_ext_completes_that_migration(self):
        self._install(["--never-ext", ".cs"])
        path = Path(self.claude_dir, "settings.local.json")
        path.write_text(path.read_text().replace(
            "MEMCONTINUUM_NEVER_EXTS='*.cs'",
            "MEMCONTINUUM_NEVER_EXTS='/etc/passwd'"))
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=nev")
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_dir,
                               "--set-never-ext", ".cs,.h"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("never=.cs;.h", note, note)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_never_ext_keeps_its_one_additive_meaning(self):
        """--never-ext adds to a row. It never doubled as "here is the whole
        list for the migration" -- that is --set-never-ext. Combining it with
        --apply is refused rather than quietly picking one of the two."""
        self._install([])
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=nev")
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--never-ext", ".h", "--apply", "--repo", self.repo],
                   self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--set-never-ext", proc.stdout + proc.stderr)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)


class TestAddLangValidatesBeforeWriting(UpdateTestBase):
    """O2: the registry row is written only after a successful re-render, and
    an unknown language never reaches the registry at all."""

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_unknown_language_is_refused_with_the_row_untouched(self):
        before = decisions_tsv(self.home).read_text()
        settings_before = self.settings_text()
        proc = run(UPDATE_SH, ["--add-lang", "bogus", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("bogus", proc.stdout + proc.stderr)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)
        self.assertEqual(self.settings_text(), settings_before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_failed_rerender_leaves_the_row_untouched(self):
        """A foreign rules file makes repo-init refuse before it writes
        anything. The registry must not record a language set that was never
        rendered anywhere."""
        rules = Path(self.claude_dir, "rules", "memcontinuum.md")
        rules.write_text("# hand-authored, not ours\n")
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)


class TestMigrationNeverGuessesTheClaudeDirSet(unittest.TestCase):
    """A legacy row records no claude-dirs. One project can have SEVERAL --
    a session-home .claude beside a bare checkout, two homes pointed at one
    store -- and the updater has no way to know how many. It PROPOSES the one
    it can see and refuses to write until a human names the whole set."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-cd-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.claude_a = str(Path(self.repo) / ".claude")
        self.claude_b = str(Path(self.tmp) / "session-home" / ".claude")
        for cd in (self.claude_a, self.claude_b):
            proc = run(INSTALL_SH, ["--project", "two", "--store", self.store,
                                    "--claude-dir", cd, "--non-interactive"], self.home)
            assert proc.returncode == 0, proc.stdout + proc.stderr
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=two")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_plain_apply_refuses_and_writes_nothing(self):
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("migrate-needs-claude-dirs", proc.stdout)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_table_proposes_the_recovered_dir_and_names_the_command(self):
        proc = run(UPDATE_SH, [], self.home)
        rows = self.assertTableProposes(proc)
        self.assertEqual(rows[0]["action"], "migrate-needs-claude-dirs")
        self.assertEqual(rows[0]["claude-dir"], self.claude_a)
        combined = proc.stdout + proc.stderr
        self.assertIn("memcontinuum-decide.sh wired", combined)
        self.assertIn("--claude-dir", combined)

    def assertTableProposes(self, proc):
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        header = lines[0].split("\t")
        return [dict(zip(header, l.split("\t"))) for l in lines[1:]]

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_named_claude_dirs_migrate_and_are_all_walked_afterwards(self):
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--claude-dir", self.claude_b], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn(f"claude-dirs={self.claude_a};{self.claude_b}", note, note)

        again = run(UPDATE_SH, [], self.home)
        rows = self.assertTableProposes(again)
        self.assertEqual([r["claude-dir"] for r in rows],
                         [self.claude_a, self.claude_b], again.stdout)
        self.assertEqual([r["action"] for r in rows], ["ok", "ok"], again.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_named_claude_dir_that_carries_no_wiring_is_refused(self):
        """The migration RECORDS what is already installed. It never wires a
        directory from scratch -- that is the skill's job, with a human."""
        empty = str(Path(self.tmp) / "not-installed")
        os.makedirs(empty, exist_ok=True)
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--claude-dir", empty], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_claude_dir_without_repo_is_refused(self):
        proc = run(UPDATE_SH, ["--apply", "--claude-dir", self.claude_a], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("--repo", proc.stdout + proc.stderr)


class TestMigrationNeverInventsALanguageSet(unittest.TestCase):
    """Wiring rendered before the language set was recorded on the hook line
    has no MEMCONTINUUM_LANG_EXTS at all. That is "unknown", not "none": a
    migration that silently wrote a language-less row would turn off code
    indexing for a project that had it on."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-langs-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")
        proc = run(INSTALL_SH, [
            "--project", "old", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", "python", "--non-interactive",
        ], self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        # Roll the nudge line back to the pre-record shape: the variable is
        # not set at all (distinct from set-and-empty, which IS a recorded
        # answer -- deliberate language-less wiring).
        path = Path(self.claude_dir, "settings.local.json")
        path.write_text(path.read_text().replace("MEMCONTINUUM_LANG_EXTS='*.py' ", ""))
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=old")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_absent_lang_exts_is_migrate_needs_langs_and_writes_nothing(self):
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_dir], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("migrate-needs-langs", proc.stdout)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_failed_rerender_leaves_the_legacy_row_untouched(self):
        """The other half of "rewrite only after a successful re-render": not
        a refusal this command made up front, but the installer actually
        failing part way. The row must be exactly as it was, and the walk must
        exit non-zero."""
        path = Path(self.claude_dir, "settings.local.json")
        # Unparseable JSON, hook command lines intact: the walk still sees
        # wiring here (it greps), so this is a real re-render attempt that
        # fails inside the installer, not a `no-wiring` skip.
        path.write_text(path.read_text() + "\nNOT JSON AT ALL\n")
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_dir,
                               "--langs", "python"], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FAILED", proc.stdout + proc.stderr)
        self.assertEqual(decisions_tsv(self.home).read_text(), before,
                         "a row rewritten after a failed render would describe "
                         "wiring that exists nowhere")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_explicit_langs_completes_the_migration(self):
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_dir,
                               "--langs", "python"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("langs=python", note, note)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_an_explicitly_empty_lang_exts_is_a_recorded_answer_not_a_gap(self):
        """`MEMCONTINUUM_LANG_EXTS=''` means "language-less wiring, on
        purpose" -- it migrates without an explicit --langs."""
        path = Path(self.claude_dir, "settings.local.json")
        path.write_text(path.read_text().replace(
            "MEMCONTINUUM_NEVER_EXTS=", "MEMCONTINUUM_LANG_EXTS='' MEMCONTINUUM_NEVER_EXTS="))
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_dir], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertNotIn("langs=", note, note)


class TestWalkExitCodesAndPrecedence(UpdateTestBase):
    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_rules_foreign_outranks_stale(self):
        """A foreign rules file is never applied -- so it must be what the
        action column SAYS, or --apply would call repo-init just to watch it
        refuse for a reason already known."""
        drifted = self.settings_text().replace(
            f"MEMCONTINUUM_RENDERED={engine_sha()}", "MEMCONTINUUM_RENDERED=deadbee")
        Path(self.claude_dir, "settings.local.json").write_text(drifted)
        Path(self.claude_dir, "rules", "memcontinuum.md").write_text("# not ours\n")
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(rows[0]["action"], "rules-foreign", proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_apply_that_skips_never_calls_repo_init_for_that_dir(self):
        Path(self.claude_dir, "rules", "memcontinuum.md").write_text("# not ours\n")
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertNotIn("applying:", proc.stdout, proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_reporting_walk_still_exits_zero(self):
        Path(self.claude_dir, "rules", "memcontinuum.md").write_text("# not ours\n")
        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("rules-foreign", proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_one_bad_dir_among_several_still_fails_the_walk(self):
        """Partial failure is failure: the table is still printed in full,
        and the exit code reports that not everything got done."""
        other = str(Path(self.tmp) / "second-claude")
        proc = run(INSTALL_SH, ["--project", "proj", "--store", self.store,
                                "--claude-dir", other, "--code-root", self.code_root,
                                "--langs", "python", "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(DECIDE_SH, ["wired", "--repo", self.repo, "--store", self.store,
                               "--project", "proj", "--claude-dir", self.claude_dir,
                               "--claude-dir", other, "--code-root", self.code_root,
                               "--langs", "python", "--never-ext", ".cs"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        Path(other, "rules", "memcontinuum.md").write_text("# not ours\n")
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = self.table_rows(proc.stdout)
        self.assertEqual(len(rows), 2, proc.stdout)


class TestRecordDecisionNeverFlipsADeclinedRow(unittest.TestCase):
    """`declined` is a human's "no". An installer -- even one run with
    --record-decision by a driven flow -- may only record an UNDECIDED repo as
    wired. Reversing a decline is `memcontinuum-decide.sh forget`, typed by
    the person who declined."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-record-declined-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_declined_row_survives_and_the_install_still_succeeds(self):
        proc = run(DECIDE_SH, ["declined", "--repo", self.repo], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(INSTALL_SH, ["--project", "dec", "--store", self.store,
                                "--claude-dir", self.claude_dir, "--non-interactive",
                                "--record-decision"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        text = decisions_tsv(self.home).read_text()
        self.assertIn("declined", text, text)
        self.assertNotIn("wired", text, text)
        self.assertIn("declined", proc.stdout + proc.stderr)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_an_undecided_repo_is_still_recorded(self):
        proc = run(INSTALL_SH, ["--project", "und", "--store", self.store,
                                "--claude-dir", self.claude_dir, "--non-interactive",
                                "--record-decision"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("wired", decisions_tsv(self.home).read_text())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_an_already_wired_row_is_still_updated(self):
        """Re-installing a repo that is already recorded as wired must keep
        working -- the rule is about `declined`, not about idempotence."""
        for _ in range(2):
            proc = run(INSTALL_SH, ["--project", "und", "--store", self.store,
                                    "--claude-dir", self.claude_dir, "--non-interactive",
                                    "--record-decision"], self.home)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("wired", decisions_tsv(self.home).read_text())


class TestPathsWithSpaces(unittest.TestCase):
    """A real store on this machine lives under a path with a space in it.
    The registry note is space-separated `key=value` fields, so a raw space in
    a value would end the field early and the rest would parse as garbage --
    values are percent-encoded on the way in and decoded on the way out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-spaces-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "my repo"))
        self.store = str(Path(self.tmp) / "my store")
        self.code_root = str(Path(self.tmp) / "my code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.claude_dir = str(Path(self.repo) / ".claude")

    def _install_and_record(self):
        proc = run(INSTALL_SH, [
            "--project", "spaced", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", "python", "--never-ext", ".cs",
            "--non-interactive",
        ], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(DECIDE_SH, [
            "wired", "--repo", self.repo, "--store", self.store, "--project", "spaced",
            "--claude-dir", self.claude_dir, "--code-root", self.code_root,
            "--langs", "python", "--never-ext", ".cs",
        ], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_spaced_store_and_claude_dir_round_trip_through_the_registry(self):
        self._install_and_record()
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        # One field per space-separated token: the encoding is what makes
        # that true for a value that itself contains a space.
        fields = dict(t.split("=", 1) for t in note.split("\t")[-1].split() if "=" in t)
        self.assertEqual(set(fields), {"store", "project", "claude-dirs",
                                       "code-roots", "langs", "never"}, note)

        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        rows = [dict(zip(lines[0].split("\t"), l.split("\t"))) for l in lines[1:]]
        self.assertEqual(len(rows), 1, proc.stdout)
        self.assertEqual(rows[0]["claude-dir"], self.claude_dir, proc.stdout)
        self.assertEqual(rows[0]["store-match"], "yes", proc.stdout)
        self.assertEqual(rows[0]["action"], "ok", proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_add_lang_rerenders_a_spaced_path_as_one_argument(self):
        self._install_and_record()
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        settings = Path(self.claude_dir, "settings.local.json").read_text()
        self.assertIn("'*.py *.swift'", settings)
        self.assertIn(self.store, settings)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_value_containing_a_literal_percent_survives_the_round_trip(self):
        weird = str(Path(self.tmp) / "pct %20 dir")
        os.makedirs(weird, exist_ok=True)
        proc = run(INSTALL_SH, ["--project", "pct", "--store", self.store,
                                "--claude-dir", self.claude_dir, "--code-root", weird,
                                "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(DECIDE_SH, ["wired", "--repo", self.repo, "--store", self.store,
                               "--project", "pct", "--claude-dir", self.claude_dir,
                               "--code-root", weird], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ok", proc.stdout)

    def test_a_semicolon_in_a_value_is_refused_not_silently_split(self):
        """`;` separates the elements of a list field -- a value carrying one
        cannot be stored, and a guess would silently split it into two."""
        proc = run(DECIDE_SH, ["wired", "--repo", self.repo,
                               "--store", "/a;b", "--project", "p",
                               "--claude-dir", self.claude_dir], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(";", proc.stdout + proc.stderr)


class TestRulesMarkerComesFromTheTemplate(unittest.TestCase):
    """The rules file's identity marker is the template's own first line.
    Copying it into the two scripts (and the tests) meant four places had to
    be edited in lockstep, and a template whose first line moved on would
    silently make every rendered file read as foreign."""

    TEMPLATE = TOOLS_DIR / "templates" / "memcontinuum-rules.md"

    def marker(self):
        return self.TEMPLATE.read_text().splitlines()[0]

    def test_nothing_outside_the_template_hardcodes_the_marker_text(self):
        """Scripts AND tests: a copy in a test is the same lockstep-edit
        problem, and a test comparing two copies of a string proves only that
        the two copies match."""
        marker = self.marker()
        offenders = []
        for path in sorted(TOOLS_DIR.glob("scripts/*.sh")) + sorted(TOOLS_DIR.glob("tests/*.py")):
            if marker in path.read_text():
                offenders.append(str(path.relative_to(TOOLS_DIR)))
        self.assertEqual(
            offenders, [],
            "these carry a copy of the rules identity marker -- read line 1 of "
            f"templates/memcontinuum-rules.md instead: {offenders}")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_changed_template_marker_still_round_trips(self):
        """Change the template's first line in a copied engine and the whole
        chain -- render, identity check, the updater's rules state -- follows
        it, with no source edit anywhere."""
        tmp = tempfile.mkdtemp(prefix="memcontinuum-marker-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        engine = Path(tmp) / "engine"
        subprocess.run(["git", "-C", str(TOOLS_DIR), "worktree", "list"],
                       capture_output=True)
        shutil.copytree(TOOLS_DIR, engine, symlinks=True, ignore=shutil.ignore_patterns(
            ".git", "fixtures", "tests", "__pycache__", ".venv"))
        tmpl = engine / "templates" / "memcontinuum-rules.md"
        lines = tmpl.read_text().splitlines(keepends=True)
        lines[0] = "<!-- memcontinuum-rules v2 - a different marker -->\n"
        tmpl.write_text("".join(lines))

        home = str(Path(tmp) / "home")
        os.makedirs(home, exist_ok=True)
        repo = git_repo(str(Path(tmp) / "repo"))
        store = str(Path(tmp) / "store")
        claude_dir = str(Path(repo) / ".claude")
        proc = run(engine / "scripts" / "repo-init.sh",
                   ["--project", "mk", "--store", store, "--claude-dir", claude_dir,
                    "--non-interactive"], home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rules = Path(claude_dir, "rules", "memcontinuum.md").read_text()
        self.assertTrue(rules.startswith("<!-- memcontinuum-rules v2 - a different marker -->"),
                        rules[:200])

        proc = run(engine / "scripts" / "memcontinuum-decide.sh",
                   ["wired", "--repo", repo, "--store", store, "--project", "mk",
                    "--claude-dir", claude_dir], home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(engine / "scripts" / "memcontinuum-update.sh", [], home)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        rows = [dict(zip(lines[0].split("\t"), l.split("\t"))) for l in lines[1:]]
        self.assertEqual(rows[0]["rules"], "ok", proc.stdout)
        self.assertNotEqual(rows[0]["rules"], "foreign", proc.stdout)


class TestPartiallyRenderedLanguagesAreReported(unittest.TestCase):
    """Recovering languages from extension globs: a language counts as
    rendered only when ALL of its extensions are on the line. One that is
    half-present used to be dropped in silence -- the migration would then
    record a language set smaller than what is actually wired, and say
    nothing about it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-partial-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")

    def _lang_exts(self, lang):
        out = subprocess.run(
            [VENV_PYTHON, "-c",
             "import sys,chunkers;"
             "print(' '.join('*'+e for e in sorted(chunkers.LANGUAGE_TABLE[sys.argv[1]]['extensions'])))",
             lang],
            cwd=str(TOOLS_DIR), capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": ""})
        return out.stdout.strip().split()

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_half_rendered_language_is_named_not_silently_dropped(self):
        multi = None
        for lang in ("swift", "python"):
            if len(self._lang_exts(lang)) > 1:
                multi = lang
                break
        if multi is None:
            self.skipTest("no language in this engine's table has more than one extension")
        exts = self._lang_exts(multi)
        proc = run(INSTALL_SH, [
            "--project", "part", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", multi, "--non-interactive",
        ], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        path = Path(self.claude_dir, "settings.local.json")
        path.write_text(path.read_text().replace(
            "MEMCONTINUUM_LANG_EXTS='%s'" % " ".join(exts),
            "MEMCONTINUUM_LANG_EXTS='%s'" % exts[0]))
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=part")
        proc = run(UPDATE_SH, [], self.home)
        combined = proc.stdout + proc.stderr
        self.assertIn("partially", combined.lower(), combined)
        self.assertIn(multi, combined, combined)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_an_extension_matching_no_language_is_named_too(self):
        proc = run(INSTALL_SH, [
            "--project", "part", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", "python", "--non-interactive",
        ], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        path = Path(self.claude_dir, "settings.local.json")
        path.write_text(path.read_text().replace(
            "MEMCONTINUUM_LANG_EXTS='*.py'", "MEMCONTINUUM_LANG_EXTS='*.py *.zz'"))
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=part")
        proc = run(UPDATE_SH, [], self.home)
        combined = proc.stdout + proc.stderr
        self.assertIn("*.zz", combined, combined)


class TestRenderFingerprint(unittest.TestCase):
    """The stamp is a fingerprint of the RENDER INPUTS, not the engine's HEAD
    commit. That is the whole distinction the update command promises: a fix
    that only touches a script reaches every wired repo the moment you pull,
    so it must leave every repo `ok`; a fix that changes what gets rendered
    must flip them `stale`. A HEAD sha changes on both, which made the promise
    false -- every commit to anything marked every repo on the machine stale."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-fp-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = Path(self.tmp) / "engine"
        shutil.copytree(TOOLS_DIR, self.engine, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", "fixtures", "tests", "__pycache__", ".venv"))

    def fingerprint(self, scope="repo", root=None):
        root = str(root or self.engine)
        proc = subprocess.run(
            [MC_BASH, "-c",
             '. "$1"/scripts/mc-registry-lib.sh; mc_render_fingerprint "$2" "$1"; '
             'printf "%s" "$MC_RENDER_FINGERPRINT"',
             "_", root, scope],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def test_it_is_twelve_hex_characters_and_stable(self):
        for scope in ("repo", "machine"):
            with self.subTest(scope=scope):
                fp = self.fingerprint(scope)
                self.assertRegex(fp, r"^[0-9a-f]{12}$", fp)
                self.assertEqual(fp, self.fingerprint(scope), "must be deterministic")

    def test_the_two_scopes_are_different_fingerprints(self):
        self.assertNotEqual(self.fingerprint("repo"), self.fingerprint("machine"))

    def test_an_unknown_scope_is_refused_rather_than_silently_hashing_something(self):
        proc = subprocess.run(
            [MC_BASH, "-c",
             '. "$1"/scripts/mc-registry-lib.sh; mc_render_fingerprint bogus "$1"; '
             'printf "%s" "$MC_RENDER_FINGERPRINT"',
             "_", str(self.engine)],
            capture_output=True, text=True)
        self.assertEqual(proc.stdout.strip(), "unknown", proc.stdout + proc.stderr)

    def test_a_machine_layer_input_does_not_move_the_repo_fingerprint(self):
        """The whole point of the split. memcontinuum-setup.sh and the
        machine-level skill copy render only into ~/.claude -- editing either
        used to mark every PER-REPO row stale, whereupon --apply re-rendered
        those repos to no effect and left the actual drift untouched."""
        before = self.fingerprint("repo")
        setup = self.engine / "memcontinuum-setup.sh"
        setup.write_text(setup.read_text() + "\n# touched\n")
        skill = self.engine / "skills" / "memcontinuum" / "SKILL.md"
        skill.write_text(skill.read_text() + "\nextra line\n")
        self.assertEqual(self.fingerprint("repo"), before)

    def test_a_machine_layer_input_does_move_the_machine_fingerprint(self):
        before = self.fingerprint("machine")
        setup = self.engine / "memcontinuum-setup.sh"
        setup.write_text(setup.read_text() + "\n# touched\n")
        self.assertNotEqual(self.fingerprint("machine"), before)

    def test_the_machine_level_skill_is_a_machine_input(self):
        before = self.fingerprint("machine")
        skill = self.engine / "skills" / "memcontinuum" / "SKILL.md"
        skill.write_text(skill.read_text() + "\nextra line\n")
        self.assertNotEqual(self.fingerprint("machine"), before)

    def test_a_template_does_not_move_the_machine_fingerprint(self):
        before = self.fingerprint("machine")
        tmpl = self.engine / "templates" / "write-hooks.json.tmpl"
        tmpl.write_text(tmpl.read_text() + "\n")
        self.assertEqual(self.fingerprint("machine"), before)

    def test_a_hook_script_is_not_a_render_input(self):
        """Hook scripts execute by absolute path, so a pull updates them live
        in every wired repo. Nothing needs re-rendering, and nothing may be
        marked stale."""
        before_repo = self.fingerprint("repo")
        before_machine = self.fingerprint("machine")
        for rel in ("hooks/memlib.sh", "hooks/memcontinuum-detect.sh"):
            f = self.engine / rel
            f.write_text(f.read_text() + "\n# touched\n")
        self.assertEqual(self.fingerprint("repo"), before_repo,
                         "editing a hook script must not change the fingerprint")
        self.assertEqual(self.fingerprint("machine"), before_machine,
                         "the detector script executes by path too -- pulling it is live")

    def test_a_template_is_a_render_input(self):
        before = self.fingerprint()
        tmpl = self.engine / "templates" / "write-hooks.json.tmpl"
        tmpl.write_text(tmpl.read_text().replace("MEMCONTINUUM_RENDERED", "MEMCONTINUUM_RENDERED2"))
        self.assertNotEqual(self.fingerprint(), before)

    def test_the_installer_and_the_merge_module_are_render_inputs(self):
        # mc_settings_merge.py is in BOTH scopes: it is what actually lands
        # the rendered blocks, per repo and into ~/.claude alike.
        for rel in ("scripts/repo-init.sh", "scripts/mc_settings_merge.py"):
            with self.subTest(rel=rel):
                engine2 = Path(self.tmp) / ("e-" + rel.replace("/", "_"))
                shutil.copytree(self.engine, engine2, symlinks=True)
                before = self.fingerprint("repo", engine2)
                f = engine2 / rel
                f.write_text(f.read_text() + "\n# touched\n")
                self.assertNotEqual(self.fingerprint("repo", engine2), before, rel)

    def test_a_copied_skill_is_a_render_input(self):
        before = self.fingerprint()
        skill = self.engine / "skills" / "memory-search" / "SKILL.md"
        skill.write_text(skill.read_text() + "\nextra line\n")
        self.assertNotEqual(self.fingerprint(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_walk_calls_a_scripts_only_change_ok_and_a_template_change_stale(self):
        home = str(Path(self.tmp) / "home")
        os.makedirs(home, exist_ok=True)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        store = str(Path(self.tmp) / "store")
        claude_dir = str(Path(repo) / ".claude")
        proc = run(self.engine / "scripts" / "repo-init.sh",
                   ["--project", "fp", "--store", store, "--claude-dir", claude_dir,
                    "--non-interactive"], home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(self.engine / "scripts" / "memcontinuum-decide.sh",
                   ["wired", "--repo", repo, "--store", store, "--project", "fp",
                    "--claude-dir", claude_dir], home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        def action():
            p = run(self.engine / "scripts" / "memcontinuum-update.sh", [], home)
            lines = [l for l in p.stdout.splitlines() if l.strip()]
            rows = [dict(zip(lines[0].split("\t"), l.split("\t"))) for l in lines[1:]]
            return rows[0]["action"], p.stdout

        self.assertEqual(action()[0], "ok")

        memlib = self.engine / "hooks" / "memlib.sh"
        memlib.write_text(memlib.read_text() + "\n# a scripts-only fix\n")
        act, out = action()
        self.assertEqual(act, "ok", "a scripts-only fix must leave every repo ok\n" + out)

        tmpl = self.engine / "templates" / "memcontinuum-rules.md"
        tmpl.write_text(tmpl.read_text() + "\nA new paragraph.\n")
        act, out = action()
        self.assertEqual(act, "stale", "a template change must flip the repo stale\n" + out)


class TestMachineLayerIsComparedSeparately(unittest.TestCase):
    """`--machine` compares the MACHINE fingerprint against what the machine
    layer was rendered with, so a setup.sh change shows up where it actually
    applies -- and nowhere else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-machine-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = Path(self.tmp) / "engine"
        shutil.copytree(TOOLS_DIR, self.engine, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", "fixtures", "tests", "__pycache__", ".venv"))
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)

    def _setup(self):
        proc = run(self.engine / "memcontinuum-setup.sh",
                   ["--python", VENV_PYTHON, "--no-model-warm"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def _machine_line(self):
        proc = run(self.engine / "scripts" / "memcontinuum-update.sh",
                   ["--machine"], self.home)
        for line in proc.stdout.splitlines():
            if line.startswith("machine:"):
                return line
        return None

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_machine_hook_line_carries_the_machine_fingerprint(self):
        self._setup()
        settings = json.loads(Path(self.home, ".claude", "settings.json").read_text())
        cmds = [h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
        detect = [c for c in cmds if "memcontinuum-detect.sh" in c]
        self.assertTrue(detect, cmds)
        self.assertIn("MEMCONTINUUM_RENDERED=%s " % engine_sha(self.engine, "machine"),
                      detect[0], detect[0])

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_machine_reads_ok_then_stale_after_a_setup_edit(self):
        self._setup()
        line = self._machine_line()
        self.assertIsNotNone(line)
        self.assertTrue(line.endswith("-- ok"), line)

        setup = self.engine / "memcontinuum-setup.sh"
        setup.write_text(setup.read_text() + "\n# a machine-layer change\n")
        line = self._machine_line()
        self.assertIsNotNone(line)
        self.assertTrue(line.endswith("-- stale"), line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_template_edit_flips_the_repos_not_the_machine(self):
        self._setup()
        repo = git_repo(str(Path(self.tmp) / "repo"))
        store = str(Path(self.tmp) / "store")
        claude_dir = str(Path(repo) / ".claude")
        proc = run(self.engine / "scripts" / "repo-init.sh",
                   ["--project", "m", "--store", store, "--claude-dir", claude_dir,
                    "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(self.engine / "scripts" / "memcontinuum-decide.sh",
                   ["wired", "--repo", repo, "--store", store, "--project", "m",
                    "--claude-dir", claude_dir], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        def row_action():
            p = run(self.engine / "scripts" / "memcontinuum-update.sh", [], self.home)
            lines = [l for l in p.stdout.splitlines() if l.strip() and "\t" in l]
            rows = [dict(zip(lines[0].split("\t"), l.split("\t"))) for l in lines[1:]]
            return rows[0]["action"], p.stdout

        self.assertEqual(row_action()[0], "ok")

        setup = self.engine / "memcontinuum-setup.sh"
        setup.write_text(setup.read_text() + "\n# a machine-layer change\n")
        act, out = row_action()
        self.assertEqual(act, "ok",
                         "a machine-layer change must not mark per-repo rows stale\n" + out)
        self.assertTrue(self._machine_line().endswith("-- stale"),
                        self._machine_line())

        tmpl = self.engine / "templates" / "memcontinuum-rules.md"
        tmpl.write_text(tmpl.read_text() + "\nA new paragraph.\n")
        act, out = row_action()
        self.assertEqual(act, "stale", out)


class TestTargetedModeRequiresRecordedWiring(unittest.TestCase):
    """`--add-lang`/`--never-ext` re-render the claude-dirs a row RECORDS.

    A legacy row records none, and `<repo>/.claude` is a guess -- one this
    command must not make: a project can have several claude-dirs and none of
    them has to be the one under the repo. And a dir that IS on record still
    has to carry this project's wiring before the installer is pointed at it;
    this command re-renders what is installed, it never installs.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-targeted-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = marked_store(str(Path(self.tmp) / "store"))

    def test_a_legacy_row_is_refused_and_no_claude_dir_is_substituted(self):
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=p code-roots=/x langs=python")
        before = decisions_tsv(self.home).read_text()
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("claude-dirs", combined, combined)
        # The refusal must hand over the migration command, not just complain.
        self.assertIn("--apply", combined, combined)
        self.assertIn("--claude-dir", combined, combined)
        self.assertFalse(Path(self.repo, ".claude").exists(),
                         "<repo>/.claude was substituted for a row that records none")
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_same_refusal_covers_never_ext(self):
        """Same refusal, and it has to be THIS refusal: the code-root here is
        real, so nothing else would stop the substituted dir being wired."""
        code_root = str(Path(self.tmp) / "code")
        os.makedirs(code_root, exist_ok=True)
        (Path(code_root) / "x.py").write_text("print(1)\n")
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=p code-roots={code_root} langs=python")
        proc = run(UPDATE_SH, ["--never-ext", ".h", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("claude-dirs", proc.stdout + proc.stderr)
        self.assertFalse(Path(self.repo, ".claude").exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_recorded_but_unwired_dir_is_refused_and_never_installed_into(self):
        code_root = str(Path(self.tmp) / "code")
        os.makedirs(code_root, exist_ok=True)
        (Path(code_root) / "x.py").write_text("print(1)\n")
        store = str(Path(self.tmp) / "real-store")
        claude_a = str(Path(self.repo) / ".claude")
        claude_b = str(Path(self.tmp) / "never-installed" / ".claude")
        os.makedirs(claude_b, exist_ok=True)
        proc = run(INSTALL_SH, ["--project", "p", "--store", store,
                                "--claude-dir", claude_a, "--code-root", code_root,
                                "--langs", "python", "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Hand-written: decide.sh would refuse to record claude_b at all, which
        # is exactly why the updater must re-check rather than trust the row.
        write_row(self.home, self.repo, "wired",
                  note=(f"store={store} project=p claude-dirs={claude_a};{claude_b} "
                        f"code-roots={code_root} langs=python"))
        before_row = decisions_tsv(self.home).read_text()
        before_a = Path(claude_a, "settings.local.json").read_text()

        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dir-not-wired", proc.stdout + proc.stderr)
        self.assertFalse(Path(claude_b, "settings.local.json").exists(),
                         "an unwired recorded dir must never be installed into")
        self.assertFalse(Path(claude_b, "rules").exists())
        self.assertEqual(Path(claude_a, "settings.local.json").read_text(), before_a,
                         "the refusal must land before any dir is re-rendered")
        self.assertEqual(decisions_tsv(self.home).read_text(), before_row)


class TestTargetedModeRefusesARowWithNoCodeRoot(unittest.TestCase):
    """repo-init ignores --langs/--never-ext when there is no --code-root to
    wire them into, so recording them for a code-root-less row would write a
    language set that renders nowhere. Refused before anything is written."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-nocr-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_add_lang_is_refused_with_the_row_and_the_wiring_untouched(self):
        proc = run(INSTALL_SH, ["--project", "p", "--store", self.store,
                                "--claude-dir", self.claude_dir,
                                "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(DECIDE_SH, ["wired", "--repo", self.repo, "--store", self.store,
                               "--project", "p", "--claude-dir", self.claude_dir],
                   self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        before_row = decisions_tsv(self.home).read_text()
        before_settings = Path(self.claude_dir, "settings.local.json").read_text()

        proc = run(UPDATE_SH, ["--add-lang", "python", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no-code-root", proc.stdout + proc.stderr)
        self.assertEqual(decisions_tsv(self.home).read_text(), before_row)
        self.assertEqual(Path(self.claude_dir, "settings.local.json").read_text(),
                         before_settings)


class TestMultiDirLegacyMigrationRecoversPerDir(unittest.TestCase):
    """One row is one project, and a project has ONE code-root/language set
    applied to all of its claude-dirs. So a legacy row's parameters are
    recovered from EVERY named dir, not from whichever was named first: two
    dirs that disagree mean the recovery has no single answer, and replaying
    the first dir's parameters onto the second would silently re-render it
    with a set it never had."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-multidir-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.code_a = str(Path(self.tmp) / "code-a")
        self.code_b = str(Path(self.tmp) / "code-b")
        for d, name in ((self.code_a, "x.py"), (self.code_b, "y.swift")):
            os.makedirs(d, exist_ok=True)
            Path(d, name).write_text("// x\n")
        self.claude_a = str(Path(self.repo) / ".claude")
        self.claude_b = str(Path(self.tmp) / "session-home" / ".claude")
        proc = run(INSTALL_SH, ["--project", "multi", "--store", self.store,
                                "--claude-dir", self.claude_a, "--code-root", self.code_a,
                                "--langs", "python", "--never-ext", ".cs",
                                "--non-interactive"], self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        proc = run(INSTALL_SH, ["--project", "multi", "--store", self.store,
                                "--claude-dir", self.claude_b, "--code-root", self.code_b,
                                "--langs", "swift", "--never-ext", ".h",
                                "--non-interactive"], self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=multi")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_disagreeing_dirs_are_refused_with_both_recoveries_printed(self):
        before = decisions_tsv(self.home).read_text()
        before_a = Path(self.claude_a, "settings.local.json").read_text()
        before_b = Path(self.claude_b, "settings.local.json").read_text()
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--claude-dir", self.claude_b], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertIn("migrate-dirs-disagree", combined, combined)
        # Both recoveries, so the human can see WHAT disagrees.
        self.assertIn(self.code_a, combined, combined)
        self.assertIn(self.code_b, combined, combined)
        self.assertIn("python", combined, combined)
        self.assertIn("swift", combined, combined)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)
        self.assertEqual(Path(self.claude_a, "settings.local.json").read_text(), before_a)
        self.assertEqual(Path(self.claude_b, "settings.local.json").read_text(), before_b)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_explicit_overrides_render_both_dirs_identically_and_record_them(self):
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--claude-dir", self.claude_b,
                               "--code-root", self.code_a, "--code-root", self.code_b,
                               "--langs", "python,swift",
                               "--set-never-ext", ".cs,.h"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn(f"claude-dirs={self.claude_a};{self.claude_b}", note, note)
        self.assertIn(f"code-roots={self.code_a};{self.code_b}", note, note)
        self.assertIn("langs=python;swift", note, note)
        self.assertIn("never=.cs;.h", note, note)
        for cd in (self.claude_a, self.claude_b):
            settings = Path(cd, "settings.local.json").read_text()
            self.assertIn("'*.py *.swift'", settings, cd)
            self.assertIn("'*.cs *.h'", settings, cd)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_code_root_override_needs_repo_like_its_siblings(self):
        """Accepted as a migration override -- and refused without --repo for
        the same reason its siblings are, not because it is an unknown flag."""
        proc = run(UPDATE_SH, ["--apply", "--code-root", self.code_a], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("unknown argument", combined, combined)
        self.assertIn("ONE registry row", combined, combined)


class TestStoreMissingOutranksNoWiring(UpdateTestBase):
    """A dir with no wiring AND a store that is gone is not a wiring problem
    to hand to the repair path -- it is a dead store, and saying `no-wiring`
    there sends a human to re-install against a store that is not there."""

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_gone_store_wins_over_absent_wiring_in_the_table(self):
        os.remove(Path(self.claude_dir, "settings.local.json"))
        os.rename(self.store, self.store + "-renamed-away")
        proc = run(UPDATE_SH, [], self.home)
        rows = self.table_rows(proc.stdout)
        self.assertEqual([r["action"] for r in rows], ["store-missing"], proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_apply_exits_nonzero_and_seeds_nothing(self):
        os.remove(Path(self.claude_dir, "settings.local.json"))
        os.rename(self.store, self.store + "-renamed-away")
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self.assertFalse(Path(self.store).exists())


class TestTargetedMultiDirIsAllOrNothing(unittest.TestCase):
    """As transactional as two installer runs can be made: every claude-dir is
    dry-run FIRST, and only an all-clear turns into real writes. Otherwise a
    row with two dirs re-renders the first with a new language set, fails on
    the second, and leaves the project half-converted with a registry row that
    describes neither half."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-update-txn-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.claude_a = str(Path(self.repo) / ".claude")
        self.claude_b = str(Path(self.tmp) / "session-home" / ".claude")
        for cd in (self.claude_a, self.claude_b):
            proc = run(INSTALL_SH, ["--project", "p", "--store", self.store,
                                    "--claude-dir", cd, "--code-root", self.code_root,
                                    "--langs", "python", "--never-ext", ".cs",
                                    "--non-interactive"], self.home)
            assert proc.returncode == 0, proc.stdout + proc.stderr
        proc = run(DECIDE_SH, ["wired", "--repo", self.repo, "--store", self.store,
                               "--project", "p", "--claude-dir", self.claude_a,
                               "--claude-dir", self.claude_b,
                               "--code-root", self.code_root, "--langs", "python",
                               "--never-ext", ".cs"], self.home)
        assert proc.returncode == 0, proc.stdout + proc.stderr

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_second_dir_that_cannot_render_stops_the_first_from_rendering(self):
        Path(self.claude_b, "rules", "memcontinuum.md").write_text("# not ours\n")
        before_a = Path(self.claude_a, "settings.local.json").read_text()
        before_row = decisions_tsv(self.home).read_text()

        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(Path(self.claude_a, "settings.local.json").read_text(), before_a,
                         "dir A was re-rendered even though dir B could not be")
        # Byte-equality above is the real assertion; this names what would
        # have changed. (MEMCONTINUUM_KNOWN_EXTS lists every extension this
        # engine knows and mentions swift either way -- the language set that
        # would have moved is MEMCONTINUUM_LANG_EXTS.)
        self.assertIn("MEMCONTINUUM_LANG_EXTS='*.py'",
                      Path(self.claude_a, "settings.local.json").read_text())
        self.assertEqual(decisions_tsv(self.home).read_text(), before_row)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_all_clear_case_still_renders_every_dir_and_records_the_row(self):
        proc = run(UPDATE_SH, ["--add-lang", "swift", "--repo", self.repo], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for cd in (self.claude_a, self.claude_b):
            self.assertIn("'*.py *.swift'", Path(cd, "settings.local.json").read_text(), cd)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("langs=python;swift", note, note)


class TestAnUnknownFingerprintNeverComparesEqual(unittest.TestCase):
    """`unknown` is not a fingerprint -- it is the absence of one (no sha256
    tool on the machine, an incomplete checkout, a render that predates
    stamping). Comparing two absences and calling them equal reports the repo
    as current and re-renders nothing, which is the one answer that cannot be
    checked. Unknown on either side means `stale`: re-render and find out."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-unknown-fp-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)

    def match(self, a, b):
        proc = subprocess.run(
            [MC_BASH, "-c",
             '. "$1"/scripts/mc-registry-lib.sh; '
             'if mc_fingerprint_match "$2" "$3"; then printf yes; else printf no; fi',
             "_", str(TOOLS_DIR), a, b],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.strip()

    def test_the_one_comparison_treats_unknown_as_a_mismatch(self):
        self.assertEqual(self.match("abc123abc123", "abc123abc123"), "yes")
        self.assertEqual(self.match("unknown", "unknown"), "no",
                         "two absences are not an agreement")
        self.assertEqual(self.match("unknown", "abc123abc123"), "no")
        self.assertEqual(self.match("abc123abc123", "unknown"), "no")
        self.assertEqual(self.match("", ""), "no")
        self.assertEqual(self.match("abc123abc123", "def456def456"), "no")

    def _crippled_engine(self, keep_repo_inputs=1):
        """A checkout mc_render_fingerprint cannot honestly fingerprint (fewer
        than three render inputs), but that update.sh can still run: the rules
        template it reads the identity marker from stays."""
        engine = Path(self.tmp) / "engine"
        shutil.copytree(TOOLS_DIR, engine, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", "fixtures", "tests", "__pycache__", ".venv"))
        for tmpl in (engine / "templates").iterdir():
            if tmpl.name != "memcontinuum-rules.md":
                tmpl.unlink()
        (engine / "scripts" / "mc_settings_merge.py").unlink()
        shutil.rmtree(engine / "skills" / "memory-search")
        shutil.rmtree(engine / "skills" / "memcontinuum")
        return engine

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_repo_row_reads_stale_when_both_sides_are_unknown(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        store = str(Path(self.tmp) / "store")
        claude_dir = str(Path(repo) / ".claude")
        proc = run(INSTALL_SH, ["--project", "u", "--store", store,
                                "--claude-dir", claude_dir, "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(DECIDE_SH, ["wired", "--repo", repo, "--store", store,
                               "--project", "u", "--claude-dir", claude_dir], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        # Both sides unknown: the rendered artifacts say so, and the engine
        # cannot compute one either.
        settings = Path(claude_dir, "settings.local.json")
        settings.write_text(settings.read_text().replace(
            "MEMCONTINUUM_RENDERED=%s " % engine_sha(), "MEMCONTINUUM_RENDERED=unknown "))
        rules = Path(claude_dir, "rules", "memcontinuum.md")
        lines = rules.read_text().splitlines(True)
        lines[1] = "<!-- memcontinuum-rendered: unknown -->\n"
        rules.write_text("".join(lines))

        engine = self._crippled_engine()
        proc = run(engine / "scripts" / "memcontinuum-update.sh", [], self.home)
        lines_out = [l for l in proc.stdout.splitlines() if l.strip()]
        header = lines_out[0].split("\t")
        row = dict(zip(header, lines_out[1].split("\t")))
        self.assertEqual(row["engine"], "unknown", proc.stdout)
        self.assertEqual(row["stamped"], "unknown", proc.stdout)
        self.assertEqual(row["rules"], "stale", proc.stdout)
        self.assertEqual(row["action"], "stale", proc.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_machine_layer_reads_stale_when_both_sides_are_unknown(self):
        engine = Path(self.tmp) / "engine"
        shutil.copytree(TOOLS_DIR, engine, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", "fixtures", "tests", "__pycache__", ".venv"))
        proc = run(engine / "memcontinuum-setup.sh",
                   ["--python", VENV_PYTHON, "--no-model-warm"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        settings = Path(self.home, ".claude", "settings.json")
        settings.write_text(settings.read_text().replace(
            "MEMCONTINUUM_RENDERED=%s " % engine_sha(engine, "machine"),
            "MEMCONTINUUM_RENDERED=unknown "))
        # Now cripple the MACHINE inputs so the engine side is unknown too.
        (engine / "scripts" / "mc_settings_merge.py").unlink()
        shutil.rmtree(engine / "skills" / "memcontinuum")

        proc = run(engine / "scripts" / "memcontinuum-update.sh", ["--machine"], self.home)
        line = [l for l in proc.stdout.splitlines() if l.startswith("machine:")]
        self.assertTrue(line, proc.stdout)
        self.assertIn("unknown", line[0], line[0])
        self.assertTrue(line[0].endswith("-- stale"), line[0])

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_state_sh_still_names_the_drift_when_both_sides_are_unknown(self):
        """The same comparison, in the hint a session actually sees. The
        engine is bootstrapped whole (state.sh needs a config.sh at all) and
        crippled afterwards, so config.sh's MEMCONTINUUM_ENGINE points at the
        checkout that can no longer fingerprint itself."""
        engine = Path(self.tmp) / "engine"
        shutil.copytree(TOOLS_DIR, engine, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", "fixtures", "tests", "__pycache__", ".venv"))
        proc = run(engine / "memcontinuum-setup.sh",
                   ["--python", VENV_PYTHON, "--no-model-warm"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        repo = git_repo(str(Path(self.tmp) / "repo"))
        store = str(Path(self.tmp) / "store")
        claude_dir = str(Path(repo) / ".claude")
        proc = run(engine / "scripts" / "repo-init.sh",
                   ["--project", "u", "--store", store,
                    "--claude-dir", claude_dir, "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        settings = Path(claude_dir, "settings.local.json")
        settings.write_text(settings.read_text().replace(
            "MEMCONTINUUM_RENDERED=%s " % engine_sha(engine),
            "MEMCONTINUUM_RENDERED=unknown "))

        for tmpl in (engine / "templates").iterdir():
            if tmpl.name != "memcontinuum-rules.md":
                tmpl.unlink()
        (engine / "scripts" / "mc_settings_merge.py").unlink()
        shutil.rmtree(engine / "skills" / "memory-search")
        self.assertEqual(engine_sha(engine), "unknown")

        proc = run(engine / "scripts" / "memcontinuum-state.sh", [repo], self.home,
                   cwd=repo)
        self.assertIn("update:", proc.stdout,
                      "an unverifiable stamp must be reported, not read as agreement:\n"
                      + proc.stdout + proc.stderr)


class TestEveryUnfinishedApplyRowFailsTheWalk(unittest.TestCase):
    """`--apply` exits 0 only when every claude-dir it walked ended up
    correct. A row it could not even resolve to a claude-dir is work left
    undone like any other -- reporting success over it is how a re-render tool
    tells you it fixed something it never looked at. `no-wiring` stays the one
    exception: that is the skill's repair path, not this command's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-exit-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = marked_store(str(Path(self.tmp) / "store"))
        # A row with no project= at all: unrecoverable, no claude-dir to walk.
        write_row(self.home, self.repo, "wired", note=f"store={self.store}")

    def test_apply_exits_nonzero_on_an_unrecoverable_row(self):
        proc = run(UPDATE_SH, ["--apply"], self.home)
        self.assertIn("unrecoverable", proc.stdout, proc.stdout)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_the_reporting_walk_still_exits_zero(self):
        proc = run(UPDATE_SH, [], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("unrecoverable", proc.stdout)


class TestMachineClaudeDirIsRecordedAndReused(unittest.TestCase):
    """The machine layer lives in whichever claude-dir setup installed it
    into, which need not be ~/.claude. Nothing recorded that, so `--machine`
    read ~/.claude, reported the real install as missing, and `--apply` would
    have rendered a SECOND machine layer at the default path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-machine-cd-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.engine = Path(self.tmp) / "engine"
        shutil.copytree(TOOLS_DIR, self.engine, symlinks=True,
                        ignore=shutil.ignore_patterns(
                            ".git", "fixtures", "tests", "__pycache__", ".venv"))
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.custom = str(Path(self.tmp) / "elsewhere" / "claude")

    def _setup(self):
        proc = run(self.engine / "memcontinuum-setup.sh",
                   ["--python", VENV_PYTHON, "--no-model-warm",
                    "--claude-dir", self.custom], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def _machine_line(self, args=()):
        proc = run(self.engine / "scripts" / "memcontinuum-update.sh",
                   ["--machine"] + list(args), self.home)
        for line in proc.stdout.splitlines():
            if line.startswith("machine:"):
                return line, proc
        return None, proc

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_setup_records_the_claude_dir_it_installed_into(self):
        self._setup()
        config = Path(self.home, ".memcontinuum", "config.sh").read_text()
        self.assertIn("MEMCONTINUUM_MACHINE_CLAUDE_DIR='%s'" % self.custom, config, config)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_machine_reports_the_recorded_dir_and_reads_ok(self):
        self._setup()
        line, proc = self._machine_line()
        self.assertIsNotNone(line, proc.stdout)
        self.assertIn(self.custom, line, line)
        self.assertTrue(line.endswith("-- ok"), line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_apply_refreshes_that_dir_in_place_and_renders_no_second_layer(self):
        self._setup()
        setup = self.engine / "memcontinuum-setup.sh"
        setup.write_text(setup.read_text() + "\n# a machine-layer change\n")
        line, _ = self._machine_line()
        self.assertTrue(line.endswith("-- stale"), line)

        proc = run(self.engine / "scripts" / "memcontinuum-update.sh",
                   ["--apply", "--machine"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(Path(self.home, ".claude", "settings.json").exists(),
                         "a second machine layer was rendered at the default path")
        self.assertIn("MEMCONTINUUM_RENDERED=%s " % engine_sha(self.engine, "machine"),
                      Path(self.custom, "settings.json").read_text())
        line, _ = self._machine_line()
        self.assertTrue(line.endswith("-- ok"), line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_default_dir_is_still_the_fallback_when_nothing_is_recorded(self):
        proc = run(self.engine / "memcontinuum-setup.sh",
                   ["--python", VENV_PYTHON, "--no-model-warm"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        config = Path(self.home, ".memcontinuum", "config.sh")
        config.write_text("\n".join(
            l for l in config.read_text().splitlines()
            if "MEMCONTINUUM_MACHINE_CLAUDE_DIR" not in l) + "\n")
        line, proc = self._machine_line()
        self.assertIsNotNone(line, proc.stdout)
        self.assertTrue(line.endswith("-- ok"), line)


class TestTheRowIsRewrittenOnlyWhenEveryDirRendered(unittest.TestCase):
    """"Record what is on disk" has to mean every dir, not every dir that
    happened to reach the installer. A skip is not a success: a dir refused
    for a foreign rules file or a dead store was never re-rendered, so a row
    rewritten after it describes parameters that dir does not carry -- and
    every future re-render replays that description."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-migrate-skip-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.claude_a = str(Path(self.repo) / ".claude")
        self.claude_b = str(Path(self.tmp) / "session-home" / ".claude")

    def _install(self, claude_dir):
        proc = run(INSTALL_SH, ["--project", "skip", "--store", self.store,
                                "--claude-dir", claude_dir, "--code-root", self.code_root,
                                "--langs", "python", "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_dead_store_skips_the_dir_and_leaves_the_row_alone(self):
        self._install(self.claude_a)
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=skip")
        os.rename(self.store, self.store + "-renamed-away")
        before = decisions_tsv(self.home).read_text()

        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--langs", "python"], self.home)
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("store-missing", combined, combined)
        self.assertNotIn("migrated:", combined, combined)
        self.assertEqual(decisions_tsv(self.home).read_text(), before,
                         "the row was rewritten over a dir that was never re-rendered")
        self.assertFalse(Path(self.store).exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_one_skipped_dir_among_two_leaves_the_row_alone(self):
        self._install(self.claude_a)
        self._install(self.claude_b)
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=skip")
        Path(self.claude_b, "rules", "memcontinuum.md").write_text("# not ours\n")
        before = decisions_tsv(self.home).read_text()

        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--claude-dir", self.claude_b], self.home)
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        # A really was re-rendered -- an installer run cannot be rolled back,
        # and re-rendering it is idempotent. What must NOT happen is the row
        # being rewritten as though B had been converted too.
        self.assertIn("OK %s" % self.claude_a, combined, combined)
        self.assertIn("SKIPPED %s" % self.claude_b, combined, combined)
        self.assertIn("rules-foreign", combined, combined)
        self.assertNotIn("migrated:", combined, combined)
        self.assertEqual(decisions_tsv(self.home).read_text(), before,
                         "one dir skipped, and the row was rewritten anyway")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_all_clear_case_still_migrates(self):
        self._install(self.claude_a)
        self._install(self.claude_b)
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=skip")
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--claude-dir", self.claude_b], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("migrated:", proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn(f"claude-dirs={self.claude_a};{self.claude_b}", note, note)


class TestThePartialRenderNoteIsOnlyPrintedWhenTrue(unittest.TestCase):
    """The note exists to stop a migration recording a smaller language set
    than what is wired without saying so. Printed when nothing is partial, it
    does the opposite job: it tells a human their fully-wired project is half
    installed, and names the language that is actually fine as the culprit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-partial-note-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.store = str(Path(self.tmp) / "store")
        self.claude_dir = str(Path(self.repo) / ".claude")

    def _legacy_walk(self):
        proc = run(INSTALL_SH, [
            "--project", "note", "--store", self.store, "--claude-dir", self.claude_dir,
            "--code-root", self.code_root, "--langs", "python", "--non-interactive",
        ], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_fully_rendered_language_produces_no_note(self):
        self._legacy_walk()
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=note")
        proc = run(UPDATE_SH, [], self.home)
        combined = proc.stdout + proc.stderr
        self.assertNotIn("partially rendered", combined, combined)
        # And the recovery itself is still right.
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_dir], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("langs=python", note, note)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_genuinely_partial_render_still_produces_one(self):
        """The other half: silencing the note by always printing nothing
        would pass the test above and lose what it is for."""
        self._legacy_walk()
        path = Path(self.claude_dir, "settings.local.json")
        path.write_text(path.read_text().replace(
            "MEMCONTINUUM_LANG_EXTS='*.py'", "MEMCONTINUUM_LANG_EXTS='*.py *.zz'"))
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=note")
        proc = run(UPDATE_SH, [], self.home)
        combined = proc.stdout + proc.stderr
        self.assertIn("partially rendered", combined, combined)
        self.assertIn("*.zz", combined, combined)


class TestFlagsTheModeDoesNotConsumeAreRefused(unittest.TestCase):
    """This command has four modes and they read the same option names
    differently. A flag the selected mode does not consume must be REFUSED,
    naming the mode and the flag -- never accepted and quietly dropped. A
    dropped flag is the worst outcome available here: the human typed what
    they wanted, the command reported success, and it did something else."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-matrix-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.claude_a = str(Path(self.repo) / ".claude")
        self.claude_b = str(Path(self.tmp) / "session-home" / ".claude")

    def _install(self, claude_dir):
        proc = run(INSTALL_SH, ["--project", "m", "--store", self.store,
                                "--claude-dir", claude_dir, "--code-root", self.code_root,
                                "--langs", "python", "--never-ext", ".cs",
                                "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def _current_format_row(self, dirs):
        for d in dirs:
            self._install(d)
        args = ["wired", "--repo", self.repo, "--store", self.store, "--project", "m"]
        for d in dirs:
            args += ["--claude-dir", d]
        args += ["--code-root", self.code_root, "--langs", "python", "--never-ext", ".cs"]
        proc = run(DECIDE_SH, args, self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def _refused(self, args, mode_word, flag):
        proc = run(UPDATE_SH, args, self.home)
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn(flag, combined, combined)
        self.assertIn(mode_word, combined.lower(), combined)
        return combined

    # --- walk mode: no --repo, so nothing that describes one row ------------

    def test_walk_mode_refuses_every_per_row_flag(self):
        for flag, value in (("--claude-dir", self.claude_a),
                            ("--code-root", self.code_root),
                            ("--langs", "python"),
                            ("--set-never-ext", ".cs")):
            with self.subTest(flag=flag):
                self._refused(["--apply", flag, value], "walk", flag)

    # --- targeted mode: --add-lang/--never-ext, which change one row's -----
    # --- language and never lists and nothing else ------------------------

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_targeted_mode_refuses_the_migration_and_dir_flags(self):
        self._current_format_row([self.claude_a])
        before = decisions_tsv(self.home).read_text()
        before_settings = Path(self.claude_a, "settings.local.json").read_text()
        for flag, value in (("--claude-dir", self.claude_a),
                            ("--code-root", self.code_root),
                            ("--langs", "python"),
                            ("--set-never-ext", ".cs")):
            with self.subTest(flag=flag):
                self._refused(["--add-lang", "swift", "--repo", self.repo, flag, value],
                              "targeted", flag)
        with self.subTest(flag="--machine"):
            self._refused(["--add-lang", "swift", "--repo", self.repo, "--machine"],
                          "targeted", "--machine")
        self.assertEqual(decisions_tsv(self.home).read_text(), before)
        self.assertEqual(Path(self.claude_a, "settings.local.json").read_text(),
                         before_settings)

    # --- a CURRENT-format row: its parameters are on record, so the ---------
    # --- migration overrides have nothing to supply ------------------------

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_recorded_row_refuses_the_migration_overrides(self):
        self._current_format_row([self.claude_a])
        before = decisions_tsv(self.home).read_text()
        for flag, value in (("--code-root", self.code_root),
                            ("--langs", "python"),
                            ("--set-never-ext", ".cs")):
            with self.subTest(flag=flag):
                combined = self._refused(
                    ["--apply", "--repo", self.repo, flag, value], "record", flag)
                self.assertNotIn("applying:", combined, combined)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_claude_dir_the_row_does_not_record_is_refused(self):
        self._current_format_row([self.claude_a])
        stranger = str(Path(self.tmp) / "stranger" / ".claude")
        os.makedirs(stranger, exist_ok=True)
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", stranger], self.home)
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("dir-not-recorded", combined, combined)
        self.assertFalse(Path(stranger, "settings.local.json").exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_claude_dir_narrows_a_recorded_row_to_those_dirs_only(self):
        """The subset walk: on a row that records its dirs, --claude-dir means
        "act on these and leave the rest alone"."""
        self._current_format_row([self.claude_a, self.claude_b])
        for cd in (self.claude_a, self.claude_b):
            path = Path(cd, "settings.local.json")
            path.write_text(path.read_text().replace(
                "MEMCONTINUUM_RENDERED=%s " % engine_sha(),
                "MEMCONTINUUM_RENDERED=deadbee "))
        before_b = Path(self.claude_b, "settings.local.json").read_text()

        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Tab-separated lines only: --apply also prints its own "applying:"
        # and "OK" progress lines onto stdout, between the table rows.
        lines = [l for l in proc.stdout.splitlines() if "\t" in l]
        header = lines[0].split("\t")
        self.assertEqual(header[1], "claude-dir", proc.stdout)
        rows = [dict(zip(header, l.split("\t"))) for l in lines[1:]]
        self.assertEqual([r["claude-dir"] for r in rows], [self.claude_a], proc.stdout)
        self.assertIn("MEMCONTINUUM_RENDERED=%s " % engine_sha(),
                      Path(self.claude_a, "settings.local.json").read_text())
        self.assertEqual(Path(self.claude_b, "settings.local.json").read_text(), before_b,
                         "a dir the command was not asked to touch was re-rendered")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_a_legacy_row_still_takes_all_four(self):
        """The green half of the matrix: on a legacy row these flags are the
        migration's whole point, and must keep working."""
        self._install(self.claude_a)
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=m")
        proc = run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--code-root", self.code_root,
                               "--langs", "python", "--set-never-ext", ".cs"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("langs=python", note, note)
        self.assertIn("never=.cs", note, note)


class TestDisagreementComparesTheRawRenderedValues(unittest.TestCase):
    """What a re-render replays is the rendered VALUE, not the tidy name it
    normalizes to. Two dirs whose extension globs differ but map to the same
    language list are still rendering different things, and picking one to
    replay onto the other silently changes what the other indexes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-rawset-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))
        self.store = str(Path(self.tmp) / "store")
        self.code_root = str(Path(self.tmp) / "code")
        os.makedirs(self.code_root, exist_ok=True)
        (Path(self.code_root) / "x.py").write_text("print(1)\n")
        self.claude_a = str(Path(self.repo) / ".claude")
        self.claude_b = str(Path(self.tmp) / "session-home" / ".claude")
        for cd in (self.claude_a, self.claude_b):
            proc = run(INSTALL_SH, ["--project", "raw", "--store", self.store,
                                    "--claude-dir", cd, "--code-root", self.code_root,
                                    "--langs", "python", "--non-interactive"], self.home)
            assert proc.returncode == 0, proc.stdout + proc.stderr
        # B carries an extra glob this engine knows no language for. Both dirs
        # still NORMALIZE to exactly "python".
        path = Path(self.claude_b, "settings.local.json")
        path.write_text(path.read_text().replace(
            "MEMCONTINUUM_LANG_EXTS='*.py'", "MEMCONTINUUM_LANG_EXTS='*.py *.zz'"))
        write_row(self.home, self.repo, "wired",
                  note=f"store={self.store} project=raw")

    def _apply(self, extra=()):
        return run(UPDATE_SH, ["--apply", "--repo", self.repo,
                               "--claude-dir", self.claude_a,
                               "--claude-dir", self.claude_b] + list(extra), self.home)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_globs_that_normalize_to_the_same_languages_still_disagree(self):
        before = decisions_tsv(self.home).read_text()
        before_a = Path(self.claude_a, "settings.local.json").read_text()
        before_b = Path(self.claude_b, "settings.local.json").read_text()
        proc = self._apply()
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("migrate-dirs-disagree", combined, combined)
        self.assertIn("*.py *.zz", combined,
                      "the raw recovered value must be shown, not the "
                      "language name it normalizes to:\n" + combined)
        self.assertEqual(decisions_tsv(self.home).read_text(), before)
        self.assertEqual(Path(self.claude_a, "settings.local.json").read_text(), before_a)
        self.assertEqual(Path(self.claude_b, "settings.local.json").read_text(), before_b)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_the_partial_note_is_attributed_to_the_dir_it_came_from(self):
        """Not the first dir's note printed for the row: A is fully rendered
        and B is not, so a first-dir-only note says nothing at all here."""
        proc = self._apply()
        combined = proc.stdout + proc.stderr
        zz_lines = [l for l in combined.splitlines() if "*.zz" in l and "partially" in l]
        self.assertTrue(zz_lines, "no partial-render note for the dir that has one:\n"
                        + combined)
        for line in zz_lines:
            self.assertIn(self.claude_b, line, line)
            self.assertNotIn(self.claude_a, line,
                             "the note was attributed to the wrong dir: " + line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_explicit_langs_resolves_it_and_both_dirs_render_identically(self):
        proc = self._apply(["--langs", "python"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        self.assertIn("langs=python", note, note)
        for cd in (self.claude_a, self.claude_b):
            settings = Path(cd, "settings.local.json").read_text()
            self.assertIn("MEMCONTINUUM_LANG_EXTS='*.py'", settings, cd)
            self.assertNotIn("*.zz", settings, cd)


class TestRepoWithoutAWiredRowIsRefused(unittest.TestCase):
    """`--repo` names the row to act on. When there is no wired row for it,
    the walk matched nothing and printed an empty table at exit 0 -- which
    reads as "checked, all current" for a repository this command never had
    anything to say about. It also left the per-row flags in limbo: the half
    of the flag matrix that only a row can settle never ran, so
    `--repo UNDECIDED --langs python` was neither consumed nor refused."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-norow-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)
        self.repo = git_repo(str(Path(self.tmp) / "repo"))

    def _wire_an_unrelated_row(self):
        """So the registry FILE exists and holds rows -- the miss has to come
        from this key, not from an empty machine."""
        other = git_repo(str(Path(self.tmp) / "other"))
        write_row(self.home, other, "wired", note="store=/nowhere project=other")

    def _refused(self, args, decision):
        before = (decisions_tsv(self.home).read_text()
                  if decisions_tsv(self.home).exists() else None)
        proc = run(UPDATE_SH, args, self.home)
        combined = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, combined)
        self.assertIn("no-wired-row", combined, combined)
        self.assertIn("decision=%s" % decision, combined, combined)
        # No table: an empty one is exactly the "nothing to report" answer
        # this refusal replaces.
        self.assertNotIn("claude-dir\tstamped", combined, combined)
        after = (decisions_tsv(self.home).read_text()
                 if decisions_tsv(self.home).exists() else None)
        self.assertEqual(after, before, "the registry was touched")
        self.assertFalse(Path(self.repo, ".claude").exists(),
                         "nothing may be wired for a repo with no wired row")
        return combined

    def test_an_undecided_repo_is_refused(self):
        self._wire_an_unrelated_row()
        self._refused(["--dry-run", "--repo", self.repo], "none")

    def test_a_declined_repo_is_refused_and_names_the_recorded_answer(self):
        self._wire_an_unrelated_row()
        write_row(self.home, self.repo, "declined")
        combined = self._refused(["--apply", "--repo", self.repo], "declined")
        self.assertIn(self.repo, combined, combined)

    def test_a_machine_with_no_registry_at_all_is_refused_too(self):
        self._refused(["--dry-run", "--repo", self.repo], "none")

    def test_per_row_flags_alongside_it_are_refused_rather_than_ignored(self):
        """The point of the ruling: with no row, the row half of the flag
        matrix never runs, so these used to be silently dropped."""
        self._wire_an_unrelated_row()
        for flag, value in (("--langs", "python"),
                            ("--code-root", self.tmp),
                            ("--set-never-ext", ".cs"),
                            ("--claude-dir", str(Path(self.repo) / ".claude"))):
            with self.subTest(flag=flag):
                self._refused(["--apply", "--repo", self.repo, flag, value], "none")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_targeted_mode_refuses_with_the_same_answer(self):
        self._wire_an_unrelated_row()
        write_row(self.home, self.repo, "declined")
        combined = self._refused(["--add-lang", "swift", "--repo", self.repo], "declined")
        # The old wording stays reachable -- it is what the message means.
        self.assertIn("no wired row", combined, combined)


class TestHelp(unittest.TestCase):
    def test_help_exits_zero_and_documents_the_flags(self):
        proc = subprocess.run([MC_BASH, str(UPDATE_SH), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for token in ("--dry-run", "--apply", "--machine", "--add-lang", "--never-ext", "--repo"):
            self.assertIn(token, proc.stdout, token)

    def test_bash_n(self):
        proc = subprocess.run([MC_BASH, "-n", str(UPDATE_SH)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
