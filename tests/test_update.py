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
import subprocess
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
DECIDE_SH = TOOLS_DIR / "scripts" / "memcontinuum-decide.sh"
UPDATE_SH = TOOLS_DIR / "scripts" / "memcontinuum-update.sh"
INSTALL_SH = TOOLS_DIR / "scripts" / "repo-init.sh"
SETUP_SH = TOOLS_DIR / "memcontinuum-setup.sh"

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
        ["bash", str(script)] + args,
        input=stdin, capture_output=True, text=True,
        env=clean_env(home), timeout=timeout, cwd=cwd or home,
    )


def git_repo(path):
    Path(path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "."], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                     "commit", "-q", "--allow-empty", "-m", "init"], cwd=path, check=True)
    return path


def engine_sha():
    out = subprocess.run(
        ["git", "-C", str(TOOLS_DIR), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return out or "unknown"


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

        proc2 = run(UPDATE_SH, ["--apply"], self.home)
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
        return run(UPDATE_SH, ["--apply"], self.home, cwd=cwd)

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


class TestHelp(unittest.TestCase):
    def test_help_exits_zero_and_documents_the_flags(self):
        proc = subprocess.run(["bash", str(UPDATE_SH), "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for token in ("--dry-run", "--apply", "--machine", "--add-lang", "--never-ext", "--repo"):
            self.assertIn(token, proc.stdout, token)

    def test_bash_n(self):
        proc = subprocess.run(["bash", "-n", str(UPDATE_SH)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
