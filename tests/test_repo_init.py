"""Tests for scripts/repo-init.sh -- the new-project installer.

These exercise the real script via subprocess (matching the convention in
tests/test_hooks.py), with HOME sandboxed to a fresh temp dir per test so
$HOME/.memcontinuum (the default MEMCONTINUUM_HOME) and settings.local.json
never touch the real machine's state.
"""
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
INSTALL_SH = TOOLS_DIR / "scripts" / "repo-init.sh"
PY_CORPUS = TOOLS_DIR / "tests" / "fixtures" / "python_corpus"
# This machine's venv python is never hardcoded in tracked test code -- set
# $MEMCONTINUUM_PYTHON in your own (untracked) shell environment before
# running this file; see README.md "Requirements" / "Running the tests".
VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
_SKIP_NO_VENV = (
    "set $MEMCONTINUUM_PYTHON to a venv python with fastembed/PyYAML "
    "installed to run these tests (see README.md)"
)

OUR_SCRIPTS = [
    "pre-edit-chain.sh",
    "newfile-nudge.sh",
    "ledger-post-edit.sh",
    "precompact-persist.sh",
    "sessionstart-remind.sh",
    "userprompt-remind.sh",
    "sessionend-stamp.sh",
]


def sandbox_home():
    return tempfile.mkdtemp(prefix="memcontinuum-install-test-home-")


def run_install(args, home, timeout=60, python=VENV_PYTHON, extra_env=None, cwd=None):
    """Runs the real scripts/repo-init.sh via subprocess with a sandboxed HOME.

    `python` sets MEMCONTINUUM_PYTHON to this machine's venv by default, so
    every test in this file exercises scripts/repo-init.sh's own logic rather than its
    python-resolution fallback chain (that resolution order has its own
    dedicated tests below -- TestPythonResolutionOrder). Pass python=None to
    leave MEMCONTINUUM_PYTHON unset (and rely on --python / .venv / the
    error path instead).
    """
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MEMCONTINUUM_"):
            del env[k]
    env["HOME"] = home
    if python is not None:
        env["MEMCONTINUUM_PYTHON"] = python
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(INSTALL_SH)] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        # Never the test process's own cwd: that is the REAL engine checkout,
        # a git repo -- any cwd-derived default in the script under test
        # would resolve against it and write into the real .claude (this
        # actually happened; the engine's own wiring had to be restored from
        # a session transcript). The sandbox HOME is the neutral floor.
        cwd=cwd or home,
    )
    return proc


def run_install_at(install_sh, args, home, path_prepend=None, timeout=60, python=None, extra_env=None, stdin=None):
    """Like run_install, but against an arbitrary scripts/repo-init.sh path (a copied
    engine checkout -- see copy_engine below) and with PATH control, for the
    --bootstrap-venv / python-resolution-order tests that need to run a copy
    without $MEMCONTINUUM_PYTHON and without the real repo's absence of a
    checked-in .venv/ leaking in either direction.

    `stdin` defaults to None (inherited from the test process, matching
    subprocess.run's own default) -- pass subprocess.DEVNULL explicitly for a
    test that must prove the "not a tty" guard fails cleanly rather than
    hanging (Task 10): inheriting a real terminal's stdin here would make
    that one test flaky depending on how the suite itself was invoked.
    """
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("MEMCONTINUUM_"):
            del env[k]
    env["HOME"] = home
    if python is not None:
        env["MEMCONTINUUM_PYTHON"] = python
    if path_prepend:
        env["PATH"] = os.pathsep.join(list(path_prepend) + [env.get("PATH", "")])
    if extra_env:
        env.update(extra_env)
    proc = subprocess.run(
        ["bash", str(install_sh)] + args,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        stdin=stdin,
        # Same pin as run_install: never the real engine checkout (INC-0102).
        cwd=home,
    )
    return proc


def make_python_and_cs_corpus(root):
    """A code root with one real .py file (from the shared python_corpus
    fixture) and one .cs file with no chunker in this engine version --
    Task 10 brief Step 1's corpus: a supported+proposed language (python)
    alongside an unsupported extension the census table must still name."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(PY_CORPUS / "basic_functions.py", root / "basic_functions.py")
    (root / "Program.cs").write_text("class Program {}\n")
    return root


def copy_engine(dst):
    """Copies just the engine files scripts/repo-init.sh needs (not memory/, .claude/,
    fixtures/, tests/) into dst, so a test can plant its own .venv/ next to
    the copy, or run with PATH manipulated, without ever touching the real
    checkout this test suite itself lives in."""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "scripts").mkdir(exist_ok=True)
    shutil.copy(INSTALL_SH, dst / "scripts" / "repo-init.sh")
    (dst / "scripts" / "repo-init.sh").chmod(0o755)
    # fix-round-4 F8: repo-init.sh's heredoc imports this sibling module by
    # SCRIPT_DIR at runtime -- a copied checkout without it fails with
    # ModuleNotFoundError, not a graceful skip.
    shutil.copy(TOOLS_DIR / "scripts" / "mc_settings_merge.py", dst / "scripts" / "mc_settings_merge.py")
    for name in ("memidx.py", "memlint.py", "requirements.txt"):
        shutil.copy(TOOLS_DIR / name, dst / name)
    for name in ("hooks", "templates", "skills"):
        shutil.copytree(TOOLS_DIR / name, dst / name)
    # Anatomy M1 Task 2: memidx.py now imports chunkers.swift at module load
    # (not lazily), so a copied checkout without the chunkers/ package next
    # to it fails with ModuleNotFoundError, not a graceful skip -- same
    # reasoning as the mc_settings_merge.py copy above.
    shutil.copytree(TOOLS_DIR / "chunkers", dst / "chunkers",
                     ignore=shutil.ignore_patterns("__pycache__"))
    return dst / "scripts" / "repo-init.sh"


def write_python_shim(path, target=VENV_PYTHON):
    """Writes an executable at `path` that just execs `target` -- a stand-in
    venv python good enough for scripts/repo-init.sh's own `-c 'pass'` sanity check
    and its real reindex/lint calls."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/usr/bin/env bash\nexec "%s" "$@"\n' % target)
    path.chmod(0o755)


