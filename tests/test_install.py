"""Tests for install.sh -- the new-project installer.

These exercise the real script via subprocess (matching the convention in
tests/test_hooks.py), with HOME sandboxed to a fresh temp dir per test so
$HOME/.memcontinuum (the default MEMCONTINUUM_HOME) and settings.local.json
never touch the real machine's state.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
INSTALL_SH = TOOLS_DIR / "install.sh"
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


def run_install(args, home, timeout=60, python=VENV_PYTHON, extra_env=None):
    """Runs the real install.sh via subprocess with a sandboxed HOME.

    `python` sets MEMCONTINUUM_PYTHON to this machine's venv by default, so
    every test in this file exercises install.sh's own logic rather than its
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
    )
    return proc


def run_install_at(install_sh, args, home, path_prepend=None, timeout=60, python=None, extra_env=None):
    """Like run_install, but against an arbitrary install.sh path (a copied
    engine checkout -- see copy_engine below) and with PATH control, for the
    --bootstrap-venv / python-resolution-order tests that need to run a copy
    without $MEMCONTINUUM_PYTHON and without the real repo's absence of a
    checked-in .venv/ leaking in either direction."""
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
    )
    return proc


def copy_engine(dst):
    """Copies just the engine files install.sh needs (not memory/, .claude/,
    fixtures/, tests/) into dst, so a test can plant its own .venv/ next to
    the copy, or run with PATH manipulated, without ever touching the real
    checkout this test suite itself lives in."""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy(INSTALL_SH, dst / "install.sh")
    (dst / "install.sh").chmod(0o755)
    for name in ("memidx.py", "memlint.py", "requirements.txt"):
        shutil.copy(TOOLS_DIR / name, dst / name)
    for name in ("hooks", "templates", "skills"):
        shutil.copytree(TOOLS_DIR / name, dst / name)
    return dst / "install.sh"


