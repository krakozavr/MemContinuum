"""0.5.0 PR 3 -- frontmatter under the guard: the `standing_marks:` block, its
plain-mode lint rules, the `mark-standing` command, and the store pre-commit
guard (memlint --against-ref). Every fixture builds its own throwaway store."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
MC_BASH = os.environ.get("MC_BASH", "bash")
PRE_COMMIT_HOOK = TOOLS_DIR / "hooks" / "pre-commit-append-only.sh"

NOW = datetime(2026, 11, 3, 9, 12, 41, tzinfo=timezone(timedelta(hours=-5)))
STAMP = "2026-11-03T09:12:41-05:00"


def link_yaml(lid, status="active", auth="owner-ratified", text="a ruling", extra=""):
    return (
        f"  - link: {lid}\n"
        "    date: '2024-01-01'\n"
        f"    status: {status}\n"
        "    kind: adopted\n"
        f"{extra}"
        "    ruling:\n"
        f"      text: \"{text}\"\n"
        f"      authority: {auth}\n"
        "      source: s\n"
    )


# newest first: L4 provisional-free active head, L3 active, L2 active (owner), L1 historical
DEFAULT_LINKS = (
    link_yaml("L4", text="fourth"),
    link_yaml("L3", text="third"),
    link_yaml("L2", text="second"),
    link_yaml("L1", status="historical", text="first"),
)


def mark_yaml(link="L3", action="add", recorded_at=STAMP, date=None, by="tester",
              authority="owner-ratified", source="owner said so", why="because", nl="\n"):
    date = date or recorded_at[:10]
    return (
        f"  - link: {link}{nl}"
        f"    action: {action}{nl}"
        f"    date: {date}{nl}"
        f"    recorded_at: \"{recorded_at}\"{nl}"
        f"    recorded_by: {by}{nl}"
        f"    authority: {authority}{nl}"
        f"    source: \"{source}\"{nl}"
        f"    why: \"{why}\"{nl}"
    )


def topic_text(standing="standing: [L2]\n", marks="", links=DEFAULT_LINKS, current="L4",
               tid="TOP-9100", extra_fm=""):
    cur = f"current: {current}\n" if current else ""
    return (
        "---\n"
        "type: topic\n"
        f"id: {tid}\n"
        "title: Marks fixture\n"
        "area: memory\n"
        f"{cur}"
        "tags: []\n"
        f"{standing}"
        f"{marks}"
        f"{extra_fm}"
        "links:\n"
        f"{''.join(links)}"
        "---\n\nBody.\n"
    )


def marks_block(*rows):
    return "standing_marks:\n" + "".join(rows)


def git(args, cwd):
    return subprocess.run(["git"] + list(args), cwd=str(cwd), capture_output=True, text=True, check=True)


def git_store(td):
    root = Path(td)
    root.mkdir(parents=True, exist_ok=True)
    git(["init", "-q", str(root)], root)
    git(["config", "user.email", "t@example.com"], root)
    git(["config", "user.name", "T"], root)
    (root / "topics").mkdir()
    return root


def commit_all(root, msg="base"):
    git(["add", "-A"], root)
    git(["commit", "-q", "-m", msg], root)


def run_memlint(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        rc = memlint.main(args)
    return rc, buf.getvalue()


def run_memidx(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = memidx.main(args)
    return rc, out.getvalue(), err.getvalue()


def lint_text(text):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "topics").mkdir()
        (root / "topics" / "foo.md").write_text(text)
        return memlint.lint_root(root)


class SymlinkedTmpMixin:
    """Every inherited test runs with the fixture under a symlinked temp dir
    (macOS /var -> /private/var, INC-0132 family)."""

    SYMLINKED_TMP = True

    def test_fixture_really_is_behind_a_symlink(self):
        self.assertNotEqual(os.path.realpath(self.tmp), str(self.tmp))


class TmpCase(unittest.TestCase):
    SYMLINKED_TMP = False

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.tmp = Path(self._td.name)
        if self.SYMLINKED_TMP:
            (self.tmp / "real").mkdir()
            os.symlink("real", self.tmp / "link")
            self.tmp = self.tmp / "link"


class ErrCase(unittest.TestCase):
    def assertError(self, errors, *needles):
        self.assertTrue(any(all(n in e for n in needles) for e in errors), f"{needles} not in {errors}")


# ---------------------------------------------------------------------------
# plain-mode lint
# ---------------------------------------------------------------------------

class TestMarksPlainLint(ErrCase):
    def lint(self, **kw):
        return lint_text(topic_text(**kw))

    def test_valid_rows_pass(self):
        errors, warnings = self.lint(standing="standing: [L2, L3]\n",
                                     marks=marks_block(mark_yaml("L3"), mark_yaml("L2", recorded_at="2026-11-02T08:00:00-05:00")))
        self.assertEqual(errors, [], errors)

    def test_grandfathered_topic_without_marks_is_clean(self):
        errors, warnings = self.lint(standing="standing: [L2, L3]\n")
        self.assertEqual(errors, [], errors)
        self.assertFalse([w for w in warnings if "standing_marks" in w], warnings)

    def test_not_a_list_of_mappings(self):
        errors, _ = lint_text(topic_text(marks="standing_marks:\n  - just-a-string\n"))
        self.assertError(errors, "standing_marks must be a list of mappings")

    def test_missing_field(self):
        row = mark_yaml().replace("    why: \"because\"\n", "")
        errors, _ = self.lint(marks=marks_block(row))
        self.assertError(errors, "standing_marks[0]", "missing field(s)", "why")

    def test_unknown_field(self):
        errors, _ = self.lint(marks=marks_block(mark_yaml() + "    note: hi\n"))
        self.assertError(errors, "unknown field(s)", "note")

    def test_bad_action(self):
        errors, _ = self.lint(marks=marks_block(mark_yaml(action="toggle")))
        self.assertError(errors, "action 'toggle'")

    def test_authority_must_be_owner(self):
        for bad in ("agent-inference", "code-derived", "reviewer-finding"):
            with self.subTest(authority=bad):
                errors, _ = self.lint(marks=marks_block(mark_yaml(authority=bad)))
                self.assertError(errors, "authority", bad)

    def test_empty_source_and_why(self):
        errors, _ = self.lint(marks=marks_block(mark_yaml(source="", why="")))
        self.assertError(errors, "missing field(s)", "source")
        self.assertError(errors, "missing field(s)", "why")

    def test_why_with_newline(self):
        # a double-quoted YAML scalar carrying an escaped newline
        errors, _ = self.lint(marks=marks_block(mark_yaml(why="two\\nlines")))
        self.assertError(errors, "why must be a single line")

    def test_recorded_at_needs_offset_and_seconds_format(self):
        for bad in ("2026-11-03T09:12:41", "2026-11-03", "yesterday", "2026-13-03T09:12:41-05:00"):
            with self.subTest(recorded_at=bad):
                errors, _ = self.lint(marks=marks_block(mark_yaml(recorded_at=bad, date="2026-11-03")))
                self.assertError(errors, "recorded_at")

    def test_date_must_be_date_part_as_written(self):
        errors, _ = self.lint(marks=marks_block(mark_yaml(date="2026-11-04")))
        self.assertError(errors, "date '2026-11-04' is not the date part")
        # the row's own offset: 2026-11-03T23:30-05:00 is 11-03 even though UTC is 11-04
        ok, _ = self.lint(marks=marks_block(mark_yaml(recorded_at="2026-11-03T23:30:00-05:00")))
        self.assertEqual(ok, [], ok)

    def test_link_must_belong_to_this_topic(self):
        errors, _ = self.lint(marks=marks_block(mark_yaml(link="L9")))
        self.assertError(errors, "link 'L9' is not a link in this topic")

    def test_superseded_link_row_stays_valid(self):
        links = (link_yaml("L2", status="superseded", extra="    superseded_by: L3\n"),
                 link_yaml("L3"))
        links = (link_yaml("L3"), link_yaml("L2", status="superseded", extra="    superseded_by: L3\n"))
        errors, _ = self.lint(standing="standing: [L3]\n", links=links, current="L3",
                              marks=marks_block(mark_yaml("L3"), mark_yaml("L2", action="remove", recorded_at="2026-11-02T08:00:00-05:00")))
        self.assertEqual(errors, [], errors)

    def test_duplicate_identity(self):
        errors, _ = self.lint(marks=marks_block(mark_yaml(), mark_yaml(why="again")))
        self.assertError(errors, "duplicate row")

    def test_same_link_action_at_a_different_time_is_not_a_duplicate(self):
        errors, _ = self.lint(marks=marks_block(mark_yaml(recorded_at="2026-11-03T09:12:41-05:00"),
                                                mark_yaml(recorded_at="2026-11-03T09:12:42-05:00")))
        self.assertEqual(errors, [], errors)

    def test_duplicate_ids_in_standing(self):
        errors, _ = self.lint(standing="standing: [L2, L2]\n")
        self.assertError(errors, "standing lists 'L2' more than once")


# ---------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------

class MarkCase(TmpCase):
    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        (self.store / "topics").mkdir(parents=True)
        self.path = self.store / "topics" / "foo.md"
        patcher = mock.patch.object(memidx, "_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("MEMCONTINUUM_RECORDED_BY", None)

    def write(self, text):
        self.path.write_bytes(text.encode("utf-8"))
        return text

    def read(self):
        return self.path.read_bytes().decode("utf-8")

    def mark(self, link, *flags, authority="owner-ratified", source="owner said so",
             why="because", topic="TOP-9100", extra=()):
        return run_memidx(["mark-standing", topic, link, *flags, "--authority", authority,
                           "--source", source, "--why", why, "--root", str(self.store), *extra])

    def parsed(self):
        return memidx.parse_record_raw(self.read(), self.path).frontmatter

    def assertLintClean(self):
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)


class TestMarkStandingCommand(MarkCase):
    def test_add_to_flow_list(self):
        self.write(topic_text(standing="standing: [L2]\n"))
        rc, out, err = self.mark("L3", "--add")
        self.assertEqual(rc, 0, err)
        text = self.read()
        self.assertIn("standing: [L2, L3]\n", text)
        fm = self.parsed()
        self.assertEqual(fm["standing"], ["L2", "L3"])
        rows = memidx.standing_mark_rows(fm)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["link"], rows[0]["action"], rows[0]["recorded_at"]), ("L3", "add", STAMP))
        self.assertEqual(rows[0]["recorded_by"], "memidx")
        self.assertEqual(str(rows[0]["date"]), "2026-11-03")
        self.assertIn("+  - link: L3", out)
        self.assertLintClean()

    def test_marks_key_sits_right_after_standing_and_before_links(self):
        self.write(topic_text(standing="standing: [L2]\n"))
        self.mark("L3", "--add")
        text = self.read()
        self.assertLess(text.index("standing: [L2, L3]"), text.index("standing_marks:"))
        self.assertLess(text.index("standing_marks:"), text.index("\nlinks:"))
        self.assertEqual(text.splitlines()[text.splitlines().index("standing: [L2, L3]") + 1], "standing_marks:")

    def test_add_to_block_list(self):
        self.write(topic_text(standing="standing:\n  - L2\n"))
        rc, _out, err = self.mark("L3", "--add")
        self.assertEqual(rc, 0, err)
        text = self.read()
        self.assertIn("standing:\n  - L2\n  - L3\nstanding_marks:\n", text)
        self.assertEqual(self.parsed()["standing"], ["L2", "L3"])
        self.assertLintClean()

    def test_first_mark_creates_standing_key(self):
        self.write(topic_text(standing=""))
        rc, _out, err = self.mark("L2", "--add")
        self.assertEqual(rc, 0, err)
        text = self.read()
        self.assertIn("tags: []\nstanding: [L2]\nstanding_marks:\n", text)
        self.assertLintClean()

    def test_standing_created_before_existing_anchors_when_no_marks(self):
        self.write(topic_text(standing="", extra_fm=""))
        text = self.read().replace("links:\n", "code_refs:\n  - src/a.py\nanchors: []\nlinks:\n", 1)
        self.write(text)
        rc, _o, err = self.mark("L2", "--add")
        self.assertEqual(rc, 0, err)
        out = self.read()
        self.assertLess(out.index("standing: [L2]"), out.index("standing_marks:"))
        self.assertLess(out.index("standing_marks:"), out.index("anchors:"))

    def test_second_mark_is_prepended(self):
        self.write(topic_text(standing="standing: [L2]\n"))
        self.mark("L3", "--add")
        with mock.patch.object(memidx, "_now", return_value=NOW + timedelta(minutes=5)):
            rc, _o, err = self.mark("L4", "--add")
        self.assertEqual(rc, 0, err)
        rows = memidx.standing_mark_rows(self.parsed())
        self.assertEqual([r["link"] for r in rows], ["L4", "L3"])
        self.assertLintClean()

    def test_remove(self):
        self.write(topic_text(standing="standing: [L2, L3]\n"))
        rc, _o, err = self.mark("L2", "--remove")
        self.assertEqual(rc, 0, err)
        self.assertIn("standing: [L3]\n", self.read())
        row = memidx.standing_mark_rows(self.parsed())[0]
        self.assertEqual((row["link"], row["action"]), ("L2", "remove"))
        self.assertLintClean()

    def test_remove_last_keeps_the_key_as_empty_list(self):
        self.write(topic_text(standing="standing: [L2]\n"))
        rc, _o, err = self.mark("L2", "--remove")
        self.assertEqual(rc, 0, err)
        self.assertIn("standing: []\nstanding_marks:\n", self.read())
        self.assertEqual(self.parsed()["standing"], [])
        self.assertLintClean()

    def test_remove_from_block_list(self):
        self.write(topic_text(standing="standing:\n  - L2\n  - L3\n"))
        self.mark("L2", "--remove")
        self.assertIn("standing:\n  - L3\nstanding_marks:\n", self.read())
        self.mark("L3", "--remove", extra=())
        self.assertIn("standing: []\nstanding_marks:\n", self.read())
        self.assertEqual(self.parsed()["standing"], [])

    def test_a_superseded_link_can_be_removed(self):
        links = (link_yaml("L3"), link_yaml("L2", status="superseded", extra="    superseded_by: L3\n"))
        self.write(topic_text(standing="standing: [L2]\n", links=links, current="L3"))
        rc, _o, err = self.mark("L2", "--remove")
        self.assertEqual(rc, 0, err)

    def test_already_standing_is_a_noop(self):
        text = self.write(topic_text(standing="standing: [L2]\n"))
        rc, out, err = self.mark("L2", "--add")
        self.assertEqual(rc, 0, err)
        self.assertIn("already standing", out)
        self.assertEqual(self.read(), text)

    def test_absent_remove_is_a_noop(self):
        text = self.write(topic_text(standing="standing: [L2]\n"))
        rc, out, err = self.mark("L3", "--remove")
        self.assertEqual(rc, 0, err)
        self.assertIn("not standing", out)
        self.assertEqual(self.read(), text)
        text = self.write(topic_text(standing=""))
        self.assertEqual(self.mark("L3", "--remove")[0], 0)
        self.assertEqual(self.read(), text)

    def test_ineligible_links_refused(self):
        links = (
            link_yaml("L5", status="provisional"),
            link_yaml("L4", auth="agent-inference"),
            link_yaml("L3", status="superseded", extra="    superseded_by: L4\n"),
            link_yaml("L2"),
        )
        text = self.write(topic_text(standing="standing: [L2]\n", links=links, current="L4"))
        for lid in ("L5", "L4", "L3", "L9"):
            with self.subTest(link=lid):
                rc, _o, err = self.mark(lid, "--add")
                self.assertEqual(rc, 1, err)
                self.assertEqual(self.read(), text)
        self.assertIn("not active", self.mark("L5", "--add")[2])
        self.assertIn("ruling.authority", self.mark("L4", "--add")[2])
        self.assertIn("not a link in this topic", self.mark("L9", "--add")[2])

    def test_dry_run_writes_nothing_and_prints_the_diff(self):
        text = self.write(topic_text(standing="standing: [L2]\n"))
        rc, out, err = self.mark("L3", "--add", extra=("--dry-run",))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.read(), text)
        self.assertIn("-standing: [L2]", out)
        self.assertIn("+standing: [L2, L3]", out)
        self.assertIn("dry-run: nothing written", out)

    def test_crlf_file_keeps_its_line_endings(self):
        text = topic_text(standing="standing: [L2]\n").replace("\n", "\r\n")
        self.path.write_bytes(text.encode())
        rc, _o, err = self.mark("L3", "--add")
        self.assertEqual(rc, 0, err)
        raw = self.path.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"standing: [L2, L3]\r\nstanding_marks:\r\n", raw)
        self.assertEqual(self.parsed()["standing"], ["L2", "L3"])

    def test_why_and_source_must_be_one_non_empty_line(self):
        text = self.write(topic_text(standing="standing: [L2]\n"))
        for kw in ({"why": "two\nlines"}, {"why": "cr\rhere"}, {"why": "  "}, {"source": ""},
                   {"source": "a\nb"}, {"why": "sep arator"}):
            with self.subTest(kw=kw):
                rc, _o, err = self.mark("L3", "--add", **kw)
                self.assertEqual(rc, 1, err)
                self.assertEqual(self.read(), text)

    def test_agent_authority_is_not_accepted(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.mark("L3", "--add", authority="agent-inference")

    def test_unknown_topic_and_unparseable_file(self):
        self.write(topic_text())
        self.assertEqual(self.mark("L3", "--add", topic="TOP-0000")[0], 1)
        bad = self.write(topic_text().replace("tags: []", "tags: not-a-list"))
        rc, _o, err = self.mark("L3", "--add")
        self.assertEqual(rc, 1)
        self.assertEqual(self.read(), bad)

    def test_recorded_by_flag_and_environment(self):
        self.write(topic_text(standing="standing: [L2]\n"))
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_RECORDED_BY": "env-agent"}):
            self.mark("L3", "--add")
        self.assertEqual(memidx.standing_mark_rows(self.parsed())[0]["recorded_by"], "env-agent")
        self.mark("L4", "--add", extra=("--recorded-by", "flag-agent"))
        self.assertEqual(memidx.standing_mark_rows(self.parsed())[0]["recorded_by"], "flag-agent")

    def test_a_recorded_by_that_yaml_would_retype_is_refused(self):
        text = self.write(topic_text(standing="standing: [L2]\n"))
        rc, _o, err = self.mark("L3", "--add", extra=("--recorded-by", "yes"))
        self.assertEqual(rc, 1, err)
        self.assertEqual(self.read(), text)

    def test_every_write_reparses_with_earlier_bytes_untouched(self):
        original = self.write(topic_text(standing="standing: [L2]\n", marks=marks_block(mark_yaml("L2", recorded_at="2026-11-01T08:00:00-05:00"))))
        self.mark("L3", "--add")
        new = self.read()
        old_row = mark_yaml("L2", recorded_at="2026-11-01T08:00:00-05:00")
        self.assertIn(old_row, new)
        self.assertEqual(new.replace("standing: [L2, L3]", "standing: [L2]").split("standing_marks:\n")[0],
                         original.split("standing_marks:\n")[0])
        self.assertLintClean()

    def test_memlint_guard_accepts_the_command_output(self):
        root = git_store(self.tmp / "gs")
        (root / "topics" / "foo.md").write_text(topic_text(standing="standing: [L2]\n"))
        commit_all(root)
        rc, _o, err = run_memidx(["mark-standing", "TOP-9100", "L3", "--add", "--authority", "owner-ratified",
                                  "--source", "s", "--why", "w", "--root", str(root)])
        self.assertEqual(rc, 0, err)
        rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
        self.assertEqual(rc, 0, out)


class TestMarkStandingCommandSymlinkedTmp(SymlinkedTmpMixin, TestMarkStandingCommand):
    pass



# ---------------------------------------------------------------------------
# the guard: REF vs working tree AND REF vs staged blob
# ---------------------------------------------------------------------------

M2 = mark_yaml("L2", recorded_at="2026-11-01T08:00:00-05:00", why="first mark")
M3 = mark_yaml("L3", recorded_at="2026-11-02T08:00:00-05:00", why="second mark")
NEW3 = mark_yaml("L3", action="add", recorded_at=STAMP)

# head L3 active, L2 active, L1 historical; the topic stands on L2
G_LINKS = (link_yaml("L3", text="third"), link_yaml("L2", text="second"),
           link_yaml("L1", status="historical", text="first"))


def base_text(**kw):
    kw.setdefault("standing", "standing: [L2]\n")
    kw.setdefault("marks", marks_block(M2))
    kw.setdefault("links", G_LINKS)
    kw.setdefault("current", "L3")
    return topic_text(**kw)


class GuardCase(ErrCase, TmpCase):
    """`guard(new_text)` runs the REF-vs-worktree and REF-vs-staged checks and
    asserts they agree (the store hook reads the staged blob)."""

    def setUp(self):
        super().setUp()
        self.root = None

    def store(self, text=None):
        self.stores = getattr(self, "stores", 0) + 1
        self.root = git_store(self.tmp / f"store{self.stores}")
        (self.root / "topics" / "foo.md").write_text(text if text is not None else base_text())
        commit_all(self.root)
        return self.root

    def guard(self, new_text, rel="foo.md"):
        if self.root is None:
            self.store()
        target = self.root / "topics" / rel
        target.write_text(new_text)
        rc_wt, out_wt = run_memlint(["--against-ref", "HEAD", str(self.root)])
        git(["add", "-A"], self.root)
        rc_st, out_st = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        self.assertEqual(rc_wt, rc_st, f"worktree vs staged disagree:\n{out_wt}\n{out_st}")
        git(["reset", "-q"], self.root)
        return rc_wt, out_wt

    def assertClean(self, new_text):
        rc, out = self.guard(new_text)
        self.assertEqual(rc, 0, out)

    def assertRefused(self, new_text, *needles):
        rc, out = self.guard(new_text)
        self.assertEqual(rc, 1, out)
        for n in needles:
            self.assertIn(n, out)
        return out


class TestStandingPairing(GuardCase):
    def test_unchanged_file_is_clean(self):
        self.store()
        self.assertClean(base_text())

    def test_bare_add_refused(self):
        out = self.assertRefused(base_text(standing="standing: [L2, L3]\n"),
                                 "standing changed for L3 (add) with no matching standing_marks row")

    def test_bare_remove_refused(self):
        self.assertRefused(base_text(standing="standing: []\n"),
                           "standing changed for L2 (remove) with no matching standing_marks row")

    def test_matched_add_passes(self):
        self.assertClean(base_text(standing="standing: [L2, L3]\n", marks=marks_block(NEW3, M2)))

    def test_matched_remove_passes(self):
        gone = mark_yaml("L2", action="remove", recorded_at=STAMP)
        self.assertClean(base_text(standing="standing: []\n", marks=marks_block(gone, M2)))

    def test_supersession_pair_with_pointer_and_current_move_passes(self):
        self.store(base_text(standing="standing: [L3]\n", marks=marks_block(M3)))
        links = (
            link_yaml("L4", text="fourth"),
            link_yaml("L3", status="superseded", text="third", extra="").replace(
                "    status: superseded\n", "    status: superseded\n    superseded_by: L4\n"),
            link_yaml("L2", text="second"),
            link_yaml("L1", status="historical", text="first"),
        )
        # both calls happen before ONE commit: {remove L3, add L4}
        rows = (mark_yaml("L4", action="add", recorded_at="2026-11-03T09:12:42-05:00"),
                mark_yaml("L3", action="remove", recorded_at=STAMP), M3)
        self.assertClean(base_text(standing="standing: [L4]\n", marks=marks_block(*rows),
                                   links=links, current="L4"))
        # without the remove row the pointer drop is bare
        self.assertRefused(base_text(standing="standing: [L4]\n", marks=marks_block(rows[0], M3),
                                     links=links, current="L4"),
                           "standing changed for L3 (remove) with no matching")

    def test_orphan_row_refused(self):
        self.assertRefused(base_text(marks=marks_block(NEW3, M2)),
                           "orphan standing mark (L3, add): standing: did not change that way")

    def test_duplicate_row_in_one_commit_refused(self):
        again = mark_yaml("L3", action="add", recorded_at="2026-11-03T09:12:42-05:00")
        self.assertRefused(base_text(standing="standing: [L2, L3]\n", marks=marks_block(again, NEW3, M2)),
                           "duplicate standing mark for (L3, add) in one commit")

    def test_net_empty_churn_is_two_orphans(self):
        add_back = mark_yaml("L2", action="add", recorded_at="2026-11-03T09:12:42-05:00")
        out = self.assertRefused(base_text(marks=marks_block(
            add_back, mark_yaml("L2", action="remove", recorded_at=STAMP), M2)),
            "orphan standing mark (L2, remove)", "orphan standing mark (L2, add)")

    def test_reorder_with_same_set_needs_no_row(self):
        self.store(base_text(standing="standing: [L2, L3]\n", marks=marks_block(M3, M2)))
        self.assertClean(base_text(standing="standing: [L3, L2]\n", marks=marks_block(M3, M2)))

    def test_flow_to_block_conversion_with_same_set_needs_no_row(self):
        self.assertClean(base_text(standing="standing:\n  - L2\n"))

    def test_first_marks_block_on_a_grandfathered_topic(self):
        self.store(base_text(marks=""))
        self.assertClean(base_text(standing="standing: [L2, L3]\n", marks=marks_block(NEW3)))
        self.assertRefused(base_text(standing="standing: [L2, L3]\n", marks=""),
                           "standing changed for L3 (add)")

    def test_grandfathered_topic_unrelated_edit_is_clean(self):
        self.store(base_text(marks=""))
        links = (link_yaml("L4", text="fourth"),) + G_LINKS
        self.assertClean(base_text(marks="", links=links, current="L4"))

    def test_duplicate_standing_ids_do_not_hide_a_change(self):
        self.assertRefused(base_text(standing="standing: [L2, L3, L3]\n"),
                           "standing changed for L3 (add)")


class TestMarksFreeze(GuardCase):
    def test_edited_row_refused(self):
        out = self.assertRefused(base_text(marks=marks_block(M2.replace("first mark", "EDITED"))),
                                 "standing mark (L2, add, 2026-11-01T08:00:00-05:00)",
                                 "row changed after being recorded")

    def test_reformatted_row_refused(self):
        self.assertRefused(base_text(marks=marks_block(M2.replace('why: "first mark"', "why: 'first mark'"))),
                           "row reformatted, not appended")

    def test_removed_row_refused(self):
        self.assertRefused(base_text(marks=""), "row removed after being recorded")

    def test_new_row_below_old_rows_refused(self):
        self.store(base_text(standing="standing: [L2, L3]\n", marks=marks_block(M3, M2)))
        newer = mark_yaml("L2", action="remove", recorded_at=STAMP)
        self.assertRefused(base_text(standing="standing: [L3]\n", marks=marks_block(M3, M2, newer)),
                           "marks not prepended")

    def test_recorded_rows_swapped_refused(self):
        self.store(base_text(standing="standing: [L2, L3]\n", marks=marks_block(M3, M2)))
        self.assertRefused(base_text(standing="standing: [L2, L3]\n", marks=marks_block(M2, M3)),
                           "recorded rows reordered")

    def test_a_marks_block_the_walker_cannot_read_is_an_error_not_a_note(self):
        flow = ("standing_marks: [{link: L3, action: add, date: 2026-11-03, "
                f"recorded_at: \"{STAMP}\", recorded_by: t, authority: owner-ratified, "
                "source: \"s\", why: \"w\"}]\n")
        self.assertRefused(base_text(standing="standing: [L2, L3]\n", marks=flow),
                           "could not extract the rows' raw text")
        # ... and a recorded block that stops being readable
        self.store(base_text())
        self.assertRefused(base_text(marks=flow.replace("L3", "L2").replace(STAMP, "2026-11-01T08:00:00-05:00")),
                           "could not extract")

    def test_crlf_marks_block_round_trips(self):
        self.store(base_text().replace("\n", "\r\n"))
        new = base_text(standing="standing: [L2, L3]\n", marks=marks_block(NEW3, M2)).replace("\n", "\r\n")
        self.assertClean(new)
        # a mixed-ending rewrite of a recorded row is a reformat
        bad = new.replace('why: "first mark"\r\n', 'why: "first mark"\n')
        self.assertRefused(bad, "row reformatted, not appended")

    def test_repair_branch_still_freezes_the_rows(self):
        root = git_store(self.tmp / "repair")
        self.root = root
        broken = base_text().replace("tags: []", "tags: not-a-list", 1)
        broken = broken.split("links:")[0] + "links: []\n---\n\nBody.\n"
        (root / "topics" / "foo.md").write_text(broken)
        commit_all(root)
        self.assertFalse(memidx.parse_record_text(broken, "x").valid)
        self.assertRefused(base_text(marks=marks_block(M2.replace("first mark", "EDITED"))),
                           "row changed after being recorded")
        self.assertClean(base_text())


class TestLinkOrderAndCurrent(GuardCase):
    def test_links_reordered_refused_and_nothing_else_fires(self):
        # swap L2 and L3 bytes verbatim: every per-link span is intact
        swapped = (G_LINKS[1], G_LINKS[0], G_LINKS[2])
        out = self.assertRefused(base_text(links=swapped, current="L2"),
                                 "links reordered (L2 before L3; the chain head is first-active-in-file-order)")
        for old_message in ("link reformatted", "changed after being recorded", "inserted out of order"):
            self.assertNotIn(old_message, out)

    def test_links_reorder_check_is_what_catches_it(self):
        swapped = (G_LINKS[1], G_LINKS[0], G_LINKS[2])
        with mock.patch.object(memlint, "_link_order_errors", return_value=[]):
            self.assertClean(base_text(links=swapped, current="L2"))

    def test_new_links_may_sit_between_old_ones_in_order(self):
        # in-order interleave is a different rule's business (inserted out of order)
        rc, out = self.guard(base_text(links=(G_LINKS[0], link_yaml("L9"), G_LINKS[1], G_LINKS[2])))
        self.assertNotIn("links reordered", out)

    def test_bare_current_flip_refused(self):
        out = self.assertRefused(base_text(current="L2"), "current: 'L2' does not match the newest active link",
                                 "current changed L3 -> L2 without a change of the newest active link")

    def test_current_with_a_new_active_link_passes(self):
        links = (link_yaml("L4", text="fourth"),) + G_LINKS
        self.assertClean(base_text(links=links, current="L4"))

    def test_new_active_link_without_moving_current_refused(self):
        links = (link_yaml("L4", text="fourth"),) + G_LINKS
        self.assertRefused(base_text(links=links, current="L3"), "current: 'L3' does not match")

    def test_provisional_link_never_licenses_a_current_move(self):
        links = (link_yaml("L4", status="provisional", auth="agent-inference"),) + G_LINKS
        self.assertRefused(base_text(links=links, current="L4"), "current: 'L4' does not match")
        self.assertClean(base_text(links=links, current="L3"))

    def test_repair_of_an_existing_disagreement_passes(self):
        self.store(base_text(current="L2"))   # REF already disagreed: current L2, head L3
        self.assertClean(base_text(current="L3"))

    def test_historical_move_exposing_an_older_active_link_passes(self):
        gone = link_yaml("L3", status="historical", text="third")
        self.assertClean(base_text(links=(gone, G_LINKS[1], G_LINKS[2]), current="L2"))

    def test_absent_current_with_unchanged_head_passes(self):
        self.store(base_text(current=None))
        self.assertClean(base_text(current=None, marks=marks_block(M2, M3)).replace(
            marks_block(M2, M3), marks_block(M2)))

    def test_removing_current_with_head_unchanged_refused(self):
        self.assertRefused(base_text(current=None), "current changed L3 -> None without a change")

    def test_adding_current_to_a_topic_that_lacked_one_passes(self):
        self.store(base_text(current=None))
        self.assertClean(base_text(current="L3"))


class TestAddedTopics(GuardCase):
    def test_new_topic_with_standing_and_no_rows_refused(self):
        self.store()
        out = self.assertRefusedNew(base_text(tid="TOP-9200", marks=""), "standing changed for L2 (add)")

    def assertRefusedNew(self, text, *needles):
        (self.root / "topics" / "bar.md").write_text(text)
        git(["add", "-A"], self.root)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        git(["reset", "-q"], self.root)
        (self.root / "topics" / "bar.md").unlink()
        self.assertEqual(rc, 1, out)
        for n in needles:
            self.assertIn(n, out)
        return out

    def test_new_topic_with_matching_rows_passes(self):
        self.store()
        text = base_text(tid="TOP-9200", marks=marks_block(mark_yaml("L2", recorded_at=STAMP)))
        (self.root / "topics" / "bar.md").write_text(text)
        git(["add", "-A"], self.root)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        self.assertEqual(rc, 0, out)

    def test_new_topic_with_rows_and_empty_standing_has_orphans(self):
        self.store()
        self.assertRefusedNew(base_text(tid="TOP-9200", standing="standing: []\n",
                                        marks=marks_block(mark_yaml("L2", recorded_at=STAMP))),
                              "orphan standing mark (L2, add)")

    def test_new_topic_without_standing_is_free(self):
        self.store()
        (self.root / "topics" / "bar.md").write_text(base_text(tid="TOP-9200", standing="", marks=""))
        git(["add", "-A"], self.root)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        self.assertEqual(rc, 0, out)


def _symlinked(cls):
    return type(f"{cls.__name__}SymlinkedTmp", (SymlinkedTmpMixin, cls), {})


TestStandingPairingSymlinkedTmp = _symlinked(TestStandingPairing)
TestMarksFreezeSymlinkedTmp = _symlinked(TestMarksFreeze)
TestLinkOrderAndCurrentSymlinkedTmp = _symlinked(TestLinkOrderAndCurrent)
TestAddedTopicsSymlinkedTmp = _symlinked(TestAddedTopics)


if __name__ == "__main__":
    unittest.main()
