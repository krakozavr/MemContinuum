"""TOP-0137 L15 (e): the one hook.log line Python itself writes -- a
`code-search` call -- and `memidx stats` counting it as its own kind. Every
fixture lives in a tmp dir; MEMCONTINUUM_HOME points there."""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402

LINE_RE = re.compile(
    r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d code-search "
    r"outcome=(ok|no-hits|error) hits=\d+ q=[A-Za-z0-9._-]{1,40} project=demo$"
)


def ns(**kw):
    base = dict(project="demo", db=None, mode="fts", limit=5, json=True,
                decision_db=None, no_heal=True, query="alpha")
    base.update(kw)
    return SimpleNamespace(**base)


class CodeSearchLogTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.home = Path(self.td.name) / "home"
        self.home.mkdir()
        self.root = Path(self.td.name) / "code"
        self.root.mkdir()
        (self.root / "a.py").write_text("def alpha():\n    return 1\n")
        env = mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        rc = memidx.cmd_code_reindex(SimpleNamespace(
            code_root=str(self.root), db=None, project="demo", no_embed=True,
            full=False, lang="python", drop_root=None))
        self.assertEqual(rc, 0)
        self.log = self.home / "hook.log"

    def search(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memidx.cmd_code_search(ns(**kw))
        return rc, out.getvalue()

    def lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_appends_exactly_one_line_of_the_shape(self):
        rc, out = self.search()
        self.assertEqual(rc, 0)
        lines = self.lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], LINE_RE)
        self.assertIn("outcome=ok hits=1 q=alpha project=demo", lines[0])

    def test_no_hits_and_hostile_query_token(self):
        self.search(query="zzzqqq nothing=here project=other " + "x" * 80)
        line = self.lines()[0]
        self.assertRegex(line, LINE_RE)
        self.assertIn("outcome=no-hits hits=0", line)
        self.assertEqual(line.count("project="), 1)

    def test_uninitialized_project_logs_error(self):
        rc, _ = self.search(project="other")
        self.assertEqual(rc, 1)
        self.assertIn("outcome=error hits=0", self.lines()[0])

    def test_stats_counts_it_as_its_own_kind(self):
        self.search()
        self.search(query="zzzqqq")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = memidx.cmd_stats(SimpleNamespace(
                project="demo", days=1, home=str(self.home), store=None,
                json=True, now=None))
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data["code_search"]["ok"], 1)
        self.assertEqual(data["code_search"]["no_hits"], 1)
        self.assertEqual(data["unknown_lines"], 0)

    def test_unwritable_log_changes_neither_exit_code_nor_stdout(self):
        rc1, out1 = self.search()
        self.log.unlink()
        self.log.mkdir()          # appending to a directory raises IsADirectoryError
        rc2, out2 = self.search()
        self.assertEqual((rc1, out1), (rc2, out2))
        self.assertTrue(self.log.is_dir())

    def test_empty_query_logs_dash_not_unknown(self):
        self.search(query="")
        self.search(query="!!! ???")
        for line in self.lines():
            self.assertRegex(line, r" q=- project=demo$")
            self.assertNotIn("q=unknown", line)

    def test_db_elsewhere_still_logs_to_home_and_stats_counts_it(self):
        other = Path(self.td.name) / "elsewhere"
        other.mkdir()
        db = other / "x.sqlite"
        rc = memidx.cmd_code_reindex(SimpleNamespace(
            code_root=str(self.root), db=str(db), project="demo", no_embed=True,
            full=False, lang="python", drop_root=None))
        self.assertEqual(rc, 0)
        self.search(db=str(db))
        self.assertFalse((other / "hook.log").exists())
        self.assertEqual(len(self.lines()), 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            memidx.cmd_stats(SimpleNamespace(
                project="demo", days=1, home=str(self.home), store=None,
                json=True, now=None))
        self.assertEqual(json.loads(out.getvalue())["code_search"]["ok"], 1)

    def test_appender_never_creates_a_missing_directory(self):
        missing = self.home / "nope"
        memidx.append_hook_log_line("code-search", {"outcome": "ok"}, log_dir=missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
