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