class TestBashSyntax(unittest.TestCase):
    def test_bash_n(self):
        proc = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestFreshInstall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = sandbox_home()
        cls.store = str(Path(cls.home) / "store")
        cls.code_root = str(Path(cls.home) / "code")
        os.makedirs(cls.code_root, exist_ok=True)
        cls.claude_dir = Path(cls.home) / ".claude"
        cls.proc = run_install(
            ["--project", "widgetco", "--store", cls.store, "--code-root", cls.code_root,
             "--claude-dir", str(cls.claude_dir)],
            cls.home,
        )
        cls.settings_path = cls.claude_dir / "settings.local.json"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_exits_zero(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout + self.proc.stderr)

    def test_store_tree_created(self):
        for d in ["topics", "incidents", "investigations", "concepts", "sources",
                  "inbox/codex", "inbox/grok", "inbox/audit"]:
            self.assertTrue((Path(self.store) / d).is_dir(), d)

    def test_store_readme_and_gitignore_written(self):
        readme = (Path(self.store) / "README.md").read_text()
        self.assertIn("widgetco", readme)
        gitignore = (Path(self.store) / ".gitignore").read_text()
        self.assertIn("*.sqlite", gitignore)

    def test_store_git_initialized_with_one_commit(self):
        self.assertTrue((Path(self.store) / ".git").is_dir())
        log = subprocess.run(
            ["git", "-C", self.store, "log", "--oneline"],
            capture_output=True, text=True,
        )
        commits = [l for l in log.stdout.strip().splitlines() if l]
        self.assertEqual(len(commits), 1, log.stdout)

    def test_post_commit_symlink_present(self):
        post_commit = Path(self.store) / ".git" / "hooks" / "post-commit"
        self.assertTrue(post_commit.is_file(), "post-commit hook missing")
        st = post_commit.stat()
        self.assertTrue(st.st_mode & stat.S_IXUSR, "post-commit hook not executable")
        text = post_commit.read_text()
        self.assertIn("post-commit-reindex.sh", text)
        self.assertIn("MEMCONTINUUM_ROOT", text)
        self.assertIn("MEMCONTINUUM_PROJECT", text)

    def test_skill_copied(self):
        dst = self.claude_dir / "skills" / "memory-search" / "SKILL.md"
        src = TOOLS_DIR / "skills" / "memory-search" / "SKILL.md"
        self.assertTrue(dst.is_file())
        self.assertEqual(dst.read_text(), src.read_text())

    def test_settings_is_valid_json(self):
        data = json.loads(self.settings_path.read_text())
        self.assertIsInstance(data, dict)

    def test_settings_contains_all_five_write_hooks(self):
        data = json.loads(self.settings_path.read_text())
        hooks = data["hooks"]
        for event, script in [
            ("PostToolUse", "ledger-post-edit.sh"),
            ("PreCompact", "precompact-persist.sh"),
            ("SessionStart", "sessionstart-remind.sh"),
            ("UserPromptSubmit", "userprompt-remind.sh"),
            ("SessionEnd", "sessionend-stamp.sh"),
        ]:
            self.assertIn(event, hooks, event)
            commands = [
                h.get("command", "")
                for group in hooks[event]
                for h in group.get("hooks", [])
            ]
            self.assertTrue(any(script in c for c in commands), (event, commands))
            matching = [c for c in commands if script in c]
            self.assertIn(f"MEMCONTINUUM_ROOT={self.store}", matching[0])
            self.assertIn("MEMCONTINUUM_PROJECT=widgetco", matching[0])
            self.assertIn(f"MEMCONTINUUM_CODE_ROOT={self.code_root}", matching[0])
            self.assertIn(str(TOOLS_DIR / "hooks" / script), matching[0])

    def test_settings_contains_pre_edit_hook_with_right_paths(self):
        data = json.loads(self.settings_path.read_text())
        pre = data["hooks"]["PreToolUse"]
        commands = [
            (h.get("if", ""), h.get("command", ""))
            for group in pre
            for h in group.get("hooks", [])
            if "pre-edit-chain.sh" in h.get("command", "")
        ]
        edit = [c for i, c in commands if i.startswith("Edit(")]
        write = [c for i, c in commands if i.startswith("Write(")]
        self.assertEqual(len(edit), 1)
        self.assertEqual(len(write), 1)
        for cmd in (edit[0], write[0]):
            self.assertIn("pre-edit-chain.sh", cmd)
            self.assertIn(f"MEMCONTINUUM_ROOT={self.store}", cmd)
            self.assertIn("MEMCONTINUUM_PROJECT=widgetco", cmd)
            self.assertIn(f"MEMCONTINUUM_STRIP_PREFIX={self.code_root}/", cmd)
        ifs = [i for i, _ in commands]
        self.assertTrue(any(self.code_root in i for i in ifs), ifs)
        # `if` filter paths use Claude Code's permission-rule syntax, where a
        # single leading slash anchors at the settings source, not the
        # filesystem root -- a rendered `Edit(/abs/root/**)` (one leading
        # slash) matches nothing at all. `self.code_root` is already
        # absolute, so the correct rendering needs a SECOND leading slash:
        # `Edit(//abs/root/**)`. Fix-round 2026-08-31: this was the actual
        # bug (confirmed empirically: a live Edit to a governed file produced
        # a PostToolUse ledger line and NO PreToolUse line).
        self.assertEqual(ifs, [f"Edit(/{self.code_root}/**)", f"Write(/{self.code_root}/**)"])

    def test_settings_contains_newfile_nudge_hook_write_only_with_right_paths(self):
        """Finding 8: newfile-nudge.sh gets its OWN "Write" (never
        "Edit|Write") matcher group, a separate group from pre-edit-chain's,
        one `if` per --code-root, and never carries MEMCONTINUUM_ROOT/
        PROJECT/STRIP_PREFIX (it never calls memidx.py)."""
        data = json.loads(self.settings_path.read_text())
        pre = data["hooks"]["PreToolUse"]
        nudge_groups = [g for g in pre if g.get("matcher") == "Write"]
        self.assertEqual(len(nudge_groups), 1, pre)
        nudge_items = [
            h for h in nudge_groups[0].get("hooks", [])
            if "newfile-nudge.sh" in h.get("command", "")
        ]
        self.assertEqual(len(nudge_items), 1, nudge_items)
        item = nudge_items[0]
        # Same double-leading-slash requirement as the pre-edit filter pair
        # (fix-round 2026-08-31, see test_settings_contains_pre_edit_hook_with_right_paths).
        self.assertEqual(item["if"], f"Write(/{self.code_root}/**)")
        self.assertIn(f"MEMCONTINUUM_CODE_ROOT={self.code_root}", item["command"])
        self.assertIn("MEMCONTINUUM_PYTHON=", item["command"])
        self.assertNotIn("MEMCONTINUUM_ROOT=", item["command"])
        # F1 fix: DOES carry the project identity marker now (the hook itself
        # never reads it -- this is so a re-run can tell this project's nudge
        # entry apart from a different project's sharing the same claude-dir;
        # see TestTwoProjectsOneClaudeDir below for the regression it closes).
        self.assertIn("MEMCONTINUUM_PROJECT=widgetco", item["command"])
        self.assertNotIn("MEMCONTINUUM_STRIP_PREFIX=", item["command"])
        # pre-edit-chain's own group must be untouched by this addition.
        edit_write_groups = [g for g in pre if g.get("matcher") == "Edit|Write"]
        self.assertEqual(len(edit_write_groups), 1, pre)

    def test_every_rendered_if_filter_has_double_leading_slash(self):
        """Fix-round 2026-08-31, the dead-PreToolUse-hook bug: Claude Code
        hook `if` filters use permission-rule path syntax, where a single
        leading slash anchors at the settings SOURCE, not the filesystem
        root (docs: code.claude.com/docs/en/permissions.md -- "Use
        //Users/alice/file for absolute paths"). `--code-root` is always
        rendered absolute (already starting with `/`), so every `if` value
        this installer writes must carry exactly two leading slashes -- one
        short, and the pattern matches nothing, ever (empirically confirmed:
        a live Edit to a governed file produced a PostToolUse ledger line
        and NO PreToolUse line, same settings file). Sweeps every `if` in
        the merged settings, not just the pre-edit/newfile-nudge pairs this
        file already pins individually above, so a future filter-pair
        template gains this coverage for free."""
        data = json.loads(self.settings_path.read_text())
        ifs = [
            h["if"]
            for event_groups in data["hooks"].values()
            for group in event_groups
            for h in group.get("hooks", [])
            if "if" in h
        ]
        self.assertTrue(ifs, "expected at least one `if`-filtered hook entry")
        pattern = re.compile(r"^(Edit|Write)\(//.+/\*\*\)$")
        for i in ifs:
            self.assertRegex(i, pattern, i)
            self.assertTrue(i.startswith(("Edit(//", "Write(//")), i)
            self.assertTrue(i.endswith("/**)"), i)

    def test_reindex_and_lint_clean_on_empty_store(self):
        out = self.proc.stdout
        self.assertIn("Reindex        : rc=0", out)
        self.assertIn("Lint           : rc=0", out)

    def test_db_created_under_home_memcontinuum(self):
        db = Path(self.home) / ".memcontinuum" / "widgetco.sqlite"
        self.assertTrue(db.is_file())


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestReinstallIdempotent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = sandbox_home()
        cls.store = str(Path(cls.home) / "store")
        cls.code_root = str(Path(cls.home) / "code")
        os.makedirs(cls.code_root, exist_ok=True)
        os.makedirs(Path(cls.home) / ".claude", exist_ok=True)
        cls.settings_path = Path(cls.home) / ".claude" / "settings.local.json"
        # seed a foreign hook + a foreign top-level key before first install
        cls.settings_path.write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit",
                        "hooks": [{"type": "command", "command": "echo foreign-hook"}],
                    }
                ]
            },
        }))

        cls.proc1 = run_install(
            ["--project", "widgetco", "--store", cls.store, "--code-root", cls.code_root,
             "--claude-dir", str(Path(cls.home) / ".claude")],
            cls.home,
        )
        cls.settings_after_1 = cls.settings_path.read_text()
        cls.proc2 = run_install(
            ["--project", "widgetco", "--store", cls.store, "--code-root", cls.code_root,
             "--claude-dir", str(Path(cls.home) / ".claude")],
            cls.home,
        )
        cls.settings_after_2 = cls.settings_path.read_text()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_both_runs_succeed(self):
        self.assertEqual(self.proc1.returncode, 0, self.proc1.stdout + self.proc1.stderr)
        self.assertEqual(self.proc2.returncode, 0, self.proc2.stdout + self.proc2.stderr)

    def test_no_duplicate_entries(self):
        data = json.loads(self.settings_after_2)
        for event in ["PostToolUse", "PreCompact", "SessionStart", "UserPromptSubmit", "SessionEnd"]:
            our_items = [
                h for group in data["hooks"][event] for h in group.get("hooks", [])
                if any(s in h.get("command", "") for s in OUR_SCRIPTS)
            ]
            self.assertEqual(len(our_items), 1, (event, our_items))
        pre_items = [
            h for group in data["hooks"]["PreToolUse"] for h in group.get("hooks", [])
        ]
        # pre-edit-chain: one Edit, one Write; newfile-nudge: one Write.
        self.assertEqual(len(pre_items), 3, pre_items)

    def test_settings_stable_across_reruns(self):
        self.assertEqual(
            json.loads(self.settings_after_1), json.loads(self.settings_after_2)
        )

    def test_foreign_hook_preserved(self):
        data = json.loads(self.settings_after_2)
        commands = [
            h.get("command", "")
            for group in data["hooks"]["PostToolUse"]
            for h in group.get("hooks", [])
        ]
        self.assertIn("echo foreign-hook", commands)

    def test_foreign_top_level_key_preserved(self):
        data = json.loads(self.settings_after_2)
        self.assertEqual(data.get("permissions"), {"allow": ["Bash(ls:*)"]})

    def test_backup_file_written(self):
        backup = Path(str(self.settings_path) + ".bak-memcontinuum")
        self.assertTrue(backup.is_file())


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestDryRun(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = str(Path(home) / "code")
            os.makedirs(code_root, exist_ok=True)
            proc = run_install(
                ["--project", "ghost", "--store", store, "--code-root", code_root,
                 "--claude-dir", str(Path(home) / ".claude"), "--dry-run"],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(Path(store).exists(), "store dir should not exist after --dry-run")
            self.assertFalse((Path(home) / ".claude").exists(), "claude-dir should not exist after --dry-run")
            mc_home = Path(home) / ".memcontinuum"
            if mc_home.exists():
                self.assertEqual(list(mc_home.glob("*.sqlite")), [])
            self.assertIn("dry-run", proc.stdout.lower())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_dry_run_then_real_run_still_works(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            claude_dir = str(Path(home) / ".claude")
            run_install(["--project", "p", "--store", store, "--claude-dir", claude_dir, "--dry-run"], home)
            proc = run_install(["--project", "p", "--store", store, "--claude-dir", claude_dir], home)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue(Path(store).is_dir())
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestNoCodeRoot(unittest.TestCase):
    def test_no_pretooluse_block_when_no_code_root(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            proc = run_install(
                ["--project", "rationale-only", "--store", store,
                 "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads((Path(home) / ".claude" / "settings.local.json").read_text())
            self.assertNotIn("PreToolUse", data.get("hooks", {}))
            # write hooks still present, without a MEMCONTINUUM_CODE_ROOT var
            cmd = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
            self.assertNotIn("MEMCONTINUUM_CODE_ROOT", cmd)
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestIdempotentDropToZeroCodeRoots(unittest.TestCase):
    def test_rerun_without_code_root_removes_stale_pretooluse_entries(self):
        """Re-running with FEWER --code-roots than the previous run (down to
        zero) must replace last run's config, not merely add to it -- an
        idempotency hole a same-config re-run can't catch."""
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = str(Path(home) / "code")
            os.makedirs(code_root, exist_ok=True)
            claude_dir = str(Path(home) / ".claude")
            proc1 = run_install(
                ["--project", "p", "--store", store, "--code-root", code_root,
                 "--claude-dir", claude_dir], home
            )
            self.assertEqual(proc1.returncode, 0, proc1.stdout + proc1.stderr)
            settings_path = Path(home) / ".claude" / "settings.local.json"
            data1 = json.loads(settings_path.read_text())
            self.assertIn("PreToolUse", data1["hooks"])

            proc2 = run_install(["--project", "p", "--store", store, "--claude-dir", claude_dir], home)
            self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
            data2 = json.loads(settings_path.read_text())
            self.assertNotIn(
                "PreToolUse", data2.get("hooks", {}),
                "stale pre-edit-chain.sh entries survived a re-run with no --code-root",
            )
            # the write hooks must also have lost MEMCONTINUUM_CODE_ROOT
            cmd = data2["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
            self.assertNotIn("MEMCONTINUUM_CODE_ROOT", cmd)
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestMultipleCodeRoots(unittest.TestCase):
    def test_two_code_roots_render_pretooluse_items_for_both_hooks(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            root_a = str(Path(home) / "code-a")
            root_b = str(Path(home) / "code-b")
            os.makedirs(root_a, exist_ok=True)
            os.makedirs(root_b, exist_ok=True)
            proc = run_install(
                ["--project", "multi", "--store", store,
                 "--code-root", root_a, "--code-root", root_b,
                 "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            data = json.loads((Path(home) / ".claude" / "settings.local.json").read_text())
            items = [h for group in data["hooks"]["PreToolUse"] for h in group.get("hooks", [])]
            # pre-edit-chain: Edit+Write for each of 2 roots (4); newfile-nudge:
            # Write-only for each of 2 roots (2) -- 6 total.
            self.assertEqual(len(items), 6, items)
            pre_edit_items = [h for h in items if "pre-edit-chain.sh" in h["command"]]
            nudge_items = [h for h in items if "newfile-nudge.sh" in h["command"]]
            self.assertEqual(len(pre_edit_items), 4, pre_edit_items)
            self.assertEqual(len(nudge_items), 2, nudge_items)

            # Fix-round 2026-08-31: `if` filter paths use Claude Code's
            # permission-rule syntax, where a single leading slash anchors at
            # the settings source, not the filesystem root -- an absolute
            # code root (root_a/root_b already start with `/`) needs a
            # SECOND leading slash to match anything.
            ifs = sorted(h["if"] for h in pre_edit_items)
            self.assertEqual(ifs, sorted([
                f"Edit(/{root_a}/**)", f"Write(/{root_a}/**)",
                f"Edit(/{root_b}/**)", f"Write(/{root_b}/**)",
            ]))
            for h in pre_edit_items:
                if root_a in h["if"]:
                    self.assertIn(f"MEMCONTINUUM_STRIP_PREFIX={root_a}/", h["command"])
                else:
                    self.assertIn(f"MEMCONTINUUM_STRIP_PREFIX={root_b}/", h["command"])

            nudge_ifs = sorted(h["if"] for h in nudge_items)
            self.assertEqual(nudge_ifs, sorted([f"Write(/{root_a}/**)", f"Write(/{root_b}/**)"]))
            for h in nudge_items:
                root = root_a if root_a in h["if"] else root_b
                self.assertIn(f"MEMCONTINUUM_CODE_ROOT={root}", h["command"])

            # write hooks only support one MEMCONTINUUM_CODE_ROOT -- must be the first root given
            post_cmd = data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
            self.assertIn(f"MEMCONTINUUM_CODE_ROOT={root_a}", post_cmd)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_two_code_roots_dry_run_renders_valid_json(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            root_a = str(Path(home) / "code-a")
            root_b = str(Path(home) / "code-b")
            proc = run_install(
                ["--project", "multi", "--store", store,
                 "--code-root", root_a, "--code-root", root_b,
                 "--claude-dir", str(Path(home) / ".claude"), "--dry-run"],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestSingleHomeTwoProjectIsolation(unittest.TestCase):
    """The discriminating isolation check: two projects sharing ONE
    MEMCONTINUUM_HOME. Two separate sandboxed HOMEs (TestTwoProjectIsolation
    below) can't fail even if project naming were botched, since cross-
    contamination is impossible by construction there."""

    def test_two_projects_one_home_get_distinct_dbs(self):
        home = sandbox_home()
        try:
            store_a = str(Path(home) / "store-a")
            store_b = str(Path(home) / "store-b")
            claude_dir = str(Path(home) / ".claude")
            proc_a = run_install(
                ["--project", "shared-home-a", "--store", store_a, "--claude-dir", claude_dir], home)
            proc_b = run_install(
                ["--project", "shared-home-b", "--store", store_b, "--claude-dir", claude_dir], home)
            self.assertEqual(proc_a.returncode, 0, proc_a.stdout + proc_a.stderr)
            self.assertEqual(proc_b.returncode, 0, proc_b.stdout + proc_b.stderr)

            mc_home = Path(home) / ".memcontinuum"
            db_a = mc_home / "shared-home-a.sqlite"
            db_b = mc_home / "shared-home-b.sqlite"
            self.assertTrue(db_a.is_file())
            self.assertTrue(db_b.is_file())
            self.assertNotEqual(db_a.read_bytes(), b"")
            self.assertNotEqual(db_b.read_bytes(), b"")
            # project A's index must not contain project B's store path or vice versa
            self.assertNotIn(store_b.encode(), db_a.read_bytes())
            self.assertNotIn(store_a.encode(), db_b.read_bytes())
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTwoProjectIsolation(unittest.TestCase):
    def test_distinct_dbs_and_state(self):
        home_a = sandbox_home()
        home_b = sandbox_home()
        try:
            store_a = str(Path(home_a) / "store")
            store_b = str(Path(home_b) / "store")
            proc_a = run_install(
                ["--project", "proj-a", "--store", store_a,
                 "--claude-dir", str(Path(home_a) / ".claude")], home_a)
            proc_b = run_install(
                ["--project", "proj-b", "--store", store_b,
                 "--claude-dir", str(Path(home_b) / ".claude")], home_b)
            self.assertEqual(proc_a.returncode, 0, proc_a.stdout + proc_a.stderr)
            self.assertEqual(proc_b.returncode, 0, proc_b.stdout + proc_b.stderr)

            db_a = Path(home_a) / ".memcontinuum" / "proj-a.sqlite"
            db_b = Path(home_b) / ".memcontinuum" / "proj-b.sqlite"
            self.assertTrue(db_a.is_file())
            self.assertTrue(db_b.is_file())
            self.assertNotEqual(db_a, db_b)
            # cross-contamination check: proj-b's home has no proj-a db and vice versa
            self.assertFalse((Path(home_a) / ".memcontinuum" / "proj-b.sqlite").exists())
            self.assertFalse((Path(home_b) / ".memcontinuum" / "proj-a.sqlite").exists())
        finally:
            shutil.rmtree(home_a, ignore_errors=True)
            shutil.rmtree(home_b, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestFailureModes(unittest.TestCase):
    def test_missing_python_fails_loudly(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            # --claude-dir given explicitly (R5, round 4: the --claude-dir
            # refusal now fires before python resolution -- this test is
            # about the python failure specifically, so it must clear that
            # earlier pure-argument check first to actually reach it).
            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude"),
                 "--python", "/no/such/python"],
                home,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("python", (proc.stdout + proc.stderr).lower())
            self.assertFalse(Path(store).exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_store_inside_foreign_git_repo_without_force_fails(self):
        home = sandbox_home()
        try:
            outer = Path(home) / "outer-repo"
            outer.mkdir()
            subprocess.run(["git", "init", "-q", str(outer)], check=True)
            store = str(outer / "nested-store")
            claude_dir = str(Path(home) / ".claude")
            proc = run_install(["--project", "p", "--store", store, "--claude-dir", claude_dir], home)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(Path(store).exists())

            # --force overrides
            proc2 = run_install(
                ["--project", "p", "--store", store, "--claude-dir", claude_dir, "--force"], home)
            self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
            self.assertTrue(Path(store).is_dir())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_unwritable_claude_dir_fails_loudly(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            locked_parent = Path(home) / "locked"
            locked_parent.mkdir()
            os.chmod(locked_parent, 0o500)
            claude_dir = str(locked_parent / "sub" / ".claude")
            try:
                proc = run_install(
                    ["--project", "p", "--store", store, "--claude-dir", claude_dir],
                    home,
                )
                self.assertNotEqual(proc.returncode, 0)
            finally:
                os.chmod(locked_parent, 0o700)
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreExistingStoreRepo(unittest.TestCase):
    def test_store_already_a_git_repo_is_not_reinitialized(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            os.makedirs(store)
            subprocess.run(["git", "init", "-q", store], check=True)
            (Path(store) / "seed.txt").write_text("pre-existing content\n")
            # F10 store-shape marker: a bare git repo with unrelated content
            # is now refused (see TestAdoptClassification below) -- a
            # genuine adopt case carries at least one of this tool's markers.
            os.makedirs(str(Path(store) / "topics"))
            (Path(store) / "topics" / ".gitkeep").write_text("")
            subprocess.run(
                ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "add", "-A"], check=True,
            )
            subprocess.run(
                ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-q", "-m", "pre-existing commit"], check=True,
            )
            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            log = subprocess.run(
                ["git", "-C", store, "log", "--oneline"], capture_output=True, text=True
            )
            commits = [l for l in log.stdout.strip().splitlines() if l]
            self.assertEqual(len(commits), 1, "scripts/repo-init.sh must not create a second commit")
            self.assertIn("pre-existing commit", log.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_adopted_store_hand_authored_readme_not_clobbered(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            os.makedirs(store)
            subprocess.run(["git", "init", "-q", store], check=True)
            # F10 store-shape marker: a README mentioning MemContinuum is one
            # of the accepted markers (the other is a topics/incidents/
            # concepts dir) -- realistic for a hand-authored provenance note
            # on a MemContinuum store, and required for this to still count
            # as "adopt" rather than "refuse: no markers".
            hand_authored = (
                "# My Hand-Authored MemContinuum Store\n\n"
                "Do not overwrite this provenance note.\n"
            )
            (Path(store) / "README.md").write_text(hand_authored)
            (Path(store) / ".gitignore").write_text("*.sqlite\ncustom-ignore-line\n")
            subprocess.run(
                ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "add", "-A"], check=True,
            )
            subprocess.run(
                ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-q", "-m", "pre-existing commit"], check=True,
            )
            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual((Path(store) / "README.md").read_text(), hand_authored)
            self.assertIn("custom-ignore-line", (Path(store) / ".gitignore").read_text())
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestDefaultStoreName(unittest.TestCase):
    """Omitting --store applies the marked naming convention (owner ruling
    2026-08-31): never a generic "memory/", never a bare "MemContinuum" --
    "<repo>-MemContinuum-Store" beside a git repo, "MemContinuum-Store" inside
    a plain working folder."""

    def test_default_beside_a_git_repo_is_a_marked_sibling(self):
        home = sandbox_home()
        try:
            repo = Path(home) / "proj"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
            proc = run_install(["--project", "p", "--dry-run"], home, cwd=str(repo))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"defaulting to {home}/proj-MemContinuum-Store", proc.stdout)
            # the hooks stay with the REPO, not the parent dir the sibling
            # store happens to land in
            self.assertIn(f"{repo}/.claude", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_default_in_a_plain_folder_is_inside_it(self):
        home = sandbox_home()
        try:
            work = Path(home) / "docs"
            work.mkdir()
            proc = run_install(["--project", "p", "--dry-run"], home, cwd=str(work))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"defaulting to {work}/MemContinuum-Store", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTwoProjectsOneClaudeDir(unittest.TestCase):
    """Two projects wired into the SAME claude-dir must coexist: the merge
    identifies its own entries by basename AND project, so initializing the
    second project must not unwire the first (regate finding 1 -- basename-
    only identification silently dropped the earlier project's entries; the
    engine's own wiring was destroyed exactly this way by a test run)."""

    def test_second_project_does_not_unwire_the_first(self):
        """F1 regression: each project also gets a --code-root, so each gets
        a newfile-nudge.sh entry too (the one hook whose template used to
        render with NO project marker at all -- markerless entries read as
        "ours" to any project's sweep, so alpha's nudge entry used to vanish
        the moment beta re-ran against this shared claude-dir)."""
        home = sandbox_home()
        try:
            claude = str(Path(home) / ".claude")
            code_roots = {}
            for name in ("alpha", "beta"):
                store = str(Path(home) / f"{name}-store")
                code_root = str(Path(home) / f"{name}-code")
                os.makedirs(code_root, exist_ok=True)
                code_roots[name] = code_root
                proc = run_install(
                    ["--project", name, "--store", store, "--claude-dir", claude,
                     "--code-root", code_root],
                    home,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

            def nudge_items(data):
                return [
                    h
                    for groups in data.get("hooks", {}).values()
                    for g in groups
                    if g.get("matcher") == "Write"
                    for h in g.get("hooks", [])
                    if "newfile-nudge.sh" in h.get("command", "")
                ]

            text = (Path(claude) / "settings.local.json").read_text(encoding="utf-8")
            data = json.loads(text)
            cmds = [
                i["command"]
                for groups in data.get("hooks", {}).values()
                for g in groups
                for i in g.get("hooks", [])
            ]
            alpha = [c for c in cmds if "MEMCONTINUUM_PROJECT=alpha" in c]
            beta = [c for c in cmds if "MEMCONTINUUM_PROJECT=beta" in c]
            # 5 write hooks + pre-edit-chain (Edit+Write) + newfile-nudge = 8
            # marked entries per project.
            self.assertGreaterEqual(len(alpha), 8, "first project's wiring was lost")
            self.assertGreaterEqual(len(beta), 8)

            alpha_nudge = [h for h in nudge_items(data) if "MEMCONTINUUM_PROJECT=alpha" in h["command"]]
            beta_nudge = [h for h in nudge_items(data) if "MEMCONTINUUM_PROJECT=beta" in h["command"]]
            self.assertEqual(len(alpha_nudge), 1, nudge_items(data))
            self.assertEqual(len(beta_nudge), 1, nudge_items(data))

            # and a re-run of alpha replaces alpha's entries, not beta's
            proc = run_install(
                ["--project", "alpha", "--store", str(Path(home) / "alpha-store"),
                 "--claude-dir", claude, "--code-root", code_roots["alpha"]], home,
            )
            self.assertEqual(proc.returncode, 0)
            data = json.loads((Path(claude) / "settings.local.json").read_text(encoding="utf-8"))
            cmds = [
                i["command"]
                for groups in data.get("hooks", {}).values()
                for g in groups
                for i in g.get("hooks", [])
            ]
            self.assertGreaterEqual(
                len([c for c in cmds if "MEMCONTINUUM_PROJECT=beta" in c]), 8)
            self.assertEqual(
                len([c for c in cmds if "MEMCONTINUUM_PROJECT=alpha" in c]), len(alpha),
                "alpha re-run duplicated or dropped its own entries")
            # F1's actual regression target: beta's nudge entry specifically
            # must still be there after alpha's re-run.
            beta_nudge_after = [h for h in nudge_items(data) if "MEMCONTINUUM_PROJECT=beta" in h["command"]]
            self.assertEqual(len(beta_nudge_after), 1, nudge_items(data))
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestNoMachineIdentifyingContent(unittest.TestCase):
    """Privacy requirement, broader than just "no hardcoded default": no
    tracked file anywhere in this repo -- including tests/ -- may name this
    development machine's username, its absolute checkout path, or the private
    codebase this engine is developed and benchmarked against. The one
    deliberate exception is LICENSE's copyright line.

    The needles below are built from string fragments rather than written as
    contiguous literals, on purpose: a `grep`-shaped regression test for "this
    substring must not appear in tracked content" can't spell that substring
    out literally in its own tracked source without permanently tripping over
    its own definition.
    """

    def test_only_license_names_the_dev_machine(self):
        result = subprocess.run(
            ["git", "ls-files"], cwd=str(TOOLS_DIR), capture_output=True, text=True, check=True,
        )
        username_needle = "kra" + "kozavr"
        path_needle = "/mnt/d/!_WORK_" + "!"
        # The private codebase this engine is developed against must not be
        # named either -- not its checkout, not its module prefixes, not its
        # product name. Its symbols and probe queries describe that project's
        # internal architecture, so they live in an untracked probe file
        # (tests/test_code_index.py load_probe_set) rather than in tracked
        # content. Same split-literal trick as the two needles above.
        project_needles = ["mmd" + "-swift", "MMD" + "App", "MMD" + "Core", "Shot" + "Porter"]
        needles = [username_needle, path_needle] + project_needles
        offenders = {}
        for rel in result.stdout.splitlines():
            if not rel:
                continue
            p = TOOLS_DIR / rel
            if not p.is_file():
                continue
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            hits = [n for n in needles if n in text]
            if hits:
                offenders[rel] = hits
        # LICENSE's copyright line is the one deliberate exception (and only
        # counts as one if/when LICENSE is actually tracked by git).
        disallowed = {rel: hits for rel, hits in offenders.items() if rel != "LICENSE"}
        self.assertEqual(
            disallowed, {},
            f"machine-identifying content found outside the allowed LICENSE exception: {disallowed}",
        )


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPythonResolutionOrder(unittest.TestCase):
    """$MEMCONTINUUM_PYTHON env -> $MEMCONTINUUM_HOME/config.sh ->
    <engine>/.venv/bin/python -> error naming --bootstrap-venv. Runs against
    a COPIED engine checkout (copy_engine) so no test ever creates, or
    depends on the absence of, a real .venv/ next to this repo's own
    scripts/repo-init.sh."""

    def test_error_when_nothing_resolves(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            store = str(Path(home) / "store")
            # --claude-dir given explicitly (R5, round 4: the --claude-dir
            # refusal now fires before python resolution -- this test is
            # about the python-resolution failure specifically, so it must
            # clear that earlier pure-argument check first to actually
            # reach it).
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--bootstrap-venv", proc.stdout + proc.stderr)
            self.assertIn("config.sh", proc.stdout + proc.stderr)
            self.assertFalse(Path(store).exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_memcontinuum_python_env_used_when_no_flag(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            fake_py = Path(engine_dir) / "not-the-venv" / "python"
            write_python_shim(fake_py)
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
                python=str(fake_py),
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {fake_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_engine_venv_used_when_no_flag_and_no_env(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            venv_py = Path(engine_dir) / ".venv" / "bin" / "python"
            write_python_shim(venv_py)
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {venv_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_python_flag_wins_over_env_and_venv(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            venv_py = Path(engine_dir) / ".venv" / "bin" / "python"
            write_python_shim(venv_py)
            explicit_py = Path(engine_dir) / "explicit" / "python"
            write_python_shim(explicit_py)
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude"),
                 "--python", str(explicit_py)],
                home,
                python="/no/such/env/python",  # would fail if this ever won
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {explicit_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_config_sh_python_used_when_no_env_no_flag_no_venv(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            cfg_py = Path(home) / "external-python" / "python"
            write_python_shim(cfg_py)
            mc_home = Path(home) / ".memcontinuum"
            mc_home.mkdir(parents=True, exist_ok=True)
            (mc_home / "config.sh").write_text(
                "if [ -z \"${MEMCONTINUUM_PYTHON:-}\" ]; then MEMCONTINUUM_PYTHON='%s'; fi\n"
                % cfg_py
            )
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {cfg_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_env_wins_over_config_sh(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            env_py = Path(home) / "env-python" / "python"
            write_python_shim(env_py)
            mc_home = Path(home) / ".memcontinuum"
            mc_home.mkdir(parents=True, exist_ok=True)
            (mc_home / "config.sh").write_text(
                "if [ -z \"${MEMCONTINUUM_PYTHON:-}\" ]; then "
                "MEMCONTINUUM_PYTHON='/no/such/config/python'; fi\n"
            )
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
                python=str(env_py),
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {env_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_pointer_config_sh_is_followed(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        other_dir = tempfile.mkdtemp(prefix="memcontinuum-other-home-")
        try:
            install_sh = copy_engine(engine_dir)
            real_py = Path(other_dir) / "real-python" / "python"
            write_python_shim(real_py)
            mc_home = Path(home) / ".memcontinuum"
            mc_home.mkdir(parents=True, exist_ok=True)
            (mc_home / "config.sh").write_text("MEMCONTINUUM_HOME='%s'\n" % other_dir)
            Path(other_dir, "config.sh").write_text(
                "if [ -z \"${MEMCONTINUUM_PYTHON:-}\" ]; then MEMCONTINUUM_PYTHON='%s'; fi\n"
                % real_py
            )
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {real_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)
            shutil.rmtree(other_dir, ignore_errors=True)

    def test_engine_venv_used_when_config_sh_sets_no_python(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            venv_py = Path(engine_dir) / ".venv" / "bin" / "python"
            write_python_shim(venv_py)
            mc_home = Path(home) / ".memcontinuum"
            mc_home.mkdir(parents=True, exist_ok=True)
            (mc_home / "config.sh").write_text("MEMCONTINUUM_ENGINE='%s'\n" % engine_dir)
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {venv_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_noisy_config_sh_stdout_does_not_corrupt_resolution(self):
        # Codex, 2026-09-01: resolve_python's config.sh sources now silence
        # stdout too (". \"$cfg\" >/dev/null 2>&1"), not just stderr -- a
        # config.sh that prints chatter used to have that chatter captured
        # into cfg_python by the command substitution, corrupting it into
        # "chatter/path" and wrongly blocking a valid engine-venv fallback.
        # Red against the pre-fix code, green now.
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            venv_py = Path(engine_dir) / ".venv" / "bin" / "python"
            write_python_shim(venv_py)
            mc_home = Path(home) / ".memcontinuum"
            mc_home.mkdir(parents=True, exist_ok=True)
            (mc_home / "config.sh").write_text(
                'echo "setup chatter"\n'
                'echo "more setup chatter" >&2\n'
            )
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {venv_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_malformed_config_sh_fails_open_to_engine_venv(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            venv_py = Path(engine_dir) / ".venv" / "bin" / "python"
            write_python_shim(venv_py)
            mc_home = Path(home) / ".memcontinuum"
            mc_home.mkdir(parents=True, exist_ok=True)
            (mc_home / "config.sh").write_text("syntax error (((\n")
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {venv_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    def test_python_flag_wins_over_config_sh(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            explicit_py = Path(engine_dir) / "explicit" / "python"
            write_python_shim(explicit_py)
            mc_home = Path(home) / ".memcontinuum"
            mc_home.mkdir(parents=True, exist_ok=True)
            (mc_home / "config.sh").write_text(
                "if [ -z \"${MEMCONTINUUM_PYTHON:-}\" ]; then "
                "MEMCONTINUUM_PYTHON='/no/such/config/python'; fi\n"
            )
            store = str(Path(home) / "store")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude"),
                 "--python", str(explicit_py)],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {explicit_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)

    # --bootstrap-venv vs. a competing config.sh is deliberately not tested
    # here: reading scripts/repo-init.sh's main flow shows --bootstrap-venv
    # sets PYTHON_BIN directly and skips resolve_python() entirely whenever
    # --python wasn't also given (see the `if [ "$BOOTSTRAP_VENV" -eq 1 ]`
    # block) -- so a competing config.sh can never be consulted, let alone
    # win, and a test asserting that would only re-prove TestBootstrapVenv's
    # existing coverage while duplicating its whole fake-uv/fake-python3
    # harness for no new signal.


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestBootstrapVenv(unittest.TestCase):
    """--bootstrap-venv, exercised offline: a fake `uv` (or, in the fallback
    test, a fake `python3`) on PATH intercepts the actual venv-creation and
    pip-install calls so nothing here ever touches the network, while still
    producing a working python (by delegating non-pip/non-venv calls to this
    machine's real venv python) so scripts/repo-init.sh's own later reindex/lint steps
    still succeed end to end."""

    def test_bootstrap_uses_uv_when_on_path_and_installs_requirements(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        fakebin = tempfile.mkdtemp(prefix="memcontinuum-fakebin-")
        try:
            install_sh = copy_engine(engine_dir)
            record = Path(fakebin) / "uv-invocations.log"
            fake_uv = Path(fakebin) / "uv"
            fake_uv.write_text(
                "#!/usr/bin/env bash\n"
                f'printf \'%s\\n\' "$*" >> "{record}"\n'
                'if [ "$1" = "venv" ]; then\n'
                '    dir="$2"\n'
                '    mkdir -p "$dir/bin"\n'
                f'    printf \'#!/usr/bin/env bash\\nexec "{VENV_PYTHON}" "$@"\\n\' > "$dir/bin/python"\n'
                '    chmod +x "$dir/bin/python"\n'
                '    exit 0\n'
                'fi\n'
                'exit 0\n'
            )
            fake_uv.chmod(0o755)

            store = str(Path(home) / "store")
            venv_dir = str(Path(home) / "bootstrapped-venv")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude"),
                 "--bootstrap-venv", venv_dir],
                home,
                path_prepend=[fakebin],
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((Path(venv_dir) / "bin" / "python").is_file())
            self.assertIn(f"python      : {venv_dir}/bin/python", proc.stdout)

            invocations = record.read_text()
            self.assertIn("venv", invocations)
            self.assertIn("requirements.txt", invocations)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)
            shutil.rmtree(fakebin, ignore_errors=True)

    def test_bootstrap_falls_back_to_python3_venv_and_pip_when_no_uv(self):
        # A real venv-python shim, written by a small static helper script
        # (not inlined into fake_python3 below) so there is only one level
        # of shell-quoting to reason about instead of two.
        MAKE_SHIM_SCRIPT = """#!/usr/bin/env bash
# make_python_shim.sh DIR PIP_RECORD REAL_PYTHON
dir="$1"
pip_record="$2"
real_python="$3"
mkdir -p "$dir/bin"
cat > "$dir/bin/python" <<SHIMEOF
#!/usr/bin/env bash
if [ "\\$1" = "-m" ] && [ "\\$2" = "pip" ]; then
    printf '%s\\n' "\\$*" >> "$pip_record"
    exit 0
fi
exec "$real_python" "\\$@"
SHIMEOF
chmod +x "$dir/bin/python"
"""
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        fakebin = tempfile.mkdtemp(prefix="memcontinuum-fakebin-")
        try:
            install_sh = copy_engine(engine_dir)
            pip_record = Path(fakebin) / "pip-invocations.log"

            make_shim = Path(fakebin) / "make_python_shim.sh"
            make_shim.write_text(MAKE_SHIM_SCRIPT)
            make_shim.chmod(0o755)

            fake_python3 = Path(fakebin) / "python3"
            fake_python3.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
                f'    exec "{make_shim}" "$3" "{pip_record}" "{VENV_PYTHON}"\n'
                "fi\n"
                "exit 0\n"
            )
            fake_python3.chmod(0o755)

            # PATH with NO uv at all -- a real `uv` lives on this machine's
            # PATH, so the fallback branch must be forced by omitting every
            # normal PATH entry that could contain it, keeping only what
            # bash/coreutils need plus our fakebin (which comes first).
            minimal_path = os.pathsep.join([fakebin, "/usr/bin", "/bin"])

            store = str(Path(home) / "store")
            venv_dir = str(Path(home) / "bootstrapped-venv")
            proc = run_install_at(
                install_sh,
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude"),
                 "--bootstrap-venv", venv_dir],
                home,
                extra_env={"PATH": minimal_path},
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((Path(venv_dir) / "bin" / "python").is_file())

            self.assertTrue(pip_record.is_file(), proc.stdout + proc.stderr)
            pip_invocations = pip_record.read_text()
            self.assertIn("requirements.txt", pip_invocations)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)
            shutil.rmtree(fakebin, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestClaudeDirRequiredWithExplicitStore(unittest.TestCase):
    """Fix-round-4 F3 (final ruling): an explicit --store with no explicit
    --claude-dir is a hard error, not a cwd-based guess -- even a git cwd can
    be the wrong repo (an explicit --store may be run from anywhere)."""

    def test_explicit_store_without_claude_dir_errors(self):
        home = sandbox_home()
        try:
            # cwd (home) IS a git repo here on purpose -- the ruling is
            # "no cwd-guessing" full stop, not "only when cwd isn't a repo".
            subprocess.run(["git", "init", "-q", home], check=True)
            store = str(Path(home) / "store")
            proc = run_install(["--project", "p", "--store", store], home)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--claude-dir", proc.stdout + proc.stderr)
            self.assertFalse(Path(store).exists())
            self.assertFalse((Path(home) / ".claude").exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_claude_dir_error_precedes_python_resolution(self):
        """R5 regression, round 4 gate: the --claude-dir refusal used to run
        AFTER --bootstrap-venv and python resolution -- an invalid
        invocation (explicit --store, no --claude-dir, and no python
        resolvable at all) used to die naming the WRONG problem ("no python
        found") instead of the actual one. Pure-argument validation must be
        checked first. python=None here (this checkout ships no .venv/ and
        the sandbox HOME strips MEMCONTINUUM_PYTHON) means python resolution
        would ALSO fail if it ever ran -- proving the error we get back is
        actually the --claude-dir one, not a lucky coincidence."""
        self.assertFalse(
            (TOOLS_DIR / ".venv" / "bin" / "python").exists(),
            "this test relies on no engine .venv existing in this checkout",
        )
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            proc = run_install(["--project", "p", "--store", store], home, python=None)
            self.assertNotEqual(proc.returncode, 0)
            out = proc.stdout + proc.stderr
            self.assertIn("--claude-dir", out)
            self.assertNotIn("no python found", out)
            self.assertFalse(Path(store).exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_explicit_store_with_claude_dir_wires_there(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            claude_dir = str(Path(home) / "somewhere-else" / ".claude")
            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", claude_dir], home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertTrue((Path(claude_dir) / "settings.local.json").is_file())
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestAdoptClassification(unittest.TestCase):
    """Fix-round-4 F10: classification happens BEFORE any mutation. --store
    at an existing git repo counts as "adopt" only if it already carries one
    of this tool's markers; otherwise it is refused outright, never silently
    adopted. An adopted store's memlint findings are report-only (exit 0); a
    freshly seeded store still hard-fails on any lint error."""

    @staticmethod
    def _duplicate_id_pair(store, name_a="a", name_b="b"):
        topics = Path(store) / "topics"
        topics.mkdir(parents=True, exist_ok=True)
        for name in (name_a, name_b):
            (topics / f"{name}.md").write_text(
                "---\ntype: topic\nid: DUP-1\ntitle: %s\narea: test\n---\nBody\n" % name
            )

    @staticmethod
    def _git_commit(store, message):
        subprocess.run(
            ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
             "add", "-A"], check=True,
        )
        subprocess.run(
            ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
             "commit", "-q", "-m", message], check=True,
        )

    def test_adopt_with_duplicate_ids_succeeds_with_findings_in_summary(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            os.makedirs(store)
            subprocess.run(["git", "init", "-q", store], check=True)
            self._duplicate_id_pair(store)
            self._git_commit(store, "legacy store with duplicate ids")

            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("duplicate id", proc.stdout)
            self.assertIn("NOTE: adopted store has memlint findings", proc.stdout)
            self.assertIn("Store install  : adopted", proc.stdout)
            # hooks were actually wired despite the findings -- report-only
            # must not mean "install aborted before the useful part."
            settings = json.loads((Path(home) / ".claude" / "settings.local.json").read_text())
            self.assertIn("hooks", settings)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_store_as_linked_worktree_adopts_cleanly(self):
        """R8 regression, round 4 gate (Codex): a store checked out via
        `git worktree add` has a .git FILE (a gitdir pointer), not a
        directory -- every `[ -d "$STORE/.git" ]` check used to misread
        that as "not a git repo at all", so a worktree store was refused
        outright (the containment check saw it as living inside its own
        main repo) or, under --force, seeded fresh content on top of the
        existing worktree. Must adopt cleanly, with no re-seeding, and its
        post-commit hook must land in the worktree's REAL hooks dir (under
        the main repo's .git/worktrees/<name>/hooks, not a nonexistent
        "$STORE/.git/hooks")."""
        home = sandbox_home()
        try:
            main_repo = str(Path(home) / "main-store")
            os.makedirs(main_repo)
            subprocess.run(["git", "init", "-q", main_repo], check=True)
            self._duplicate_id_pair(main_repo, "keepme", "keepme2")
            # give it real (non-duplicate) shape too, and a distinguishing
            # marker file so re-seeding would be detectable.
            topics = Path(main_repo) / "topics"
            (topics / "keepme.md").unlink()
            (topics / "keepme2.md").unlink()
            (topics / "T-0001.md").write_text(
                "---\ntype: topic\nid: T-0001\ntitle: real\narea: test\n---\nBody\n"
            )
            sentinel = Path(main_repo) / "topics" / "SENTINEL.md"
            sentinel.write_text("---\ntype: topic\nid: SENTINEL-1\ntitle: s\narea: test\n---\nkeep\n")
            self._git_commit(main_repo, "seed main store")

            worktree = str(Path(home) / "worktree-store")
            subprocess.run(
                ["git", "-C", main_repo, "worktree", "add", worktree, "-b", "wt-branch"],
                check=True, capture_output=True, text=True,
            )
            self.assertTrue(Path(worktree, ".git").is_file(), "a linked worktree's .git must be a FILE")

            proc = run_install(
                ["--project", "p", "--store", worktree, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("adopted", proc.stdout)
            # No re-seeding: the sentinel content survives untouched.
            self.assertTrue((Path(worktree) / "topics" / "SENTINEL.md").is_file())

            # Where git actually RUNS hooks for commits in the worktree:
            # `rev-parse --git-path hooks` (the shared repo's .git/hooks) --
            # NOT --git-dir (.git/worktrees/<name>), whose hooks/ git never
            # consults (regate round-2 probe, git 2.43).
            hooks_dir = subprocess.run(
                ["git", "-C", worktree, "rev-parse", "--git-path", "hooks"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
            if not os.path.isabs(hooks_dir):
                hooks_dir = str(Path(worktree) / hooks_dir)
            post_commit = Path(hooks_dir) / "post-commit"
            self.assertTrue(post_commit.is_file(), f"post-commit hook missing at {post_commit}")
            self.assertFalse(
                (Path(worktree) / ".git" / "hooks").exists(),
                "must never treat the worktree's .git (a FILE) as a hooks-holding directory",
            )

            # And the wrapper actually FIRES on a real worktree commit: the
            # install-time reindex left <home>/.memcontinuum/p.sqlite --
            # delete it, commit in the worktree, and the post-commit
            # reindex must recreate it.
            index_db = Path(home) / ".memcontinuum" / "p.sqlite"
            self.assertTrue(index_db.is_file(), "install-time reindex should have built the index")
            index_db.unlink()
            commit_env = dict(os.environ)
            for k in list(commit_env):
                if k.startswith("MEMCONTINUUM_"):
                    del commit_env[k]
            commit_env["HOME"] = home
            (Path(worktree) / "topics" / "T-0002.md").write_text(
                "---\ntype: topic\nid: T-0002\ntitle: wt\narea: test\n---\nBody\n"
            )
            subprocess.run(
                ["git", "-C", worktree, "add", "-A"],
                check=True, capture_output=True, text=True, env=commit_env,
            )
            subprocess.run(
                ["git", "-C", worktree, "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "worktree commit"],
                check=True, capture_output=True, text=True, env=commit_env,
            )
            self.assertTrue(
                index_db.is_file(),
                "a commit made in the linked worktree must run the post-commit reindex wrapper",
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_unrelated_git_repo_store_refused(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            os.makedirs(store)
            (Path(store) / "unrelated.txt").write_text("just some other repo's content\n")
            subprocess.run(["git", "init", "-q", store], check=True)
            self._git_commit(store, "unrelated repo")

            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.returncode, 9, proc.stdout + proc.stderr)
            self.assertIn("none of this tool's markers", proc.stdout + proc.stderr)
            # nothing was seeded into the unrelated repo, and no --force
            # carve-out exists for this refusal.
            self.assertFalse((Path(store) / "topics").exists())
            self.assertFalse((Path(home) / ".claude").exists())

            proc2 = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude"),
                 "--force"],
                home,
            )
            self.assertNotEqual(proc2.returncode, 0, "no --force carve-out for the shape refusal")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_fresh_seed_with_planted_duplicate_ids_still_hard_fails(self):
        """A location that is NOT YET a git repo is always a fresh seed,
        whatever content it happens to hold -- repo-init.sh will git-init it
        itself. A lint error there still hard-fails (exit 8): this installer
        (via reindex/lint of whatever the seed step + any pre-planted
        content produced) is the only thing that could have put it there."""
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            self._duplicate_id_pair(store)  # store dir exists, NOT a git repo yet

            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.returncode, 8, proc.stdout + proc.stderr)
            self.assertIn("duplicate id", proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestProjectNameCanonicalization(unittest.TestCase):
    """Fix-round-4 F8 addendum: --project is restricted to [A-Za-z0-9._-]+,
    not just "no /" -- it is embedded, unquoted, as a MEMCONTINUUM_PROJECT=
    identity marker in every hook command line."""

    def test_quote_in_project_name_refused(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            proc = run_install(
                ["--project", "a'b", "--store", store, "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(Path(store).exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestCodeCensusAndConsentDialogue(unittest.TestCase):
    """Task 10 brief Step 1 scenarios (a)-(d) plus the carried missing-
    --code-root check and the KNOWN_EXTS-always-rendered requirement."""

    def test_a_langs_flag_non_interactive_wires_python_and_runs_code_reindex(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = make_python_and_cs_corpus(Path(home) / "code")
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "widgetco", "--store", store, "--code-root", str(code_root),
                 "--claude-dir", str(claude_dir), "--langs", "python", "--non-interactive"],
                home, python=VENV_PYTHON,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("--lang python", proc.stdout)

            settings = json.loads((claude_dir / "settings.local.json").read_text())
            nudge_groups = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Write"]
            self.assertEqual(len(nudge_groups), 1, settings["hooks"]["PreToolUse"])
            item = nudge_groups[0]["hooks"][0]
            self.assertIn("MEMCONTINUUM_LANG_EXTS='*.py'", item["command"])
            self.assertIn("MEMCONTINUUM_KNOWN_EXTS=", item["command"])

            code_db = Path(home) / ".memcontinuum" / "widgetco-code.sqlite"
            self.assertTrue(code_db.is_file(), "code db not created -- initial code-reindex did not run")
            conn = sqlite3.connect(str(code_db))
            row = conn.execute("SELECT langs FROM code_meta WHERE project=?", ("widgetco",)).fetchone()
            conn.close()
            self.assertIsNotNone(row, "no code_meta row for project widgetco")
            self.assertEqual(row[0], "python")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_b_census_table_in_stdout_names_unsupported_cs_extension(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = make_python_and_cs_corpus(Path(home) / "code")
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "p", "--store", store, "--code-root", str(code_root),
                 "--claude-dir", str(claude_dir), "--non-interactive"],
                home, python=VENV_PYTHON,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(".cs", proc.stdout)
            self.assertIn("unsupported", proc.stdout.lower())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_c_no_code_root_means_no_census_and_no_new_env_tokens(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "p", "--store", store, "--claude-dir", str(claude_dir)],
                home, python=VENV_PYTHON,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("census", proc.stdout.lower())
            settings_text = (claude_dir / "settings.local.json").read_text()
            self.assertNotIn("MEMCONTINUUM_LANG_EXTS", settings_text)
            self.assertNotIn("MEMCONTINUUM_KNOWN_EXTS", settings_text)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_d_non_interactive_without_langs_is_language_less_with_note(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = make_python_and_cs_corpus(Path(home) / "code")
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "p", "--store", store, "--code-root", str(code_root),
                 "--claude-dir", str(claude_dir), "--non-interactive"],
                home, python=VENV_PYTHON,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("language-less", proc.stdout.lower())

            code_db = Path(home) / ".memcontinuum" / "p-code.sqlite"
            self.assertFalse(
                code_db.exists(),
                "code-reindex must not run when --non-interactive gave no --langs (language-less wiring)",
            )

            settings = json.loads((claude_dir / "settings.local.json").read_text())
            nudge_groups = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Write"]
            item = nudge_groups[0]["hooks"][0]
            self.assertNotIn("MEMCONTINUUM_LANG_EXTS", item["command"])
            self.assertIn("MEMCONTINUUM_KNOWN_EXTS=", item["command"])
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_missing_code_root_dir_fails_with_own_message(self):
        """Carry (Task 8 reviewer): repo-init does its own existence check on
        each code root BEFORE invoking census -- a missing dir is repo-init's
        own error, never inferred from census's empty-dict fail-open."""
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            missing = Path(home) / "does-not-exist"
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "p", "--store", store, "--code-root", str(missing),
                 "--claude-dir", str(claude_dir), "--non-interactive"],
                home, python=VENV_PYTHON,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not exist", proc.stdout + proc.stderr)
            self.assertFalse(Path(store).exists(), "must fail before writing anything")
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_known_exts_token_present_even_with_no_langs_chosen_and_empty_root(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = Path(home) / "code"
            os.makedirs(code_root, exist_ok=True)
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "p", "--store", store, "--code-root", str(code_root),
                 "--claude-dir", str(claude_dir), "--non-interactive"],
                home, python=VENV_PYTHON,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            settings = json.loads((claude_dir / "settings.local.json").read_text())
            item = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Write"][0]["hooks"][0]
            self.assertIn("MEMCONTINUUM_KNOWN_EXTS='*.py *.swift'", item["command"])
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_not_a_tty_without_non_interactive_fails_clearly_not_hangs(self):
        """A proposed (non-empty) language set with no --langs/--non-interactive
        and no tty on stdin must fail fast with a clear message, never hang
        waiting on /dev/tty."""
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = make_python_and_cs_corpus(Path(home) / "code")
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "p", "--store", store, "--code-root", str(code_root),
                 "--claude-dir", str(claude_dir)],
                home, python=VENV_PYTHON,
                stdin=subprocess.DEVNULL,
                timeout=20,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("tty", (proc.stdout + proc.stderr).lower())
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
