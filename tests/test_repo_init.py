"""Tests for scripts/repo-init.sh -- the new-project installer.

These exercise the real script via subprocess (matching the convention in
tests/test_hooks.py), with HOME sandboxed to a fresh temp dir per test so
$HOME/.memcontinuum (the default MEMCONTINUUM_HOME) and settings.local.json
never touch the real machine's state.
"""
import json
import os
import re
import shlex
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
# Same seam tests/test_write_hooks.py uses: tests/run_bash32.sh points MC_BASH
# at a real bash 3.2.57 binary. repo-init.sh shells out to
# memcontinuum-decide.sh through "$BASH", so that nested call follows.
MC_BASH = os.environ.get("MC_BASH", "bash")
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
    # Resolved, not raw (Ruling 89, symlink-paths fix): repo-init.sh's
    # abspath() now resolves symlinks (os.path.realpath, not
    # os.path.abspath), so every --store/--claude-dir/--code-root this file
    # builds from `home` and later compares against rendered/registry
    # output must be resolved too, or the comparison is a Linux-only
    # assumption again -- macOS's TMPDIR (/var/folders/... ->
    # /private/var/folders/...) diverges exactly like a symlinked git
    # toplevel does. Same fix, same reasoning, as git_repo() in
    # tests/test_update.py -- this file just never needed it before,
    # because abspath() used to be a no-op on an already-absolute path.
    return os.path.realpath(tempfile.mkdtemp(prefix="memcontinuum-install-test-home-"))


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
        [MC_BASH, str(INSTALL_SH)] + args,
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
        [MC_BASH, str(install_sh)] + args,
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
    # repo-init.sh sources this sibling for its store predicates and its
    # registry-row helpers, from its first validation onwards -- a copied
    # checkout without it exits "incomplete checkout" before doing anything.
    shutil.copy(TOOLS_DIR / "scripts" / "mc-registry-lib.sh", dst / "scripts" / "mc-registry-lib.sh")
    shutil.copy(TOOLS_DIR / "scripts" / "memcontinuum-decide.sh", dst / "scripts" / "memcontinuum-decide.sh")
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
        proc = subprocess.run([MC_BASH, "-n", str(INSTALL_SH)], capture_output=True, text=True)
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
        # D1 (updater workstream): the installed copy carries one extra
        # line -- a "<!-- memcontinuum-rendered: SHA -->" stamp right after
        # the frontmatter's closing "---" -- that the template never has.
        # Stripped before comparing, the two are still identical.
        dst = self.claude_dir / "skills" / "memory-search" / "SKILL.md"
        src = TOOLS_DIR / "skills" / "memory-search" / "SKILL.md"
        self.assertTrue(dst.is_file())
        stamp_re = re.compile(r"^<!-- memcontinuum-rendered: [^\n]* -->\n", re.M)
        self.assertEqual(stamp_re.sub("", dst.read_text()), src.read_text())

    def test_skill_copy_stamp_present(self):
        dst = self.claude_dir / "skills" / "memory-search" / "SKILL.md"
        text = dst.read_text()
        lines = text.splitlines()
        # Right after the frontmatter's closing "---" (the second "---"
        # line), never at byte 0 -- the opening "---" must stay line 1 for
        # the skill loader.
        self.assertEqual(lines[0], "---")
        fm_end = lines[1:].index("---") + 1
        self.assertTrue(
            lines[fm_end + 1].startswith("<!-- memcontinuum-rendered: "),
            f"expected a stamp line right after the frontmatter, got: {lines[fm_end + 1]!r}",
        )

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

    def test_pre_edit_chain_commands_render_outer_timeout_of_5(self):
        """F6 (external-review fix round): the OUTER Claude Code backstop.
        templates/code-root-filter-pair.json.tmpl renders `"timeout": 5` into
        both the Edit and Write pre-edit-chain.sh command lines; this proves
        the value survives the real repo-init.sh render/parse/write pipeline
        end to end, not just the template text on its own (see
        tests/test_hooks.py and tests/test_mc_settings_merge.py for the two
        narrower checks this one sits on top of)."""
        data = json.loads(self.settings_path.read_text())
        pre = data["hooks"]["PreToolUse"]
        pre_edit_commands = [
            h
            for group in pre
            for h in group.get("hooks", [])
            if "pre-edit-chain.sh" in h.get("command", "")
        ]
        self.assertEqual(len(pre_edit_commands), 2, pre_edit_commands)
        for h in pre_edit_commands:
            self.assertEqual(h.get("timeout"), 5, h)

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

    # --- D1 (updater workstream): version stamp ---------------------------

    def _engine_sha(self):
        """The stamp this checkout renders with, asked of the one function
        that computes it (mc_render_fingerprint) -- a test that re-derives it
        would only prove the two copies agree."""
        return subprocess.run(
            [MC_BASH, "-c",
             '. "$1"/scripts/mc-registry-lib.sh; mc_render_fingerprint repo "$1"; '
             'printf "%s" "$MC_RENDER_FINGERPRINT"',
             "_", str(TOOLS_DIR)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_every_rendered_hook_line_carries_the_stamp(self):
        """D1: MEMCONTINUUM_RENDERED=<engine short sha> on every hook line
        this installer renders -- all seven scripts (the five always-wired
        write-side hooks, plus pre-edit-chain.sh rendered twice for
        Edit/Write, plus newfile-nudge.sh -- eight command lines total for
        an install with one --code-root, this fixture's shape)."""
        data = json.loads(self.settings_path.read_text())
        commands = [
            h.get("command", "")
            for event_groups in data["hooks"].values()
            for group in event_groups
            for h in group.get("hooks", [])
            if "command" in h
        ]
        self.assertEqual(len(commands), 8, commands)
        token = f"MEMCONTINUUM_RENDERED={self._engine_sha()}"
        for cmd in commands:
            self.assertIn(token, cmd, cmd)

    # --- D4 (updater workstream): rendered rules file ----------------------

    def test_rules_file_rendered_with_store_filled(self):
        rules = self.claude_dir / "rules" / "memcontinuum.md"
        self.assertTrue(rules.is_file())
        text = rules.read_text()
        lines = text.splitlines()
        # From the template that defines it, never a copy: a second copy here
        # would have to be edited in lockstep with the template, and a test
        # comparing two copies of a string proves only that they match.
        template_marker = (TOOLS_DIR / "templates" / "memcontinuum-rules.md"
                           ).read_text().splitlines()[0]
        self.assertEqual(lines[0], template_marker)
        self.assertEqual(lines[1], f"<!-- memcontinuum-rendered: {self._engine_sha()} -->")
        self.assertIn(f"MemContinuum store ({self.store})", text)
        self.assertNotIn("{{STORE}}", text)


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

    def test_two_code_roots_readme_recipe_has_both_code_root_flags(self):
        """Task 8: memlint's --code-root is repeatable, so the store README's
        recipe line ({{CODE_ROOT_ARGS}}, templates/store-README.md.tmpl:29)
        must render one --code-root per recorded root -- a lint following
        the recipe as printed must see every root, not just the first."""
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
            readme = (Path(store) / "README.md").read_text()
            recipe = next(l for l in readme.splitlines() if "memlint.py" in l and "PYTHONPATH" in l)
            self.assertIn(f"--code-root {root_a}", recipe, recipe)
            self.assertIn(f"--code-root {root_b}", recipe, recipe)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_code_root_with_a_space_is_quoted_in_the_readme_recipe(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            root_with_space = str(Path(home) / "code root")
            os.makedirs(root_with_space, exist_ok=True)
            proc = run_install(
                ["--project", "spacey", "--store", store,
                 "--code-root", root_with_space,
                 "--claude-dir", str(Path(home) / ".claude")],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            readme = (Path(store) / "README.md").read_text()
            recipe = next(l for l in readme.splitlines() if "memlint.py" in l and "PYTHONPATH" in l)
            self.assertIn("--code-root", recipe, recipe)
            # Quoted well enough that a shell splitting the recipe line sees
            # the space-containing path as ONE word, not two -- robust to
            # either `printf %q` backslash-escaping or a quoted string.
            tokens = shlex.split(recipe)
            self.assertIn(root_with_space, tokens, recipe)
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

    def test_foreign_rules_file_refused_before_any_mutation(self):
        """D4: an existing $CLAUDE_DIR/rules/memcontinuum.md whose first
        line does not match the identity marker is a hand-authored or
        foreign file -- refused, not overwritten, and refused BEFORE any
        other mutation (the store tree, settings.local.json) so a refusal
        never leaves a half-finished install behind."""
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            claude_dir = Path(home) / ".claude"
            rules_dir = claude_dir / "rules"
            rules_dir.mkdir(parents=True)
            (rules_dir / "memcontinuum.md").write_text("# hand-written notes\nnot ours\n")
            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(claude_dir)],
                home,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("memcontinuum.md", proc.stdout + proc.stderr)
            self.assertEqual(
                (rules_dir / "memcontinuum.md").read_text(),
                "# hand-written notes\nnot ours\n",
                "foreign rules file must be left untouched",
            )
            self.assertFalse(Path(store).exists(), "no mutation at all on refusal")
            self.assertFalse((claude_dir / "settings.local.json").exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_foreign_skill_copy_refused_before_any_mutation(self):
        """Ruling 52: same principle as the rules file, for the installed
        memory-search skill copy -- an existing $CLAUDE_DIR/skills/
        memory-search/SKILL.md with no `name: memory-search` frontmatter is
        hand-authored or foreign, refused before any other mutation (the
        store tree, settings.local.json) so a refusal never leaves a
        half-finished install behind. Closes the bypass
        `--add-lang`/`--never-ext` used to have: they call repo-init.sh
        directly, never through memcontinuum-update.sh's own skill-foreign
        gate."""
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            claude_dir = Path(home) / ".claude"
            skill_dir = claude_dir / "skills" / "memory-search"
            skill_dir.mkdir(parents=True)
            foreign = "---\nname: not-memory-search\ndescription: hand-authored\n---\n\n# not ours\n"
            (skill_dir / "SKILL.md").write_text(foreign)
            proc = run_install(
                ["--project", "p", "--store", store, "--claude-dir", str(claude_dir)],
                home,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("SKILL.md", proc.stdout + proc.stderr)
            self.assertEqual(
                (skill_dir / "SKILL.md").read_text(), foreign,
                "foreign skill copy must be left untouched",
            )
            self.assertFalse(Path(store).exists(), "no mutation at all on refusal")
            self.assertFalse((claude_dir / "settings.local.json").exists())
            self.assertFalse((claude_dir / "rules" / "memcontinuum.md").exists())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_own_installed_skill_copy_is_overwritten_on_reinstall(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            claude_dir = Path(home) / ".claude"
            proc1 = run_install(["--project", "p", "--store", store, "--claude-dir", str(claude_dir)], home)
            self.assertEqual(proc1.returncode, 0, proc1.stdout + proc1.stderr)
            skill_path = claude_dir / "skills" / "memory-search" / "SKILL.md"
            self.assertTrue(skill_path.is_file())
            self.assertIn("name: memory-search", skill_path.read_text())
            proc2 = run_install(["--project", "p", "--store", store, "--claude-dir", str(claude_dir)], home)
            self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
            self.assertIn("name: memory-search", skill_path.read_text())
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_own_rendered_rules_file_is_overwritten_on_reinstall(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            claude_dir = Path(home) / ".claude"
            proc1 = run_install(["--project", "p", "--store", store, "--claude-dir", str(claude_dir)], home)
            self.assertEqual(proc1.returncode, 0, proc1.stdout + proc1.stderr)
            rules_path = claude_dir / "rules" / "memcontinuum.md"
            self.assertTrue(rules_path.is_file())
            proc2 = run_install(["--project", "p", "--store", store, "--claude-dir", str(claude_dir)], home)
            self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
            self.assertIn(f"MemContinuum store ({store})", rules_path.read_text())
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
            # Resolved, not raw: the default store sibling is built from
            # `git rev-parse --show-toplevel` (physical, symlink-free), and
            # the default --claude-dir is that same toplevel + "/.claude" --
            # both necessarily resolved. `home`/`repo` themselves are the RAW
            # tempfile.mkdtemp() form; on macOS that is /var/folders/...,
            # itself a symlink to /private/var/folders/.... Comparing raw
            # against the engine's resolved output was a Linux-only
            # assumption (Linux's /tmp is not usually symlinked, so raw
            # happened to equal resolved there).
            home_r = os.path.realpath(home)
            repo_r = os.path.realpath(str(repo))
            self.assertIn(f"defaulting to {home_r}/proj-MemContinuum-Store", proc.stdout)
            # the hooks stay with the REPO, not the parent dir the sibling
            # store happens to land in
            self.assertIn(f"{repo_r}/.claude", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_default_in_a_plain_folder_is_inside_it(self):
        home = sandbox_home()
        try:
            work = Path(home) / "docs"
            work.mkdir()
            proc = run_install(["--project", "p", "--dry-run"], home, cwd=str(work))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            # Resolved, not raw -- see the sibling-store test above: outside
            # a git repo the default store is "$(pwd -P)/MemContinuum-Store",
            # and a freshly-started bash's $PWD comes from getcwd(), which
            # is always symlink-free.
            self.assertIn(f"defaulting to {os.path.realpath(str(work))}/MemContinuum-Store", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_default_in_a_plain_folder_under_a_symlinked_logical_pwd_resolves_physically(self):
        # Ruling 89: the no-git default used to be plain "$PWD/MemContinuum-
        # Store", which trusts whatever $PWD says. A freshly spawned bash
        # normally recomputes $PWD via getcwd() (physical) regardless of
        # what the caller's environment says -- UNLESS the environment
        # already carries a PWD that stat-matches the actual cwd, in which
        # case bash keeps that string VERBATIM. That is exactly what an
        # interactive shell that `cd`-ed through a symlink leaves behind:
        # its own $PWD is the logical (symlinked) path, and a subprocess it
        # spawns inherits that same PWD, matching its own cwd by inode. The
        # fix (`pwd -P`) must resolve physically regardless. `cwd=` alone
        # would not reproduce this -- a fresh subprocess.run() with no PWD
        # override recomputes $PWD physically on its own either way; the
        # env PWD override is what actually exercises the "kept verbatim"
        # branch this test is for.
        home = sandbox_home()
        try:
            real_work = Path(home) / "docs-real"
            real_work.mkdir()
            work_link = Path(home) / "docs-link"
            work_link.symlink_to(real_work, target_is_directory=True)
            proc = run_install(
                ["--project", "p", "--dry-run"], home,
                cwd=str(work_link), extra_env={"PWD": str(work_link)},
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            work_real = os.path.realpath(str(work_link))
            self.assertNotEqual(str(work_link), work_real, "test setup must actually be symlinked")
            self.assertIn(f"defaulting to {work_real}/MemContinuum-Store", proc.stdout)
            self.assertNotIn(f"{work_link}/MemContinuum-Store", proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestSymlinkedStoreAndCodeRootResolvePhysically(unittest.TestCase):
    """Ruling 89 / symlink-paths fix: abspath() (scripts/repo-init.sh) used
    to be os.path.abspath, which never resolves symlinks. A --store or
    --code-root reached through a symlinked directory (macOS's
    /var/folders/... -> /private/var/folders/..., a symlinked $HOME, a
    mounted drive) used to bake the RAW (symlinked) form into every
    rendered hook line, the store's own post-commit wrapper, and (when
    --record-decision is given) the registry row -- while memidx.py's own
    code_meta.code_root is stored via Path(...).resolve(). abspath() now
    uses os.path.realpath, so every one of these forms must agree."""

    def test_symlinked_store_and_code_root_render_physically_everywhere(self):
        home = sandbox_home()
        try:
            repo = Path(home) / "proj"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
            subprocess.run(
                ["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                 "commit", "-q", "--allow-empty", "-m", "init"],
                cwd=repo, check=True,
            )

            real_store_parent = Path(home) / "real-store-parent"
            real_store_parent.mkdir()
            store_link = Path(home) / "store-link"
            store_link.symlink_to(real_store_parent, target_is_directory=True)
            store = str(store_link / "store")
            store_real = os.path.realpath(store)
            self.assertNotEqual(store, store_real, "test setup must actually be symlinked")

            real_code_parent = Path(home) / "real-code-parent"
            real_code_parent.mkdir()
            code_link = Path(home) / "code-link"
            code_link.symlink_to(real_code_parent, target_is_directory=True)
            code_root = str(code_link / "code")
            os.makedirs(code_root)
            code_root_real = os.path.realpath(code_root)
            self.assertNotEqual(code_root, code_root_real, "test setup must actually be symlinked")

            claude_dir = repo / ".claude"
            proc = run_install(
                ["--project", "symtest", "--store", store, "--code-root", code_root,
                 "--claude-dir", str(claude_dir), "--non-interactive", "--record-decision"],
                home,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

            # 1. rendered hook line
            data = json.loads((claude_dir / "settings.local.json").read_text())
            cmds = [
                h.get("command", "")
                for group in data["hooks"]["PostToolUse"]
                for h in group.get("hooks", [])
                if "ledger-post-edit.sh" in h.get("command", "")
            ]
            self.assertTrue(cmds, data["hooks"])
            self.assertIn(f"MEMCONTINUUM_ROOT={store_real}", cmds[0])
            self.assertIn(f"MEMCONTINUUM_CODE_ROOT={code_root_real}", cmds[0])
            self.assertNotIn(store, cmds[0])
            self.assertNotIn(code_root, cmds[0])

            # 2. the store's own post-commit reindex wrapper
            post_commit = Path(store_real) / ".git" / "hooks" / "post-commit"
            self.assertTrue(post_commit.is_file(), post_commit)
            text = post_commit.read_text()
            self.assertIn(f"MEMCONTINUUM_ROOT={store_real}", text)
            self.assertNotIn(f"MEMCONTINUUM_ROOT={store}", text)

            # 3. the registry row (--record-decision)
            decisions = Path(home) / ".memcontinuum" / "decisions.tsv"
            self.assertTrue(decisions.is_file(), "no decisions.tsv written")
            note = decisions.read_text().splitlines()[-1]
            self.assertIn(f"store={store_real}", note)
            self.assertIn(f"code-roots={code_root_real}", note)
            self.assertNotIn(f"store={store} ", note)
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

    @staticmethod
    def _tracked_entries():
        """[(mode, path)] from `git ls-files -s` -- mode included, because a
        symlink's own blob is its TARGET PATH and a content scan that follows
        the link never reads it."""
        result = subprocess.run(
            ["git", "ls-files", "-s"], cwd=str(TOOLS_DIR),
            capture_output=True, text=True, check=True,
        )
        entries = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            meta, _, rel = line.partition("\t")
            entries.append((meta.split()[0], rel))
        return entries

    @staticmethod
    def _untracked_entries():
        """[(mode, path)] for files git sees in the working tree but does not
        yet track (`git ls-files -o --exclude-standard`) -- a brand-new file
        sitting in a working tree before its own `git add`. CI only ever
        scans committed content, so without this a needle-bearing file could
        sit untracked, pass this test locally, then reach origin on a later
        `git add -A` elsewhere and only be caught by CI (or not at all, if
        that CI run is the one adding it). `--exclude-standard` means a
        gitignored file (tests/mac_smoke.local, by design) never appears
        here. Mode is always the regular-file sentinel: `git ls-files -o`
        reports no stat/type info, and an untracked symlink is out of scope
        the same way test_no_tracked_symlinks scopes the tracked case."""
        result = subprocess.run(
            ["git", "ls-files", "-o", "--exclude-standard"], cwd=str(TOOLS_DIR),
            capture_output=True, text=True, check=True,
        )
        return [("100644", rel) for rel in result.stdout.splitlines() if rel]

    @classmethod
    def _needles(cls):
        username_needle = "kra" + "kozavr"
        path_needle = "/mnt/d/!_WORK_" + "!"
        project_needles = ["mmd" + "-swift", "MMD" + "App", "MMD" + "Core", "Shot" + "Porter"]
        return [username_needle, path_needle] + project_needles

    @classmethod
    def _scan_for_needles(cls, entries):
        """The needle scan test_only_license_names_the_dev_machine performs,
        factored out so a regression test can prove it also catches an
        untracked offender."""
        needles = cls._needles()
        offenders = {}
        for mode, rel in entries:
            p = TOOLS_DIR / rel
            if mode == "120000":
                try:
                    hits = [n for n in needles if n in os.readlink(p)]
                except OSError:
                    hits = []
                if hits:
                    offenders[rel] = hits
                continue
            if not p.is_file():
                continue
            try:
                text = p.read_text(errors="ignore")
            except Exception:
                continue
            hits = [n for n in needles if n in text]
            if hits:
                offenders[rel] = hits
        return offenders

    def test_no_tracked_symlinks(self):
        """A tracked symlink is refused outright in this repo. Its blob content
        is the link target, so a link created for local convenience -- e.g. a
        worktree pointing `fixtures/records` at the main checkout -- commits an
        absolute path naming this machine's home directory. The content scan
        below cannot catch that: `Path.is_file()` follows the link to a
        directory, returns False, and skips the entry unread."""
        symlinks = [rel for mode, rel in self._tracked_entries() if mode == "120000"]
        self.assertEqual(
            symlinks, [],
            "tracked symlinks found -- their blobs are path strings, which leak "
            f"local layout: {symlinks}. Untrack them (git rm --cached).",
        )

    def test_worktree_fixture_link_is_untracked(self):
        """The private-corpus link a worktree creates must never be committed.
        Asserted separately from the blanket symlink rule so the failure names
        the actual convention when it is the one that broke."""
        link = TOOLS_DIR / "fixtures" / "records"
        if not link.exists() and not link.is_symlink():
            self.skipTest("no fixtures/records in this working tree")
        tracked = {rel for _, rel in self._tracked_entries()}
        leaked = sorted(r for r in tracked if r == "fixtures/records"
                        or r.startswith("fixtures/records/"))
        self.assertEqual(
            leaked, [],
            "fixtures/records is tracked -- it holds (or links to) a project's "
            f"real incident notes and must stay untracked: {leaked}",
        )

    def test_only_license_names_the_dev_machine(self):
        # The private codebase this engine is developed against must not be
        # named either -- not its checkout, not its module prefixes, not its
        # product name. Its symbols and probe queries describe that project's
        # internal architecture, so they live in an untracked probe file
        # (tests/test_code_index.py load_probe_set) rather than in tracked
        # content. Same split-literal trick as the two needles above (see
        # _needles()).
        #
        # Scans tracked AND untracked-but-not-gitignored entries (see
        # _untracked_entries): a file already `git add`ed is not the only way
        # a needle reaches a future commit -- a new file sitting in the
        # working tree, one `git add -A` away from being swept in, is exactly
        # as dangerous and CI alone would only catch it after the fact.
        entries = self._tracked_entries() + self._untracked_entries()
        offenders = self._scan_for_needles(entries)
        # LICENSE's copyright line is the one deliberate exception (and only
        # counts as one if/when LICENSE is actually tracked by git).
        disallowed = {rel: hits for rel, hits in offenders.items() if rel != "LICENSE"}
        self.assertEqual(
            disallowed, {},
            f"machine-identifying content found outside the allowed LICENSE exception: {disallowed}",
        )

    def test_untracked_offender_is_reported(self):
        """Regression test for the scan's untracked coverage: a file that is
        neither tracked nor gitignored -- e.g. a brand-new file before its
        own `git add` -- must still be caught. Writes a real temp file
        directly into this checkout (both `_tracked_entries` and
        `_untracked_entries` shell out to `git ls-files` with cwd=TOOLS_DIR,
        so there is no copied-repo indirection available here as there is
        for the repo-init.sh tests), and removes it in `finally` regardless
        of outcome."""
        probe = TOOLS_DIR / "tests" / "_needle_probe_untracked.txt"
        self.assertFalse(probe.exists(), "stray probe file left over from a previous run")
        try:
            probe.write_text("kra" + "kozavr", encoding="utf-8")
            entries = self._untracked_entries()
            rels = {rel for _, rel in entries}
            self.assertIn(
                "tests/_needle_probe_untracked.txt", rels,
                "git ls-files -o --exclude-standard did not report the new untracked file",
            )
            offenders = self._scan_for_needles(entries)
            self.assertIn("tests/_needle_probe_untracked.txt", offenders)
        finally:
            probe.unlink(missing_ok=True)


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

    def test_bootstrap_prefers_requirements_lock_when_present(self):
        # task-9: bootstrap_venv must install from the exact-pinned
        # requirements.lock when one sits next to requirements.txt (CI and
        # a real repo checkout both ship one), not the loose >= file.
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        fakebin = tempfile.mkdtemp(prefix="memcontinuum-fakebin-")
        try:
            install_sh = copy_engine(engine_dir)
            (Path(engine_dir) / "requirements.lock").write_text("fastembed==0.8.0\nPyYAML==6.0.3\n")

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

            invocations = record.read_text()
            self.assertIn("requirements.lock", invocations)
            self.assertNotIn("requirements.txt", invocations)
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

    def test_bootstrap_falls_back_prefers_requirements_lock_when_present(self):
        # Plain-pip-branch counterpart of
        # test_bootstrap_prefers_requirements_lock_when_present: no uv on
        # PATH forces the `python3 -m venv` + pip fallback, and that branch
        # must prefer requirements.lock over requirements.txt exactly the
        # same way the uv branch does.
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
            (Path(engine_dir) / "requirements.lock").write_text("fastembed==0.8.0\nPyYAML==6.0.3\n")
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

            # PATH with NO uv at all -- see the sibling fallback test above
            # for why this must be a minimal, uv-free PATH.
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
            self.assertIn("requirements.lock", pip_invocations)
            self.assertNotIn("requirements.txt", pip_invocations)
        finally:
            shutil.rmtree(home, ignore_errors=True)
            shutil.rmtree(engine_dir, ignore_errors=True)
            shutil.rmtree(fakebin, ignore_errors=True)

    def test_bootstrap_fallback_without_ensurepip_names_uv(self):
        # WSL's python3 -m venv fails here with no ensurepip and no sudo
        # (DESIGN-anatomy-m2-deltas.md "Verified facts") -- B4 requires the
        # fallback to fail with a clear, actionable message naming uv, not
        # a bare "venv creation failed".
        home = sandbox_home()
        engine_dir = tempfile.mkdtemp(prefix="memcontinuum-engine-copy-")
        fakebin = tempfile.mkdtemp(prefix="memcontinuum-fakebin-")
        try:
            install_sh = copy_engine(engine_dir)
            fake_python3 = Path(fakebin) / "python3"
            fake_python3.write_text(
                "#!/usr/bin/env bash\n"
                'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
                '    echo "Error: Command '"'"'/tmp/x/bin/python3 -Im ensurepip '"'"'" >&2\n'
                '    echo "ensurepip is not available" >&2\n'
                '    exit 1\n'
                "fi\n"
                "exit 0\n"
            )
            fake_python3.chmod(0o755)
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
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("uv", proc.stdout + proc.stderr)
            self.assertIn("ensurepip", (proc.stdout + proc.stderr).lower())
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
            row = conn.execute("SELECT langs FROM code_project WHERE project=?", ("widgetco",)).fetchone()
            conn.close()
            self.assertIsNotNone(row, "no code_project row for project widgetco")
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
            # Ruling 6 (fix round): language-less wiring renders the token
            # EXPLICITLY EMPTY, never omitted -- hooks/newfile-nudge.sh's
            # `${MEMCONTINUUM_LANG_EXTS-*.swift}` (no colon) only falls back
            # to the legacy *.swift default when the var is UNSET, so an
            # omitted token would (wrongly) still nudge on new .swift files
            # after an explicit skip. An empty value matches nothing.
            self.assertIn("MEMCONTINUUM_LANG_EXTS='' ", item["command"])
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
            self.assertIn(
                "MEMCONTINUUM_KNOWN_EXTS='*.cjs *.java *.js *.jsx *.lua *.mjs *.php *.py *.rs *.swift *.ts *.tsx'",
                item["command"],
            )
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


class TestSetupMenu(unittest.TestCase):
    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_non_interactive_setup_skips_the_menu(self):
        # matches the existing test_repo_init.py tty-refusal pattern
        # (test_not_a_tty_without_non_interactive_fails_clearly_not_hangs,
        # above): a non-tty run of memcontinuum-setup.sh with --python
        # explicit must never block on /dev/tty, proving the menu is
        # additive, not a new hard requirement. TOOLS_DIR here in place of
        # the brief's REPO_ROOT -- this file's own module-level constant for
        # the checkout root; there is no second name for it.
        home = sandbox_home()
        try:
            proc = subprocess.run(
                ["bash", str(TOOLS_DIR / "memcontinuum-setup.sh"),
                 "--python", VENV_PYTHON, "--no-model-warm", "--dry-run",
                 "--claude-dir", str(Path(home) / ".claude")],
                cwd=home, env={**os.environ, "HOME": home},
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestMultiRootCodeIndex(unittest.TestCase):
    """`code-reindex` is root-scoped: indexing one root touches only that
    root's own stored rows (code_meta is keyed by (project, code_root)), so
    repo-init runs one code-reindex call per --code-root and indexes all
    of them, instead of only the first."""

    @staticmethod
    def _two_roots(home):
        first = Path(home) / "code-a"
        first.mkdir(parents=True)
        shutil.copy(PY_CORPUS / "basic_functions.py", first / "alpha_module.py")
        second = Path(home) / "code-b"
        second.mkdir(parents=True)
        shutil.copy(PY_CORPUS / "basic_functions.py", second / "beta_module.py")
        return first, second

    def test_every_code_root_is_indexed(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            first, second = self._two_roots(home)
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "multi", "--store", store,
                 "--code-root", str(first), "--code-root", str(second),
                 "--claude-dir", str(claude_dir), "--langs", "python",
                 "--non-interactive"],
                home, python=VENV_PYTHON, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = proc.stdout + proc.stderr

            # One code-reindex step line per root, naming that root.
            self.assertIn(f"code-reindex ({first})", out, out)
            self.assertIn(f"code-reindex ({second})", out, out)
            self.assertNotIn("NOTE: only the first", out, out)
            # Present tense throughout: no roadmap vocabulary in anything a
            # person reads during an install.
            self.assertNotIn("milestone", out.lower(), out)

            code_db = Path(home) / ".memcontinuum" / "multi-code.sqlite"
            self.assertTrue(code_db.is_file(), out)
            conn = sqlite3.connect(str(code_db))
            try:
                roots = {
                    r[0] for r in conn.execute(
                        "SELECT code_root FROM code_meta WHERE project=?", ("multi",)
                    ).fetchall()
                }
                paths = {r[0] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            finally:
                conn.close()
            # Resolved, not raw: memidx.py stores code_root via
            # Path(args.code_root).resolve(), and `first`/`second` are the
            # raw tempfile.mkdtemp()-derived form (macOS's /var/folders/...,
            # a symlink to /private/var/folders/...). The step-line check
            # above stays raw on purpose -- that line echoes the --code-root
            # argument as typed, never resolved.
            self.assertEqual(roots, {str(first.resolve()), str(second.resolve())}, roots)
            self.assertEqual(paths, {"alpha_module.py", "beta_module.py"}, paths)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_no_removed_churn_from_a_second_root(self):
        """Each root's code-reindex call is scoped to that root alone: a
        second root's own first index must never report the first root's
        files as "removed"."""
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            first, second = self._two_roots(home)
            claude_dir = Path(home) / ".claude"
            proc = run_install_at(
                INSTALL_SH,
                ["--project", "multi", "--store", store,
                 "--code-root", str(first), "--code-root", str(second),
                 "--claude-dir", str(claude_dir), "--langs", "python",
                 "--non-interactive"],
                home, python=VENV_PYTHON, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("1 removed", proc.stdout, proc.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestCensusInvalidJSONFails(unittest.TestCase):
    """I1 (final fix wave, Grok MEDIUM): the census heredoc used to swallow
    a JSONDecodeError and carry on with an empty dict, so a broken census
    reached language-less wiring through the same side door the non-zero-rc
    check exists to close -- "nothing proposed", said in the voice of a
    successful scan. Unparseable stdout is now a failure, like rc != 0."""

    def test_garbage_census_stdout_fails_the_install(self):
        home = sandbox_home()
        try:
            store = str(Path(home) / "store")
            code_root = make_python_and_cs_corpus(Path(home) / "code")
            claude_dir = Path(home) / ".claude"
            # A python shim that corrupts ONLY the `code-census` call (the
            # heredoc itself runs under this same interpreter and must keep
            # working, so everything else execs the real venv python).
            shim = Path(home) / "bin" / "python"
            shim.parent.mkdir(parents=True, exist_ok=True)
            shim.write_text(
                "#!/usr/bin/env bash\n"
                "for a in \"$@\"; do\n"
                "  if [ \"$a\" = \"code-census\" ]; then\n"
                "    echo 'this is not json {{{'\n"
                "    exit 0\n"
                "  fi\n"
                "done\n"
                'exec "' + VENV_PYTHON + '" "$@"\n'
            )
            shim.chmod(0o755)

            proc = run_install_at(
                INSTALL_SH,
                ["--project", "p", "--store", store, "--code-root", str(code_root),
                 "--claude-dir", str(claude_dir), "--non-interactive"],
                home, python=str(shim), timeout=60,
            )
            self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
            combined = (proc.stdout + proc.stderr).lower()
            self.assertIn("json", combined, proc.stdout + proc.stderr)
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestNeverExtension(unittest.TestCase):
    """B4 (final fix wave): the dialogue's "never for one extension" answer
    used to throw away the whole proposed language set -- answering "stop
    nagging me about .cs" silently disabled python indexing too. It now
    keeps the proposed languages and excludes only the named extension,
    persisted for this wiring by rendering MEMCONTINUUM_NEVER_EXTS onto the
    nudge hook line. `--never-ext` is the non-interactive seam for the same
    variable the interactive option 4 sets."""

    def _install(self, home, extra):
        store = str(Path(home) / "store")
        code_root = make_python_and_cs_corpus(Path(home) / "code")
        claude_dir = Path(home) / ".claude"
        proc = run_install_at(
            INSTALL_SH,
            ["--project", "neverco", "--store", store, "--code-root", str(code_root),
             "--claude-dir", str(claude_dir), "--langs", "python",
             "--non-interactive"] + extra,
            home, python=VENV_PYTHON, timeout=120,
        )
        return proc, claude_dir

    def test_never_ext_keeps_the_chosen_languages_and_renders_the_env(self):
        home = sandbox_home()
        try:
            proc, claude_dir = self._install(home, ["--never-ext", ".cs"])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

            code_db = Path(home) / ".memcontinuum" / "neverco-code.sqlite"
            self.assertTrue(
                code_db.is_file(),
                "python must still be indexed -- 'never .cs' is about one extension, "
                "not about abandoning the language set\n" + proc.stdout + proc.stderr,
            )
            conn = sqlite3.connect(str(code_db))
            try:
                row = conn.execute(
                    "SELECT langs FROM code_project WHERE project=?", ("neverco",)
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row[0], "python")

            settings = json.loads((claude_dir / "settings.local.json").read_text())
            nudge = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Write"]
            command = nudge[0]["hooks"][0]["command"]
            self.assertIn("MEMCONTINUUM_NEVER_EXTS='*.cs'", command, command)
            self.assertIn("MEMCONTINUUM_LANG_EXTS='*.py'", command, command)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_never_ext_is_normalized_and_accepts_a_comma_list(self):
        home = sandbox_home()
        try:
            proc, claude_dir = self._install(home, ["--never-ext", "cs,.vb"])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            settings = json.loads((claude_dir / "settings.local.json").read_text())
            nudge = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Write"]
            command = nudge[0]["hooks"][0]["command"]
            self.assertIn("MEMCONTINUUM_NEVER_EXTS='*.cs *.vb'", command, command)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_no_never_ext_renders_the_token_empty(self):
        home = sandbox_home()
        try:
            proc, claude_dir = self._install(home, [])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            settings = json.loads((claude_dir / "settings.local.json").read_text())
            nudge = [g for g in settings["hooks"]["PreToolUse"] if g.get("matcher") == "Write"]
            command = nudge[0]["hooks"][0]["command"]
            self.assertIn("MEMCONTINUUM_NEVER_EXTS='' ", command, command)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_the_note_names_the_extension_and_does_not_overpromise(self):
        home = sandbox_home()
        try:
            proc, _claude_dir = self._install(home, ["--never-ext", ".cs"])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = proc.stdout
            self.assertIn("never=.cs", out, out)
            self.assertIn("this wiring", out.lower(), out)
        finally:
            shutil.rmtree(home, ignore_errors=True)


class TestAdoptOnly(unittest.TestCase):
    """`--adopt-only`: this install may WIRE an existing store, never CREATE
    one. The re-render path (scripts/memcontinuum-update.sh) always passes it,
    so a registry row naming a store that has been renamed or deleted can
    never make a re-render seed a fresh store at the old path.

    The refusal is pre-mutation and unconditional: no --force carve-out, and
    --dry-run refuses too (a preview of an install that must never happen is
    not useful, it is misleading)."""

    def setUp(self):
        self.home = sandbox_home()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.store = str(Path(self.home) / "store")
        self.claude_dir = str(Path(self.home) / "wt" / ".claude")

    def _install(self, args, **kw):
        return run_install(
            ["--project", "adoptonly", "--store", self.store,
             "--claude-dir", self.claude_dir, "--non-interactive"] + args,
            self.home, timeout=120, **kw)

    def _assert_nothing_created(self):
        self.assertFalse(Path(self.store).exists(),
                         "--adopt-only must not create the store directory")
        self.assertFalse(Path(self.store, ".git").exists(),
                         "--adopt-only must not git init a store")
        self.assertFalse(Path(self.claude_dir, "settings.local.json").exists(),
                         "--adopt-only refused: no wiring may be written either")

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_refuses_when_the_store_path_does_not_exist(self):
        proc = self._install(["--adopt-only"])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self._assert_nothing_created()

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_refuses_under_dry_run_too(self):
        proc = self._install(["--adopt-only", "--dry-run"])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self._assert_nothing_created()

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_refuses_a_plain_directory_that_is_not_a_git_repo(self):
        os.makedirs(self.store)
        proc = self._install(["--adopt-only"])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self.assertFalse(Path(self.store, ".git").exists())
        self.assertFalse(Path(self.claude_dir, "settings.local.json").exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_refuses_a_git_repo_without_this_tools_markers(self):
        os.makedirs(self.store)
        subprocess.run(["git", "init", "-q", "."], cwd=self.store, check=True)
        proc = self._install(["--adopt-only"])
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("store-missing", proc.stdout + proc.stderr)
        self.assertFalse(Path(self.claude_dir, "settings.local.json").exists())

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_accepts_an_existing_marked_store(self):
        first = self._install([])
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        again = self._install(["--adopt-only"])
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("adopted", again.stdout)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_help_documents_it(self):
        proc = run_install(["--help"], self.home, python=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("--adopt-only", proc.stdout)


if __name__ == "__main__":
    unittest.main()
