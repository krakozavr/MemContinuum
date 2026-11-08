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


if __name__ == "__main__":
    unittest.main()