def write_python_shim(path, target=VENV_PYTHON):
    """Writes an executable at `path` that just execs `target` -- a stand-in
    venv python good enough for install.sh's own `-c 'pass'` sanity check
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
        cls.proc = run_install(
            ["--project", "widgetco", "--store", cls.store, "--code-root", cls.code_root],
            cls.home,
        )
        cls.claude_dir = Path(cls.home) / ".claude"
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
        self.assertEqual(item["if"], f"Write({self.code_root}/**)")
        self.assertIn(f"MEMCONTINUUM_CODE_ROOT={self.code_root}", item["command"])
        self.assertIn("MEMCONTINUUM_PYTHON=", item["command"])
        self.assertNotIn("MEMCONTINUUM_ROOT=", item["command"])
        self.assertNotIn("MEMCONTINUUM_PROJECT=", item["command"])
        self.assertNotIn("MEMCONTINUUM_STRIP_PREFIX=", item["command"])
        # pre-edit-chain's own group must be untouched by this addition.
        edit_write_groups = [g for g in pre if g.get("matcher") == "Edit|Write"]
        self.assertEqual(len(edit_write_groups), 1, pre)

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
            ["--project", "widgetco", "--store", cls.store, "--code-root", cls.code_root],
            cls.home,
        )
        cls.settings_after_1 = cls.settings_path.read_text()
        cls.proc2 = run_install(
            ["--project", "widgetco", "--store", cls.store, "--code-root", cls.code_root],
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
                ["--project", "ghost", "--store", store, "--code-root", code_root, "--dry-run"],
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
            run_install(["--project", "p", "--store", store, "--dry-run"], home)
            proc = run_install(["--project", "p", "--store", store], home)
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
            proc = run_install(["--project", "rationale-only", "--store", store], home)
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
            proc1 = run_install(
                ["--project", "p", "--store", store, "--code-root", code_root], home
            )
            self.assertEqual(proc1.returncode, 0, proc1.stdout + proc1.stderr)
            settings_path = Path(home) / ".claude" / "settings.local.json"
            data1 = json.loads(settings_path.read_text())
            self.assertIn("PreToolUse", data1["hooks"])

            proc2 = run_install(["--project", "p", "--store", store], home)
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
                 "--code-root", root_a, "--code-root", root_b],
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

            ifs = sorted(h["if"] for h in pre_edit_items)
            self.assertEqual(ifs, sorted([
                f"Edit({root_a}/**)", f"Write({root_a}/**)",
                f"Edit({root_b}/**)", f"Write({root_b}/**)",
            ]))
            for h in pre_edit_items:
                if root_a in h["if"]:
                    self.assertIn(f"MEMCONTINUUM_STRIP_PREFIX={root_a}/", h["command"])
                else:
                    self.assertIn(f"MEMCONTINUUM_STRIP_PREFIX={root_b}/", h["command"])

            nudge_ifs = sorted(h["if"] for h in nudge_items)
            self.assertEqual(nudge_ifs, sorted([f"Write({root_a}/**)", f"Write({root_b}/**)"]))
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
                 "--code-root", root_a, "--code-root", root_b, "--dry-run"],
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
            proc_a = run_install(["--project", "shared-home-a", "--store", store_a], home)
            proc_b = run_install(["--project", "shared-home-b", "--store", store_b], home)
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
            proc_a = run_install(["--project", "proj-a", "--store", store_a], home_a)
            proc_b = run_install(["--project", "proj-b", "--store", store_b], home_b)
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
            proc = run_install(
                ["--project", "p", "--store", store, "--python", "/no/such/python"],
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
            proc = run_install(["--project", "p", "--store", store], home)
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(Path(store).exists())

            # --force overrides
            proc2 = run_install(["--project", "p", "--store", store, "--force"], home)
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
            subprocess.run(
                ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "add", "-A"], check=True,
            )
            subprocess.run(
                ["git", "-C", store, "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                 "commit", "-q", "-m", "pre-existing commit"], check=True,
            )
            proc = run_install(["--project", "p", "--store", store], home)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            log = subprocess.run(
                ["git", "-C", store, "log", "--oneline"], capture_output=True, text=True
            )
            commits = [l for l in log.stdout.strip().splitlines() if l]
            self.assertEqual(len(commits), 1, "install.sh must not create a second commit")
            self.assertIn("pre-existing commit", log.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_adopted_store_hand_authored_readme_not_clobbered(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            os.makedirs(store)
            subprocess.run(["git", "init", "-q", store], check=True)
            hand_authored = "# My Hand-Authored Store\n\nDo not overwrite this provenance note.\n"
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
            proc = run_install(["--project", "p", "--store", store], home)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual((Path(store) / "README.md").read_text(), hand_authored)
            self.assertIn("custom-ignore-line", (Path(store) / ".gitignore").read_text())
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestNoMachineIdentifyingContent(unittest.TestCase):
    """Privacy requirement, broader than just "no hardcoded default": no
    tracked file anywhere in this repo -- including tests/ -- may name this
    development machine's username or its absolute checkout path. The one
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
            hits = [n for n in (username_needle, path_needle) if n in text]
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
    """$MEMCONTINUUM_PYTHON env -> <engine>/.venv/bin/python -> error naming
    --bootstrap-venv. Runs against a COPIED engine checkout (copy_engine) so
    no test ever creates, or depends on the absence of, a real .venv/ next
    to this repo's own install.sh."""

    def test_error_when_nothing_resolves(self):
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        try:
            install_sh = copy_engine(engine_dir)
            store = str(Path(home) / "store")
            proc = run_install_at(install_sh, ["--project", "p", "--store", store], home)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--bootstrap-venv", proc.stdout + proc.stderr)
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
                install_sh, ["--project", "p", "--store", store], home,
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
            proc = run_install_at(install_sh, ["--project", "p", "--store", store], home)
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
                ["--project", "p", "--store", store, "--python", str(explicit_py)],
                home,
                python="/no/such/env/python",  # would fail if this ever won
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(f"python      : {explicit_py}", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestBootstrapVenv(unittest.TestCase):
    """--bootstrap-venv, exercised offline: a fake `uv` (or, in the fallback
    test, a fake `python3`) on PATH intercepts the actual venv-creation and
    pip-install calls so nothing here ever touches the network, while still
    producing a working python (by delegating non-pip/non-venv calls to this
    machine's real venv python) so install.sh's own later reindex/lint steps
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
                ["--project", "p", "--store", store, "--bootstrap-venv", venv_dir],
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
                ["--project", "p", "--store", store, "--bootstrap-venv", venv_dir],
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


if __name__ == "__main__":
    unittest.main()
