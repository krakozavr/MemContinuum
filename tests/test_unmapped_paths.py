"""Path resolution inside `memidx.py unmapped`: one resolution per directory,
not per path, with the same answers as Path.resolve()."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memidx  # noqa: E402


class TestUnmappedResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_syscalls_bounded_by_directories_not_paths(self):
        paths = []
        for d in ("a", "b/c", "b/d"):
            (self.root / d).mkdir(parents=True)
            for i in range(17):
                f = self.root / d / f"f{i}.txt"
                f.write_text("x")
                paths.append(str(f))
        depth = len(self.root.parts) + 2
        real_lstat = os.lstat
        calls = []

        def counting(p, *a, **k):
            calls.append(p)
            return real_lstat(p, *a, **k)

        with mock.patch("os.lstat", counting):
            resolver = memidx._UnmappedResolver(paths)
            out = [resolver.resolve(p) for p in paths]
        self.assertEqual(out, [Path(p).resolve() for p in paths])
        # 3 directories: each ancestor is lstat-ed once, files are answered
        # from one listing per directory -- never files x depth.
        self.assertLessEqual(len(calls), 3 * depth)
        self.assertLess(len(calls), len(paths))

    def test_symlinked_file_and_directory_resolve_like_path_resolve(self):
        (self.root / "real").mkdir()
        (self.root / "real" / "x.py").write_text("x")
        (self.root / "real" / "y.py").write_text("y")
        os.symlink(self.root / "real", self.root / "linkdir")
        os.symlink(self.root / "real" / "x.py", self.root / "linkfile.py")
        paths = [
            str(self.root / "linkdir" / "x.py"),
            str(self.root / "linkdir" / "y.py"),
            str(self.root / "linkfile.py"),
            str(self.root / "real" / "x.py"),
            str(self.root / "real" / "gone.py"),
            str(self.root / "missingdir" / "z.py"),
            str(self.root / "real" / ".." / "real" / "x.py"),
        ]
        for resolver in (memidx._UnmappedResolver(paths), memidx._UnmappedResolver()):
            for p in paths:
                self.assertEqual(resolver.resolve(p), Path(p).resolve(), p)
        resolver = memidx._UnmappedResolver(paths)
        self.assertEqual(resolver.resolve(paths[0]), self.root / "real" / "x.py")
        self.assertEqual(resolver.resolve(paths[2]), self.root / "real" / "x.py")
        # And the root choice is unchanged through the public helpers.
        roots = [self.root / "real"]
        self.assertEqual(memidx._unmapped_path_candidates(paths[0], roots, resolver), [paths[0], "x.py"])
        self.assertEqual(memidx._unmapped_display_path(paths[2], roots, resolver), "x.py")

    def test_a_listing_miss_falls_back_to_lstat(self):
        """A decomposed (NFD) name in the directory listing against a composed
        (NFC) query: the listing does not contain the queried spelling, but
        the filesystem (APFS, drvfs) resolves it. A miss must ask lstat, not
        conclude "not a symlink"."""
        (self.root / "real").mkdir()
        (self.root / "real" / "target.py").write_text("x")
        composed = "caf\u00e9.py"
        decomposed = "cafe\u0301.py"
        os.symlink(self.root / "real" / "target.py", self.root / composed)
        other = self.root / "other.py"
        other.write_text("y")
        query = str(self.root / composed)
        resolver = memidx._UnmappedResolver([query, str(other)])
        # A fake listing that spells the symlink the other way.
        resolver._listings[str(self.root)] = {decomposed: True, "other.py": False}
        self.assertEqual(resolver.resolve(query), self.root / "real" / "target.py")
        self.assertEqual(resolver.resolve(str(other)), other)

    def test_an_exact_listing_hit_needs_no_lstat_for_the_file(self):
        (self.root / "d").mkdir()
        paths = []
        for i in range(3):
            f = self.root / "d" / f"f{i}.txt"
            f.write_text("x")
            paths.append(str(f))
        real_lstat = os.lstat
        calls = []

        def counting(p, *a, **k):
            calls.append(str(p))
            return real_lstat(p, *a, **k)

        resolver = memidx._UnmappedResolver(paths)
        with mock.patch("os.lstat", counting):
            for p in paths:
                resolver.resolve(p)
        self.assertEqual([c for c in calls if c.startswith(str(self.root / "d" / "f"))], [])

    def test_a_deleted_file_in_a_listed_directory_is_still_a_plain_path(self):
        (self.root / "d").mkdir()
        for i in range(2):
            (self.root / "d" / f"f{i}.txt").write_text("x")
        gone = str(self.root / "d" / "gone.txt")
        paths = [str(self.root / "d" / "f0.txt"), str(self.root / "d" / "f1.txt"), gone]
        resolver = memidx._UnmappedResolver(paths)
        self.assertEqual(resolver.resolve(gone), Path(gone).resolve())

    def test_a_leading_double_slash_delegates_to_path_resolve(self):
        (self.root / "real").mkdir()
        (self.root / "real" / "x.py").write_text("x")
        os.symlink(self.root / "real", self.root / "linkdir")
        raw = "/" + str(self.root / "linkdir" / "x.py")
        self.assertTrue(raw.startswith("//"))
        for resolver in (memidx._UnmappedResolver([raw]), memidx._UnmappedResolver()):
            self.assertEqual(resolver.resolve(raw), Path(raw).resolve())

    def test_a_symlinked_root_matches_the_resolved_root(self):
        """macOS /tmp -> /private/tmp: the configured root is spelled through
        a symlink, the resolved root is what the resolved path is compared to."""
        (self.root / "private" / "code" / "src").mkdir(parents=True)
        (self.root / "private" / "code" / "src" / "a.py").write_text("x")
        os.symlink(self.root / "private", self.root / "tmp")
        spelled = self.root / "tmp" / "code"
        roots = [spelled.resolve()]
        raw = str(spelled / "src" / "a.py")
        resolver = memidx._UnmappedResolver([raw])
        self.assertEqual(memidx._unmapped_path_candidates(raw, roots, resolver), [raw, "src/a.py"])
        self.assertEqual(memidx._unmapped_display_path(raw, roots, resolver), "src/a.py")
        self.assertEqual(
            memidx._unmapped_path_candidates(raw, roots, resolver),
            memidx._unmapped_path_candidates(raw, roots, None),
        )


    def test_a_missing_file_is_lstatted_once_by_the_command(self):
        """Gate round 2 R2-4: candidates and display come from ONE resolution
        of the raw path, so a listing miss pays one final-component lstat."""
        import contextlib
        import io
        from tests.test_write_hooks import TOPIC_MD, _write, reindex
        code = self.root / "code"
        (code / "src").mkdir(parents=True)
        (code / "src" / "here.py").write_text("x")
        missing = str(code / "src" / "missing.py")
        store, db = self.root / "store", self.root / "zp.sqlite"
        _write(store / "topics" / "testing" / "mapped-topic.md", TOPIC_MD)
        reindex(store, db, project="zp")
        real_lstat = os.lstat
        calls = []

        def counting(p, *a, **k):
            calls.append(str(p))
            return real_lstat(p, *a, **k)

        with mock.patch("os.lstat", counting), contextlib.redirect_stdout(io.StringIO()):
            memidx.main(["unmapped", missing, str(code / "src" / "here.py"), "--root", str(store),
                         "--project", "zp", "--db", str(db), "--json", "--code-root", str(code)])
        self.assertEqual([c for c in calls if c == missing], [missing], calls)


class TestUnmappedGitIgnored(unittest.TestCase):
    """TOP-0137 L29: a path the code root's git ignores is `ignored`, not a
    coverage gap -- the filter lives in `unmapped`, never in the ledger."""

    def setUp(self):
        from tests.test_write_hooks import build_store_root, git_init, reindex
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.store, self.db = self.base / "store", self.base / "zp.sqlite"
        build_store_root(self.store)
        reindex(self.store, self.db, project="zp")
        self.git_init = git_init

    def _code_root(self, name="code", git=True):
        code = self.base / name
        (code / "scratch").mkdir(parents=True)
        (code / "src").mkdir()
        (code / ".gitignore").write_text("scratch/\n")
        (code / "scratch" / "a.py").write_text("x")
        (code / "src" / "nothing.py").write_text("x")
        if git:
            self.git_init(code)
        return code

    def _run(self, code, rel_paths, json_out=True, extra_paths=()):
        import contextlib
        import io
        import json
        paths = [str(code / r) for r in rel_paths] + list(extra_paths)
        argv = ["unmapped", *paths, "--root", str(self.store), "--project", "zp",
                "--db", str(self.db), "--code-root", str(code)]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            memidx.main(argv + (["--json"] if json_out else []))
        return buf.getvalue() if not json_out else json.loads(buf.getvalue()), paths

    def test_a_gitignored_path_is_ignored_not_unmapped(self):
        code = self._code_root()
        out, paths = self._run(code, ["scratch/a.py", "src/nothing.py", "scratch/never-written.py"])
        self.assertEqual(out["ignored"], ["scratch/a.py", "scratch/never-written.py"])
        self.assertEqual(out["unmapped"], ["src/nothing.py"])
        self.assertEqual(out["by_path"], {paths[0]: "ignored", paths[1]: "unmapped", paths[2]: "ignored"})
        text, _ = self._run(code, ["scratch/a.py", "src/nothing.py"], json_out=False)
        self.assertIn("unmapped: 1\n  src/nothing.py\n", text)
        self.assertIn("ignored: 1\n  scratch/a.py\n", text)

    def test_the_same_path_in_a_non_git_root_is_unmapped(self):
        code = self._code_root(git=False)
        out, _ = self._run(code, ["scratch/a.py"])
        self.assertEqual((out["ignored"], out["unmapped"]), ([], ["scratch/a.py"]))

    def test_one_git_call_per_root(self):
        code = self._code_root()
        real_run = memidx.subprocess.run
        calls = []

        def spy(cmd, *a, **k):
            if "check-ignore" in cmd:
                calls.append(cmd)
            return real_run(cmd, *a, **k)

        with mock.patch.object(memidx.subprocess, "run", spy):
            self._run(code, ["scratch/a.py", "src/nothing.py", "scratch/b.py"])
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("--no-optional-locks", calls[0])
        self.assertNotIn("--no-index", calls[0])

    def test_a_failing_git_leaves_the_old_verdict(self):
        code = self._code_root()
        stub_dir = self.base / "stub-bin"
        stub_dir.mkdir()
        stub = stub_dir / "git"
        stub.write_text("#!/bin/sh\nexit 3\n")
        stub.chmod(0o755)
        with mock.patch.dict(os.environ, {"PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"}):
            out, _ = self._run(code, ["scratch/a.py"])
        self.assertEqual((out["ignored"], out["unmapped"]), ([], ["scratch/a.py"]))

    def test_a_missing_git_leaves_the_old_verdict(self):
        code = self._code_root()
        with mock.patch.object(memidx.subprocess, "run", side_effect=FileNotFoundError("git")):
            out, _ = self._run(code, ["scratch/a.py"])
        self.assertEqual((out["ignored"], out["unmapped"]), ([], ["scratch/a.py"]))

    def test_a_tracked_file_matching_an_ignore_pattern_stays_unmapped(self):
        import subprocess
        code = self._code_root()
        subprocess.run(["git", "add", "-f", "scratch/a.py"], cwd=code, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "track"], cwd=code, check=True)
        out, _ = self._run(code, ["scratch/a.py", "scratch/b.py"])
        self.assertEqual(out["unmapped"], ["scratch/a.py"])
        self.assertEqual(out["ignored"], ["scratch/b.py"])

    def test_a_store_path_is_never_checked(self):
        code = self._code_root()
        (self.store / ".gitignore").write_text("topics/\n")
        store_file = str(self.store / "topics" / "testing" / "mapped-topic.md")
        out, _ = self._run(code, [], extra_paths=[store_file])
        self.assertEqual(out["ignored"], [])
        self.assertEqual(out["by_path"].get(store_file) != "ignored", True)

    def _slow_git(self, seconds, log=None):
        """A `git` on PATH that sleeps in check-ignore only (and logs each
        such call), delegating everything else to the real git."""
        import shutil
        stub_dir = self.base / "slow-bin"
        stub_dir.mkdir(exist_ok=True)
        stub = stub_dir / "git"
        stub.write_text(
            "#!/bin/sh\n"
            'case " $* " in *" check-ignore "*)\n'
            + (f'  echo "$*" >> "{log}"\n' if log else "")
            + f"  sleep {seconds};;\nesac\n"
            f'exec "{shutil.which("git")}" "$@"\n'
        )
        stub.chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}"})

    def test_a_slow_git_fails_open_within_the_ignore_check_budget(self):
        import time
        code = self._code_root()
        with self._slow_git(3), mock.patch.dict(os.environ, {"MEMCONTINUUM_IGNORE_CHECK_BUDGET": "0.2"}):
            t0 = time.monotonic()
            out, paths = self._run(code, ["scratch/a.py", "src/nothing.py"])
            took = time.monotonic() - t0
        self.assertLess(took, 2.5, took)
        self.assertEqual(out["ignored"], [])
        self.assertEqual(out["by_path"], {paths[0]: "unmapped", paths[1]: "unmapped"})
        self.assertEqual(out["ignore_check"], "timeout")

    def test_the_first_root_eating_the_budget_skips_the_second(self):
        log = self.base / "check-ignore-calls.log"
        first, second = self._code_root("code1"), self._code_root("code2")
        import contextlib
        import io
        import json
        paths = [str(first / "scratch" / "a.py"), str(second / "scratch" / "a.py")]
        argv = ["unmapped", *paths, "--root", str(self.store), "--project", "zp", "--db", str(self.db),
                "--code-root", str(first), "--code-root", str(second), "--json"]
        buf = io.StringIO()
        with self._slow_git(3, log), mock.patch.dict(os.environ, {"MEMCONTINUUM_IGNORE_CHECK_BUDGET": "0.2"}):
            with contextlib.redirect_stdout(buf):
                memidx.main(argv)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["by_path"], {paths[0]: "unmapped", paths[1]: "unmapped"})
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertEqual(out["ignore_check"], "timeout")

    def test_a_root_left_when_the_deadline_is_spent_is_skipped(self):
        from types import SimpleNamespace
        a, b = self._code_root("ra"), self._code_root("rb")
        ticks = iter([0.0, 0.4, 5.0])  # deadline set, first root has time, second has none
        fake = SimpleNamespace(monotonic=lambda: next(ticks))
        with mock.patch.object(memidx, "time", fake), \
                mock.patch.dict(os.environ, {"MEMCONTINUUM_IGNORE_CHECK_BUDGET": "1"}):
            out, status = memidx._unmapped_git_ignored(
                [("A", a, "scratch/a.py"), ("B", b, "scratch/a.py")])
        self.assertEqual((out, status), ({"A"}, "skipped"))

    def test_a_clean_run_reports_complete(self):
        code = self._code_root()
        out, _ = self._run(code, ["scratch/a.py"])
        self.assertEqual(out["ignore_check"], "complete")

    def test_the_budget_is_explicit_else_a_quarter_of_the_hook_budget_else_one_second(self):
        keys = ("MEMCONTINUUM_IGNORE_CHECK_BUDGET", "MC_WATCHDOG_EFFECTIVE_BUDGET")
        base = {k: v for k, v in os.environ.items() if k not in keys}
        for env, want in (({}, 1.0),
                          ({"MC_WATCHDOG_EFFECTIVE_BUDGET": "4.0"}, 1.0),
                          ({"MC_WATCHDOG_EFFECTIVE_BUDGET": "1.5"}, 0.375),
                          ({"MC_WATCHDOG_EFFECTIVE_BUDGET": "1.5", "MEMCONTINUUM_IGNORE_CHECK_BUDGET": "0.2"}, 0.2),
                          ({"MEMCONTINUUM_IGNORE_CHECK_BUDGET": "junk"}, 1.0)):
            with mock.patch.dict(os.environ, {**base, **env}, clear=True):
                self.assertEqual(memidx._ignore_check_budget(), want, env)

    def _nested(self, store_rel):
        """A code root that is a git repo ignoring `memory/`, holding the store."""
        from tests.test_write_hooks import build_store_root, reindex
        code = self.base / "repo"
        code.mkdir()
        (code / ".gitignore").write_text("memory/\nscratch/\nnotes-*.md\n")
        store = code / store_rel if store_rel else code
        build_store_root(store)
        db = self.base / "nested.sqlite"
        reindex(store, db, project="zp")
        self.git_init(code)
        return code, store, db

    def _run_nested(self, code, store, db, paths):
        import contextlib
        import io
        import json
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            memidx.main(["unmapped", *paths, "--root", str(store), "--project", "zp", "--db", str(db),
                         "--code-root", str(code), "--json"])
        return json.loads(buf.getvalue())

    def test_a_store_nested_in_a_code_root_is_never_ignore_checked(self):
        code, store, db = self._nested("memory")
        (code / "scratch").mkdir()
        inside = str(store / "topics" / "testing" / "mapped-topic.md")
        extra = str(store / "notes-not-a-topic.md")
        scratch = str(code / "scratch" / "a.py")
        out = self._run_nested(code, store, db, [inside, extra, scratch])
        self.assertNotEqual(out["by_path"][extra], "ignored")
        self.assertNotEqual(out["by_path"][inside], "ignored")
        self.assertEqual(out["by_path"][scratch], "ignored")

    def test_a_store_that_is_the_code_root_is_never_ignore_checked(self):
        code, store, db = self._nested("")
        (code / "memory").mkdir(exist_ok=True)
        extra = str(store / "notes-not-a-topic.md")
        out = self._run_nested(code, store, db, [extra])
        self.assertEqual(out["by_path"][extra], "unmapped")


class TestUnmappedZeroPaths(unittest.TestCase):
    """`unmapped` with no PATH still runs the index-state check and the stale
    self-heal, and reports whether the heal ran (`healed`)."""

    def _run(self, store, db):
        import contextlib
        import io
        import json
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = memidx.main(["unmapped", "--root", str(store), "--project", "zp",
                              "--db", str(db), "--json"])
        return rc, json.loads(buf.getvalue())

    def test_zero_paths_report_healed_only_when_the_heal_ran(self):
        from tests.test_write_hooks import TOPIC_MD, _write, reindex
        with tempfile.TemporaryDirectory() as td:
            store, db = Path(td) / "store", Path(td) / "zp.sqlite"
            _write(store / "topics" / "testing" / "mapped-topic.md", TOPIC_MD)
            rc, out = self._run(store, db)  # no index yet
            self.assertEqual(rc, 1)
            self.assertEqual(out["coverage_status"], "uninitialized")
            self.assertIs(out["healed"], False)
            self.assertEqual(out["by_path"], {})
            reindex(store, db, project="zp")
            rc, out = self._run(store, db)
            self.assertEqual((rc, out["coverage_status"], out["healed"]), (0, "ok", False))
            _write(store / "topics" / "testing" / "second.md",
                   TOPIC_MD.replace("TOP-9001", "TOP-9002").replace("src/mapped.py", "src/second.py"))
            rc, out = self._run(store, db)
            self.assertEqual((rc, out["coverage_status"], out["healed"]), (0, "ok", True))
            rc, out = self._run(store, db)
            self.assertIs(out["healed"], False)


if __name__ == "__main__":
    unittest.main()
