"""0.5.0 PR 2 -- binding proposals and anchors: the `anchors:` schema and its
append-only freeze (memlint), the capture commands, and the delivery routes.
Every fixture builds its own throwaway store, code root and session files."""
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

DIGEST = "a" * 64
DIGEST2 = "b" * 64


def anchor_block(rows, indent="  "):
    """`anchors:` frontmatter text for a list of dicts (insertion order kept)."""
    out = ["anchors:\n"]
    for row in rows:
        first = True
        for k, v in row.items():
            lead = f"{indent}- " if first else f"{indent}  "
            first = False
            out.append(f"{lead}{k}: {v}\n")
    return "".join(out)


def good_row(**over):
    row = {
        "ref": "src/foo.py",
        "link": "L1",
        "algo": "sha256",
        "digest": f'"{DIGEST}"',
        "span": "file",
        "captured": "bind",
        "captured_at": "2026-11-02",
        "code_head": '"6228be7"',
        "worktree": "clean",
    }
    row.update(over)
    return row


def ref_mark_text(ref, action="add", recorded_at="2026-11-03T09:12:41-05:00"):
    return (
        "ref_marks:\n"
        f"  - ref: \"{ref}\"\n"
        f"    action: {action}\n"
        f"    date: {recorded_at[:10]}\n"
        f"    recorded_at: \"{recorded_at}\"\n"
        "    recorded_by: tester\n"
        "    authority: agent-inference\n"
        "    source: \"test\"\n"
        "    why: \"test\"\n"
    )


def topic_text(rows=None, code_refs=("src/foo.py",), extra_links="", anchors_text=None, marks_text=""):
    refs = "".join(f"  - {r}\n" for r in code_refs)
    refs_block = f"code_refs:\n{refs}" if code_refs else "code_refs: []\n"
    anchors = anchors_text if anchors_text is not None else (anchor_block(rows) if rows else "")
    return (
        "---\n"
        "type: topic\n"
        "id: TOP-1\n"
        "title: Test topic\n"
        "area: memory\n"
        "current: L1\n"
        "tags: []\n"
        f"{refs_block}"
        f"{marks_text}"
        f"{anchors}"
        "links:\n"
        f"{extra_links}"
        "  - link: L1\n"
        "    date: '2024-01-01'\n"
        "    status: active\n"
        "    kind: adopted\n"
        "    ruling:\n"
        "      text: \"first ruling\"\n"
        "      authority: agent-inference\n"
        "---\n\nBody.\n"
    )


def git(args, cwd):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=True
    )


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


def lint_text(text):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "topics").mkdir()
        (root / "topics" / "foo.md").write_text(text)
        return memlint.lint_root(root)


def run_memidx(args):
    """memidx.main(args) in-process; returns (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = memidx.main(args)
    return rc, out.getvalue(), err.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PY_SRC = (
    "def alpha():\n"
    "    return 1\n"
    "\n"
    "\n"
    "def beta():\n"
    "    x = 2\n"
    "    return x\n"
)

PY_AMBIGUOUS = (
    "class A:\n"
    "    def run(self):\n"
    "        return 1\n"
    "\n"
    "class B:\n"
    "    def run(self):\n"
    "        return 2\n"
)


def make_code_root(td, files):
    """A throwaway git repo holding `files` ({relpath: text}), committed."""
    root = Path(td) / "code"
    root.mkdir()
    git(["init", "-q", str(root)], root)
    git(["config", "user.email", "t@example.com"], root)
    git(["config", "user.name", "T"], root)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode())
    git(["add", "-A"], root)
    git(["commit", "-q", "-m", "code"], root)
    return root


def link_spans(text):
    fm = memidx.parse_record_text(text, "x").frontmatter
    return memlint._link_raw_spans(text, fm["links"])


class TestAnchorSchemaLint(unittest.TestCase):
    def assertError(self, errors, *needles):
        self.assertTrue(
            any(all(n in e for n in needles) for e in errors), f"{needles} not in {errors}"
        )

    def test_valid_block_passes(self):
        errors, _ = lint_text(topic_text([good_row()]))
        self.assertEqual(errors, [], errors)

    def test_valid_symbol_row_passes(self):
        row = good_row(ref="src/foo.py#bar", span="symbol", chunker_version='"py-1"')
        errors, _ = lint_text(topic_text([row], code_refs=("src/foo.py#bar",)))
        self.assertEqual(errors, [], errors)

    def test_ref_not_in_code_refs(self):
        errors, _ = lint_text(topic_text([good_row(ref="src/other.py")]))
        self.assertError(errors, "not present verbatim in this topic's code_refs")

    def test_link_not_in_links(self):
        errors, _ = lint_text(topic_text([good_row(link="L9")]))
        self.assertError(errors, "is not a link in this topic")

    def test_algo_must_be_sha256(self):
        errors, _ = lint_text(topic_text([good_row(algo="md5")]))
        self.assertError(errors, "algo")

    def test_digest_must_be_64_lowercase_hex(self):
        for bad in ('"abc"', f'"{"A" * 64}"', f'"{"g" * 64}"', f'"{"a" * 63}"'):
            with self.subTest(digest=bad):
                errors, _ = lint_text(topic_text([good_row(digest=bad)]))
                self.assertError(errors, "digest")

    def test_unknown_span_captured_worktree(self):
        for field, value in (("span", "line"), ("captured", "magic"), ("worktree", "moist")):
            with self.subTest(field=field):
                errors, _ = lint_text(topic_text([good_row(**{field: value})]))
                self.assertError(errors, f"unknown {field}")

    def test_prefix_and_glob_refs_carry_no_row(self):
        for ref in ("src/", "src/*.py", "src/[ab].py"):
            with self.subTest(ref=ref):
                errors, _ = lint_text(topic_text([good_row(ref=f'"{ref}"')], code_refs=(f'"{ref}"',)))
                self.assertError(errors, "prefix or glob")

    def test_duplicate_ref_link_key(self):
        rows = [good_row(), good_row(captured_at="2026-11-03", digest=f'"{DIGEST2}"')]
        errors, _ = lint_text(topic_text(rows))
        self.assertError(errors, "more than one row")

    def test_symbol_row_requires_chunker_version(self):
        row = good_row(ref="src/foo.py#bar", span="symbol")
        errors, _ = lint_text(topic_text([row], code_refs=("src/foo.py#bar",)))
        self.assertError(errors, "chunker_version")

    def test_anchors_must_be_list_of_mappings(self):
        text = topic_text(anchors_text="anchors:\n  - just-a-string\n")
        errors, _ = lint_text(text)
        self.assertError(errors, "anchors must be a list of mappings")


class TestAnchorFreeze(unittest.TestCase):
    def _base(self, td, rows=None):
        root = git_store(td)
        (root / "topics" / "foo.md").write_text(topic_text(rows if rows is not None else [good_row()]))
        commit_all(root)
        return root

    def test_appended_row_passes_and_links_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base(td)
            new_row = good_row(ref="src/bar.py", captured_at="2026-11-03")
            (root / "topics" / "foo.md").write_text(
                topic_text([good_row(), new_row], code_refs=("src/foo.py", "src/bar.py"),
                           marks_text=ref_mark_text("src/bar.py"))
            )
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_first_block_added_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = git_store(td)
            (root / "topics" / "foo.md").write_text(topic_text())
            commit_all(root)
            (root / "topics" / "foo.md").write_text(topic_text([good_row()]))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_edited_digest_errors_naming_the_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base(td)
            (root / "topics" / "foo.md").write_text(topic_text([good_row(digest=f'"{DIGEST2}"')]))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("anchor ('src/foo.py', 'L1')", out)
            self.assertIn("changed after being recorded", out)

    def test_removed_row_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base(td)
            (root / "topics" / "foo.md").write_text(topic_text())
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("anchor ('src/foo.py', 'L1')", out)

    def test_changed_captured_at_identity_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base(td)
            (root / "topics" / "foo.md").write_text(topic_text([good_row(captured_at="2026-11-09")]))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)

    def test_reformatted_row_same_fields_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base(td)
            text = topic_text([good_row()]).replace(
                f'digest: "{DIGEST}"', f"digest: '{DIGEST}'"
            )
            (root / "topics" / "foo.md").write_text(text)
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("reformatted, not appended", out)

    def test_repaired_ref_blob_still_freezes_its_anchor_rows(self):
        """REF blob invalid with no recoverable links (a repair branch): the
        anchor rows it recorded must not be rewritten under cover of a note."""
        with tempfile.TemporaryDirectory() as td:
            root = git_store(td)
            old = topic_text([good_row()]).replace("tags: []", "tags: not-a-list", 1)
            old = old.split("links:")[0] + "links: []\n---\n\nBody.\n"
            (root / "topics" / "foo.md").write_text(old)
            commit_all(root)
            old_result = memidx.parse_record_text(old, "x")
            self.assertFalse(old_result.valid)
            (root / "topics" / "foo.md").write_text(topic_text([good_row(digest=f'"{DIGEST2}"')]))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("anchor ('src/foo.py', 'L1')", out)
            (root / "topics" / "foo.md").write_text(topic_text([good_row()]))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_second_row_for_existing_key_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base(td)
            rows = [good_row(), good_row(captured_at="2026-11-03", digest=f'"{DIGEST2}"')]
            (root / "topics" / "foo.md").write_text(topic_text(rows))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("more than one row", out)


class TestBind(unittest.TestCase):
    TODAY = "2026-11-02"

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = self._td.name
        self.code = make_code_root(self.td, {"src/foo.py": PY_SRC, "src/amb.py": PY_AMBIGUOUS,
                                             "src/data.txt": "hello\n"})
        self.store = git_store(Path(self.td) / "store")
        patcher = mock.patch.object(memidx, "_anchor_today", return_value=self.TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put_topic(self, text):
        (self.store / "topics" / "foo.md").write_text(text)
        commit_all(self.store)

    def topic(self):
        return (self.store / "topics" / "foo.md").read_text()

    def bind(self, ref, *extra):
        return run_memidx(["bind", "TOP-1", ref, "--root", str(self.store),
                           "--code-root", str(self.code), *extra])

    def test_dry_run_prints_diff_and_writes_nothing(self):
        self.put_topic(topic_text(code_refs=()))
        before = self.topic()
        rc, out, err = self.bind("src/foo.py", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.topic(), before)
        self.assertIn("+anchors:", out)
        self.assertIn(f'+    digest: "{sha(PY_SRC.encode())}"', out)
        self.assertIn("dry-run: nothing written", out)

    def test_write_places_anchors_before_links_and_keeps_link_bytes(self):
        self.put_topic(topic_text())
        before = self.topic()
        spans_before = link_spans(before)
        rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 0, err)
        after = self.topic()
        self.assertLess(after.index("anchors:"), after.index("links:"))
        self.assertEqual(link_spans(after), spans_before)
        fm = memidx.parse_record_text(after, "x").frontmatter
        row = fm["anchors"][0]
        self.assertEqual(row["ref"], "src/foo.py")
        self.assertEqual(row["link"], "L1")
        self.assertEqual(row["span"], "file")
        self.assertEqual(row["captured"], "bind")
        self.assertEqual(row["digest"], sha(PY_SRC.encode()))
        self.assertEqual(str(row["captured_at"]), self.TODAY)
        self.assertEqual(row["worktree"], "clean")
        self.assertRegex(row["code_head"], r"^[0-9a-f]{7,}$")
        rc, lint = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, lint)
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)

    def test_appends_to_an_existing_anchors_block(self):
        first = good_row(ref="src/data.txt")
        self.put_topic(topic_text([first], code_refs=("src/data.txt",)))
        rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 0, err)
        after = self.topic()
        self.assertEqual(after.count("anchors:"), 1)
        fm = memidx.parse_record_text(after, "x").frontmatter
        self.assertEqual([r["ref"] for r in fm["anchors"]], ["src/data.txt", "src/foo.py"])
        self.assertEqual(fm["code_refs"], ["src/data.txt", "src/foo.py"])
        rc, lint = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, lint)

    def test_empty_inline_code_refs_becomes_a_block(self):
        self.put_topic(topic_text(code_refs=()))
        self.assertIn("code_refs: []", self.topic())
        rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 0, err)
        after = self.topic()
        self.assertNotIn("code_refs: []", after)
        self.assertIn('code_refs:\n  - "src/foo.py"\n', after)

    def test_existing_identical_code_ref_is_left_alone(self):
        self.put_topic(topic_text(code_refs=("src/foo.py",)))
        rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.topic().count("src/foo.py"), 1 + self.topic().count("ref: "))

    def test_second_bind_for_same_key_is_a_noop(self):
        self.put_topic(topic_text())
        self.assertEqual(self.bind("src/foo.py")[0], 0)
        once = self.topic()
        rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 0)
        self.assertIn("already has an anchor", out)
        self.assertEqual(self.topic(), once)

    def test_symbol_one_match_hashes_the_inclusive_span(self):
        self.put_topic(topic_text())
        rc, out, err = self.bind("src/foo.py#beta")
        self.assertEqual(rc, 0, err)
        fm = memidx.parse_record_text(self.topic(), "x").frontmatter
        row = fm["anchors"][0]
        self.assertEqual(row["span"], "symbol")
        self.assertTrue(row["chunker_version"])
        beta = "def beta():\n    x = 2\n    return x\n"
        self.assertEqual(row["digest"], sha(beta.encode()))
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)

    def test_symbol_zero_matches_is_captured_as_file_span(self):
        self.put_topic(topic_text())
        rc, out, err = self.bind("src/foo.py#nope")
        self.assertEqual(rc, 0, err)
        row = memidx.parse_record_text(self.topic(), "x").frontmatter["anchors"][0]
        self.assertEqual(row["span"], "file")
        self.assertEqual(row["digest"], sha(PY_SRC.encode()))
        self.assertEqual(row["ref"], "src/foo.py#nope")

    def test_symbol_two_matches_writes_nothing_and_says_ambiguous(self):
        self.put_topic(topic_text())
        before = self.topic()
        rc, out, err = self.bind("src/amb.py#run")
        self.assertEqual(rc, 1)
        self.assertIn("symbol ambiguous", err)
        self.assertEqual(self.topic(), before)

    def check_unanchored_bind(self, ref):
        self.put_topic(topic_text())
        rc, out, err = self.bind(ref)
        self.assertEqual(rc, 0, err)
        self.assertIn("bound without an anchor", out)
        fm = memidx.parse_record_text(self.topic(), "x").frontmatter
        self.assertEqual(fm["code_refs"], ["src/foo.py", ref])
        self.assertEqual([(m["ref"], m["action"]) for m in fm["ref_marks"]], [(ref, "add")])
        self.assertFalse(fm.get("anchors"))
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)
        rc, lint = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, lint)
        self.assertEqual(self.bind(ref)[0], 0)
        self.assertIn("already bound", self.bind(ref)[1])

    def test_prefix_is_bound_without_an_anchor(self):
        self.check_unanchored_bind("src/")

    def test_glob_is_bound_without_an_anchor(self):
        self.check_unanchored_bind("src/*.py")

    def test_prefix_dry_run_prints_the_diff_and_writes_nothing(self):
        self.put_topic(topic_text())
        before = self.topic()
        rc, out, err = self.bind("src/", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.topic(), before)
        self.assertIn("+  - ", out)
        self.assertIn("dry-run: nothing written", out)

    def test_unbound_prefix_is_restored_by_bind(self):
        self.put_topic(topic_text())
        base = memidx._now()
        steps = iter(base + timedelta(minutes=i) for i in range(1, 4))
        with mock.patch.object(memidx, "_now", side_effect=lambda: next(steps)):
            self.assertEqual(self.bind("src/")[0], 0)
            commit_all(self.store)
            rc, out, err = run_memidx(["unbind", "TOP-1", "src/", "--root", str(self.store)])
            self.assertEqual(rc, 0, err)
            commit_all(self.store)
            rc, out, err = self.bind("src/")
        self.assertEqual(rc, 0, err)
        fm = memidx.parse_record_text(self.topic(), "x").frontmatter
        self.assertIn("src/", fm["code_refs"])
        self.assertEqual([m["action"] for m in fm["ref_marks"]], ["add", "remove", "add"])
        self.assertFalse(fm.get("anchors"))
        rc, lint = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, lint)
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)

    def test_prefix_without_slash_or_naming_no_directory_is_refused(self):
        self.put_topic(topic_text())
        before = self.topic()
        for ref, needle in (("nodir/", "no existing directory"),
                            ("src/foo.py/", "no existing directory"),
                            ("src/*.py#beta", "no #symbol fragment")):
            with self.subTest(ref=ref):
                rc, out, err = self.bind(ref)
                self.assertEqual(rc, 1, err)
                self.assertIn(needle, err)
        rc, out, err = self.bind("src")
        self.assertEqual(rc, 2)
        self.assertIn("directory", err)
        self.assertEqual(self.topic(), before)

    def test_prefix_must_stay_under_a_code_root(self):
        """Gate round 1 R1-4: absolute, `..` and symlink-out prefixes are refused; a symlink
        that stays inside is accepted."""
        self.put_topic(topic_text())
        before = self.topic()
        outside = Path(self.td) / "outside-dir"
        (outside / "deep").mkdir(parents=True)
        os.symlink(outside, self.code / "src" / "leak")
        (self.code / "src" / "real").mkdir()
        os.symlink(self.code / "src" / "real", self.code / "src" / "alias")
        for ref in ("/tmp/", f"{outside}/", "../", "src/../../x/", "../*.py", "/etc/*", "src/leak/", "src/leak/deep/"):
            with self.subTest(ref=ref):
                rc, out, err = self.bind(ref)
                self.assertEqual(rc, 1, (out, err))
                self.assertEqual(self.topic(), before)
        self.assertIn("relative to a code root", self.bind("/tmp/")[2])
        self.assertIn("no existing directory", self.bind("src/leak/")[2])
        rc, out, err = self.bind("src/alias/")
        self.assertEqual(rc, 0, err)
        self.assertIn("src/alias/", memidx.parse_record_text(self.topic(), "x").frontmatter["code_refs"])

    def test_directory_without_slash_is_rc_2(self):
        self.put_topic(topic_text())
        rc, out, err = self.bind("src")
        self.assertEqual(rc, 2, err)

    def test_dirty_worktree_is_recorded(self):
        self.put_topic(topic_text())
        (self.code / "src" / "data.txt").write_text("changed\n")
        self.assertEqual(self.bind("src/data.txt")[0], 0)
        row = memidx.parse_record_text(self.topic(), "x").frontmatter["anchors"][0]
        self.assertEqual(row["worktree"], "dirty")
        self.assertEqual(row["digest"], sha(b"changed\n"))

    def test_captured_ruling_label(self):
        self.put_topic(topic_text())
        self.assertEqual(self.bind("src/foo.py", "--captured", "ruling")[0], 0)
        row = memidx.parse_record_text(self.topic(), "x").frontmatter["anchors"][0]
        self.assertEqual(row["captured"], "ruling")

    def test_unknown_topic_and_missing_file_are_rc_1(self):
        self.put_topic(topic_text())
        rc, out, err = run_memidx(["bind", "TOP-99", "src/foo.py", "--root", str(self.store),
                                   "--code-root", str(self.code)])
        self.assertEqual(rc, 1)
        rc, out, err = self.bind("src/missing.py")
        self.assertEqual(rc, 1)
        self.assertIn("file not found", err)


def plain_topic(tid, code_refs, title="T"):
    refs = "".join(f"  - {r}\n" for r in code_refs)
    refs_block = f"code_refs:\n{refs}" if code_refs else "code_refs: []\n"
    return (
        "---\ntype: topic\n"
        f"id: {tid}\ntitle: {title}\narea: testing\ncurrent: L1\ntags: []\n"
        f"{refs_block}"
        "links:\n  - link: L1\n    date: '2026-01-01'\n    status: active\n    kind: adopted\n"
        "    ruling: {text: a ruling, authority: owner-ratified, source: s}\n"
        "---\n\nBody.\n"
    )


class SessionCase(unittest.TestCase):
    """A tmp store + indexed db, a code root and a sessions dir.

    `SYMLINKED_TMP` re-runs a class with every path in the `link` spelling of
    a symlinked temp dir (macOS: /var -> /private/var, INC-0132 family): code
    that returns, joins or compares a realpath-spelled path fails on Linux."""

    SYMLINKED_TMP = False

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = Path(self._td.name)
        if self.SYMLINKED_TMP:
            (self.td / "real").mkdir()
            os.symlink("real", self.td / "link")
            self.td = self.td / "link"
        self.store = self.td / "store"
        (self.store / "topics").mkdir(parents=True)
        self.code = self.td / "code"
        (self.code / "src").mkdir(parents=True)
        self.sessions = self.td / "sessions"
        self.sessions.mkdir()
        self.db = self.td / "idx.sqlite"
        self.home = self.td / "home"
        self.home.mkdir()
        for patcher in (mock.patch.object(memidx, "_anchor_today", return_value="2026-11-02"),
                        mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_topic(self, name, text):
        (self.store / "topics" / f"{name}.md").write_text(text, encoding="utf-8")

    def reindex(self):
        rc, out, err = run_memidx(["reindex", "--root", str(self.store), "--project", "p",
                                   "--db", str(self.db), "--no-embed"])
        self.assertEqual(rc, 0, err)

    def code_file(self, rel, text):
        path = self.code / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        return str(path)

    def session(self, sid, ledger=(), fallbacks=(), lock=True):
        state = {"session_id": sid, "ledger": list(ledger), "search_fallbacks": list(fallbacks)}
        (self.sessions / f"{sid}.json").write_text(json.dumps(state))
        if lock:
            (self.sessions / f"{sid}.json.lock").write_text("")

    def ledger_row(self, rel, sha_hex="c" * 64, kind="code", root=None, seen_at="2026-11-02T10:00:00Z"):
        root = str(self.code) if root is None and kind == "code" else (root or "")
        return {"path": str(self.code / rel), "kind": kind, "content_sha256": sha_hex,
                "seen_at": seen_at, "root": root, "source": "tool"}

    def fallback(self, rel, tid, title="Some title", hook="pre-edit-chain"):
        return {"file": str(self.code / rel), "id": tid, "title": title, "hook": hook}

    def propose(self, sid="s1", *extra, db=True):
        args = ["propose-bindings", "--session", sid, "--root", str(self.store), "--project", "p",
                "--sessions-dir", str(self.sessions), "--code-root", str(self.code), *extra]
        if db:
            args += ["--db", str(self.db)]
        return run_memidx(args)


class TestProposeBindings(SessionCase):
    def test_section_one_joins_fallback_to_ledger_row(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.reindex()
        self.code_file("src/foo.py", "x\n")
        self.session("s1", [self.ledger_row("src/foo.py", "ab" * 32)],
                     [self.fallback("src/foo.py", "TOP-9001", "Alpha title")])
        rc, out, err = self.propose()
        self.assertEqual(rc, 0, err)
        self.assertIn("last 20 fallbacks only", out)
        self.assertIn("TOP-9001  src/foo.py  " + "ab" * 6 + "  2026-11-02T10:00:00Z  pre-edit-chain", out)
        self.assertIn("    Alpha title", out)

    def test_worktree_edit_joins_on_worktree_path_and_proposes_the_main_relpath(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.reindex()
        self.code_file("src/foo.py", "x\n")
        wt_path = str(self.code / ".worktrees" / "w1" / "src" / "foo.py")
        row = self.ledger_row("src/foo.py", "ab" * 32)
        row["worktree_path"] = wt_path
        self.assertNotEqual(row["path"], wt_path)
        fb = {"file": wt_path, "id": "TOP-9001", "title": "Alpha title", "hook": "pre-edit-chain"}
        self.session("s1", [row], [fb])
        rc, out, err = self.propose()
        self.assertEqual(rc, 0, err)
        self.assertIn("TOP-9001  src/foo.py  " + "ab" * 6, out)

    def test_topic_that_already_matches_the_path_is_not_proposed(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"]))
        self.reindex()
        self.session("s1", [self.ledger_row("src/foo.py")], [self.fallback("src/foo.py", "TOP-9001")])
        rc, out, err = self.propose()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("TOP-9001", out)

    def test_stale_index_is_refreshed_before_the_direct_match_exclusion(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.reindex()
        # the on-disk store now binds the path; the index has not seen it
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"], "Alpha"))
        self.session("s1", [self.ledger_row("src/foo.py")], [self.fallback("src/foo.py", "TOP-9001")])
        rc, out, err = self.propose()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("TOP-9001", out)

    def test_removed_binding_no_longer_suppresses_a_proposal(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"], "Alpha"))
        self.reindex()
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.session("s1", [self.ledger_row("src/foo.py")], [self.fallback("src/foo.py", "TOP-9001")])
        rc, out, err = self.propose()
        self.assertEqual(rc, 0, err)
        self.assertIn("TOP-9001  src/foo.py", out)

    def test_non_code_and_rootless_ledger_rows_are_never_proposed(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"]))
        self.reindex()
        self.session("s1", [self.ledger_row("src/foo.py", kind="store")],
                     [self.fallback("src/foo.py", "TOP-9001")])
        rc, out, err = self.propose()
        self.assertNotIn("TOP-9001", out)

    def test_section_two_lists_unmapped_ledgered_code_paths_not_named_above(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/mapped.py"]))
        self.reindex()
        self.session("s1",
                     [self.ledger_row("src/foo.py"), self.ledger_row("src/mapped.py"),
                      self.ledger_row("src/hint.py", "d" * 64)],
                     [self.fallback("src/foo.py", "TOP-9001")])
        rc, out, err = self.propose()
        self.assertEqual(rc, 0, err)
        head, _, hints = out.partition("Filing hints, not bindings:")
        self.assertIn("src/foo.py", head)
        self.assertIn("src/hint.py  " + "d" * 12, hints)
        self.assertNotIn("src/mapped.py", hints)
        self.assertNotIn("src/foo.py", hints)

    def test_coverage_not_ok_prints_unknown_and_never_unmapped(self):
        self.session("s1", [self.ledger_row("src/hint.py")], [])
        rc, out, err = self.propose(db=False)   # no index at the default home -> not "ok"
        self.assertEqual(rc, 0, err)
        tail = out.partition("Filing hints, not bindings:")[2]
        self.assertEqual(tail.strip(), "coverage: unknown")
        self.assertNotIn("src/hint.py", out)

    def test_write_inbox_writes_the_same_text(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"]))
        self.reindex()
        self.session("s1", [self.ledger_row("src/foo.py")], [self.fallback("src/foo.py", "TOP-9001")])
        rc, out, err = self.propose("s1", "--write-inbox")
        self.assertEqual(rc, 0, err)
        written = self.store / "inbox" / "proposals" / "2026-11-02-s1.md"
        self.assertTrue(written.is_file())
        self.assertIn("TOP-9001  src/foo.py", written.read_text())
        self.assertEqual(sorted(p.name for p in (self.store / "topics").iterdir()), ["a.md"])

    def test_without_write_inbox_nothing_is_written(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"]))
        self.reindex()
        self.session("s1", [self.ledger_row("src/foo.py")], [self.fallback("src/foo.py", "TOP-9001")])
        self.assertEqual(self.propose()[0], 0)
        self.assertFalse((self.store / "inbox").exists())

    def test_unreadable_or_missing_session_is_rc_1(self):
        self.reindex()
        rc, out, err = self.propose("nope")
        self.assertEqual(rc, 1)
        (self.sessions / "half.json").write_text('{"ledger": [')
        rc, out, err = self.propose("half")
        self.assertEqual(rc, 1)


class TestAnchorBackfill(SessionCase):
    def backfill(self, *extra):
        return run_memidx(["anchor-backfill", "--root", str(self.store), "--project", "p",
                           "--sessions-dir", str(self.sessions), "--code-root", str(self.code), *extra])

    def topic_file(self):
        return self.store / "topics" / "a.md"

    def setup_topic(self):
        self.add_topic("a", plain_topic(
            "TOP-9001", ["src/foo.py", "src/sub/", "src/*.py", "src/untouched.py", "src/foo.py#beta"]))
        self.code_file("src/foo.py", PY_SRC)
        self.code_file("src/untouched.py", "u\n")
        self.session("s1", [self.ledger_row("src/foo.py", "c" * 64)])

    def rows(self):
        fm = memidx.parse_record_text(self.topic_file().read_text(), "x").frontmatter
        return fm.get("anchors") or []

    def test_captures_labelled_backfill_from_the_file_not_the_ledger(self):
        self.setup_topic()
        rc, out, err = self.backfill()
        self.assertEqual(rc, 0, err)
        by_ref = {r["ref"]: r for r in self.rows()}
        self.assertEqual(sorted(by_ref), ["src/foo.py", "src/foo.py#beta"])
        file_row = by_ref["src/foo.py"]
        self.assertEqual(file_row["captured"], "backfill")
        self.assertEqual(file_row["digest"], sha(PY_SRC.encode()))
        self.assertNotEqual(file_row["digest"], "c" * 64)
        self.assertEqual(file_row["link"], "L1")
        self.assertEqual(file_row["code_head"], "unavailable")
        self.assertEqual(file_row["worktree"], "unknown")
        self.assertEqual(by_ref["src/foo.py#beta"]["span"], "symbol")
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)

    def test_prefix_glob_and_unledgered_refs_get_no_row(self):
        self.setup_topic()
        self.assertEqual(self.backfill()[0], 0)
        refs = {r["ref"] for r in self.rows()}
        self.assertNotIn("src/sub/", refs)
        self.assertNotIn("src/*.py", refs)
        self.assertNotIn("src/untouched.py", refs)

    def test_idempotent_second_run_changes_nothing(self):
        self.setup_topic()
        self.assertEqual(self.backfill()[0], 0)
        once = self.topic_file().read_bytes()
        rc, out, err = self.backfill()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.topic_file().read_bytes(), once)
        self.assertIn("0 anchor(s) captured", out)

    def test_dry_run_prints_the_diff_and_writes_nothing(self):
        self.setup_topic()
        before = self.topic_file().read_bytes()
        rc, out, err = self.backfill("--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.topic_file().read_bytes(), before)
        self.assertIn("+    captured: backfill", out)
        self.assertIn("would be captured", out)

    def test_lock_siblings_and_half_written_sessions_are_tolerated(self):
        self.setup_topic()
        (self.sessions / "live.json").write_text('{"ledger": [')
        (self.sessions / "live.json.lock").write_text("")
        rc, out, err = self.backfill()
        self.assertEqual(rc, 0, err)
        self.assertIn("live.json", err)
        self.assertNotIn("live.json.lock", err)
        self.assertEqual(len(self.rows()), 2)

    def test_every_session_file_counts(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/a.py", "src/b.py"]))
        self.code_file("src/a.py", "a\n")
        self.code_file("src/b.py", "b\n")
        self.session("s1", [self.ledger_row("src/a.py")])
        self.session("s2", [self.ledger_row("src/b.py")])
        self.assertEqual(self.backfill()[0], 0)
        self.assertEqual(sorted(r["ref"] for r in self.rows()), ["src/a.py", "src/b.py"])


class DeliveryCase(SessionCase):
    """For-path delivery fixtures: real bind -> mutate the code -> for-path."""

    def setUp(self):
        super().setUp()
        self.codedb = self.td / "p-code.sqlite"

    def bind(self, ref, tid="TOP-9001", *extra):
        rc, out, err = run_memidx(["bind", tid, ref, "--root", str(self.store),
                                   "--code-root", str(self.code), *extra])
        self.assertEqual(rc, 0, err)

    def code_reindex(self):
        rc, out, err = run_memidx(["code-reindex", "--code-root", str(self.code), "--project", "p",
                                   "--db", str(self.codedb), "--lang", "python", "--no-embed"])
        self.assertEqual(rc, 0, err)

    def for_path(self, cand, live=None, *extra, json_text=False, with_live=True):
        live = live if live is not None else str(self.code / cand)
        args = ["for-path", cand, "--project", "p", "--db", str(self.db), "--root", str(self.store)]
        if with_live:
            args += ["--live-file", live, "--code-db", str(self.codedb)]
        args += list(extra)
        if json_text:
            args += ["--json", "--with-chain-text"]
        rc, out, err = run_memidx(args)
        self.assertEqual(rc, 0, err)
        return out

    def lines(self, out):
        return out.splitlines()

    def anchor_lines(self, out):
        return [ln for ln in self.lines(out) if ln.startswith("Anchor ")]


class TestForPathAnchorDelivery(DeliveryCase):
    def start(self, refs=("src/foo.py",), src=PY_SRC):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.code_file("src/foo.py", src)
        for ref in refs:
            self.bind(ref)
        self.reindex()

    def test_unchanged_is_silent(self):
        self.start()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")), [])

    def test_changed_file_names_the_capture_kind_right_after_the_header(self):
        self.start()
        self.code_file("src/foo.py", PY_SRC + "# edit\n")
        self.reindex()
        out = self.for_path("src/foo.py")
        lines = self.lines(out)
        self.assertTrue(lines[0].startswith("TOP-9001 Alpha"))
        self.assertEqual(lines[1], "Anchor changed since capture (bind): src/foo.py")
        self.assertTrue(lines[2].lstrip().startswith("L1"), lines[:3])

    def test_status_and_authority_strings_are_unchanged_by_the_annotation(self):
        self.start()
        plain = self.for_path("src/foo.py", with_live=False)
        self.code_file("src/foo.py", "different\n")
        annotated = self.for_path("src/foo.py")
        self.assertEqual([ln for ln in annotated.splitlines() if not ln.startswith("Anchor ")],
                         plain.splitlines())

    def test_json_chain_text_is_the_same_text_as_plain_mode(self):
        self.start()
        self.code_file("src/foo.py", "different\n")
        plain = self.for_path("src/foo.py")
        env = json.loads(self.for_path("src/foo.py", json_text=True))
        self.assertEqual(env["chain_text"], plain.rstrip("\n"))
        self.assertIn("Anchor changed since capture", env["chain_text"])

    def test_without_live_file_nothing_changes(self):
        self.start()
        self.code_file("src/foo.py", "different\n")
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py", with_live=False)), [])

    def test_missing_file_gone(self):
        self.start()
        out = self.for_path("src/foo.py", live=str(self.code / "src" / "gone.py"))
        self.assertIn("Anchor missing: src/foo.py (file gone).", out)

    def test_never_captured(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.reindex()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")),
                         ["Anchor never captured: src/foo.py."])

    def test_prefix_and_glob_refs_are_not_checked(self):
        for ref, text in (("src/", "Anchor not checked: src/ is a prefix or glob."),
                          ("src/*.py", "Anchor not checked: src/*.py is a prefix or glob.")):
            with self.subTest(ref=ref):
                (self.store / "topics" / "a.md").write_text(plain_topic("TOP-9001", [ref], "Alpha"))
                self.code_file("src/foo.py", PY_SRC)
                self.reindex()
                self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")), [text])

    def test_priority_changed_beats_never_and_more_counts_same_status_refs(self):
        self.add_topic("a", plain_topic(
            "TOP-9001", ["src/foo.py", "src/foo.py#beta", "src/foo.py#alpha", "src/foo.py#nope2"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py")
        self.bind("src/foo.py#beta")
        self.code_file("src/foo.py", PY_SRC + "# edit\n")
        self.reindex()
        # the file row changed; the #beta row would be missing (no code index) and
        # two refs were never captured -- changed outranks both
        out = self.for_path("src/foo.py")
        lines = self.anchor_lines(out)
        self.assertEqual(lines, ["Anchor changed since capture (bind): src/foo.py"])
        self.code_file("src/foo.py", PY_SRC)   # file row now matches again; #beta needs the index
        out = self.for_path("src/foo.py")
        self.assertEqual(self.anchor_lines(out),
                         ["Anchor missing: src/foo.py#beta (snapshot unavailable)."])

    def test_more_suffix_counts_other_refs_of_the_same_status(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py", "src/foo.py#zzz"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py")
        self.bind("src/foo.py#zzz")        # zero matches -> captured as a file-span row
        self.code_file("src/foo.py", "changed\n")
        self.reindex()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")),
                         ["Anchor changed since capture (bind): src/foo.py (+1 more)"])

    def test_backfill_rows_add_the_late_capture_sentence(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.session("s1", [self.ledger_row("src/foo.py")])
        rc, out, err = run_memidx(["anchor-backfill", "--root", str(self.store), "--project", "p",
                                   "--sessions-dir", str(self.sessions), "--code-root", str(self.code)])
        self.assertEqual(rc, 0, err)
        self.code_file("src/foo.py", "changed\n")
        self.reindex()
        self.assertEqual(
            self.anchor_lines(self.for_path("src/foo.py")),
            ["Anchor changed since capture (backfill): src/foo.py Captured at backfill 2026-11-02; "
             "original ruling bytes unknown."])

    def test_budget_exhausted_still_delivers_the_ruling(self):
        self.start()
        with mock.patch.object(memidx, "ANCHOR_BUDGET_SECONDS", -1.0):
            out = self.for_path("src/foo.py")
        self.assertIn("Anchor missing: src/foo.py (check budget exhausted).", out)
        self.assertIn("a ruling", out)


class TestAnchorBudgetBoundsTheWork(DeliveryCase):
    """The deadline is checked before every read, hash, query and parse, so a
    clock that crosses it AFTER work began still ends in the exhausted header."""

    def test_clock_crosses_after_the_first_file_read(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        live = self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py")
        self.reindex()
        now = {"t": 0.0}
        real_read = Path.read_bytes

        def read_bytes(path):
            data = real_read(path)
            if str(path) == live:
                now["t"] = 100.0       # the read itself blew the 300 ms budget
            return data

        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(Path, "read_bytes", read_bytes):
            out = self.for_path("src/foo.py")
        self.assertEqual(self.anchor_lines(out), ["Anchor missing: src/foo.py (check budget exhausted)."])
        self.assertIn("a ruling", out)
        self.assertTrue(out.splitlines()[0].startswith("TOP-9001 Alpha"))

    def test_clock_crosses_between_opening_the_code_index_and_querying_it(self):
        row = {"ref": "src/foo.py#beta", "span": "symbol", "digest": "x" * 64}
        now = {"t": 0.0}
        checker = memidx.AnchorChecker("p", lambda _p: PY_SRC.encode(), code_db=self.codedb,
                                       deadline=50.0)
        conn = object()

        def db():
            now["t"] = 100.0
            return conn

        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(checker, "_db", db):
            self.assertEqual(checker.check(row), ("missing", "check budget exhausted"))

    def test_standing_counts_topics_left_unchecked(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.reindex()
        conn = memidx.open_db_noncreating(self.db, project="p")
        try:
            with mock.patch.object(memidx, "ANCHOR_BUDGET_SECONDS", -1.0):
                summary = memidx.standing_anchor_summary(conn, "p", ["TOP-9001"], [self.code])
        finally:
            conn.close()
        self.assertEqual(summary.get("unchecked"), 1)
        self.assertEqual(summary["never"], 0)


class TestForPathSymbolAnchors(DeliveryCase):
    def start(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py#beta")
        self.reindex()
        self.code_reindex()

    def test_unchanged_symbol_is_silent(self):
        self.start()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")), [])

    def test_changed_symbol_body(self):
        self.start()
        self.code_file("src/foo.py", PY_SRC.replace("x = 2", "x = 3"))
        self.code_reindex()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")),
                         ["Anchor changed since capture (bind): src/foo.py#beta"])

    def test_edit_elsewhere_in_the_file_leaves_the_symbol_anchor_silent(self):
        self.start()
        self.code_file("src/foo.py", PY_SRC.replace("return 1", "return 11"))
        self.code_reindex()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")), [])

    def test_symbol_absent(self):
        self.start()
        self.code_file("src/foo.py", "def alpha():\n    return 1\n")
        self.code_reindex()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")),
                         ["Anchor missing: src/foo.py#beta (symbol absent)."])

    def test_symbol_ambiguous(self):
        self.start()
        self.code_file("src/foo.py", PY_AMBIGUOUS + "\ndef beta():\n    return 1\n\nclass C:\n    def beta(self):\n        return 2\n")
        self.code_reindex()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")),
                         ["Anchor missing: src/foo.py#beta (symbol ambiguous)."])

    def test_snapshot_unavailable_without_a_code_index(self):
        self.start()
        self.codedb.unlink()
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")),
                         ["Anchor missing: src/foo.py#beta (snapshot unavailable)."])

    def test_index_stale_when_the_file_moved_after_indexing(self):
        self.start()
        self.code_file("src/foo.py", PY_SRC + "\n# moved on, index not refreshed\n")
        self.assertEqual(self.anchor_lines(self.for_path("src/foo.py")),
                         ["Anchor missing: src/foo.py#beta (index stale)."])

    def test_delivery_is_read_only_and_chunker_free(self):
        self.start()
        self.code_file("src/foo.py", PY_SRC.replace("x = 2", "x = 3"))
        self.code_reindex()
        before = {p.name: p.read_bytes() for p in self.td.glob("p-code.sqlite*")}
        opened = []
        real_connect = memidx.sqlite3.connect

        def spy(target, *a, **kw):
            opened.append(str(target))
            return real_connect(target, *a, **kw)

        def boom(*a, **kw):
            raise AssertionError("delivery touched a chunker / writable code index")

        with mock.patch.object(memidx.sqlite3, "connect", spy), \
                mock.patch.object(memidx.chunkers, "get_chunker", boom), \
                mock.patch.object(memidx, "validate_chunk_result", boom), \
                mock.patch.object(memidx, "open_code_db", boom), \
                mock.patch.object(memidx, "code_index_report", boom):
            out = self.for_path("src/foo.py")
        self.assertIn("Anchor changed since capture", out)
        code_opens = [o for o in opened if "p-code.sqlite" in o]
        self.assertTrue(code_opens)
        for target in code_opens:
            self.assertTrue(target.startswith("file:") and "mode=ro" in target, target)
        after = {p.name: p.read_bytes() for p in self.td.glob("p-code.sqlite*")}
        self.assertEqual(before, after)


class TestStandingAnchorSummary(DeliveryCase):
    """`standing --json` carries anchor_summary / anchor_line; the digest text,
    its hash and the 4800-byte refusal are untouched."""

    def standing_topic(self, text="stands", refs=("src/foo.py",)):
        refs_block = "".join(f"  - {r}\n" for r in refs)
        return (
            "---\ntype: topic\nid: TOP-9900\ntitle: Standing\narea: testing\ncurrent: L1\n"
            "standing: [L1]\ntags: []\n"
            f"code_refs:\n{refs_block}"
            "links:\n  - link: L1\n    date: '2026-01-01'\n    status: active\n    kind: adopted\n"
            f"    ruling: {{text: {json.dumps(text)}, authority: owner-verbatim, source: s}}\n"
            "---\n\nBody.\n"
        )

    def standing(self, *extra, roots=True):
        args = ["standing", "--project", "p", "--db", str(self.db), "--root", str(self.store),
                "--code-db", str(self.codedb), "--json"]
        if roots:
            args += ["--code-root", str(self.code)]
        rc, out, err = run_memidx(args + list(extra))
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def setup_store(self, topic_text):
        self.add_topic("s", topic_text)
        self.code_file("src/foo.py", PY_SRC)
        self.reindex()

    def test_counts_and_line_for_each_status(self):
        self.setup_store(self.standing_topic(refs=("src/foo.py", "src/", "src/gone.py")))
        out = self.standing()
        self.assertEqual(out["anchor_summary"], {"changed": 0, "missing": 0, "never": 2, "not_checked": 1})
        self.assertEqual(out["anchor_line"], "Anchor never captured: 2.")
        self.bind("src/foo.py", "TOP-9900")
        self.reindex()
        self.code_file("src/foo.py", "edited\n")
        out = self.standing()
        self.assertEqual(out["anchor_summary"]["changed"], 1)
        self.assertEqual(out["anchor_line"], "Anchor changed since capture: 1.")

    def test_missing_outranks_never(self):
        self.setup_store(self.standing_topic(refs=("src/foo.py", "src/bar.py")))
        self.bind("src/foo.py", "TOP-9900")
        self.reindex()
        (self.code / "src" / "foo.py").unlink()
        out = self.standing()
        self.assertEqual(out["anchor_line"], "Anchor missing: 1.")

    def test_no_code_root_skips_the_summary(self):
        self.setup_store(self.standing_topic())
        out = self.standing(roots=False)
        self.assertEqual(out["anchor_summary"], {"skipped": "no-root"})
        self.assertEqual(out["anchor_line"], "")

    def test_digest_and_hash_are_the_same_with_and_without_roots(self):
        self.setup_store(self.standing_topic())
        with_roots, without = self.standing(), self.standing(roots=False)
        for key in ("hash", "links", "bytes", "lines"):
            self.assertEqual(with_roots[key], without[key])

    def test_plain_output_is_untouched(self):
        self.setup_store(self.standing_topic())
        rc, out, err = run_memidx(["standing", "--project", "p", "--db", str(self.db),
                                   "--root", str(self.store), "--code-root", str(self.code)])
        self.assertEqual(rc, 0, err)
        self.assertNotIn("Anchor", out)

    def test_the_line_is_dropped_before_the_digest_when_the_cap_bites(self):
        cap = memidx.STANDING_CAP_BYTES
        n = 1
        while True:
            line = memidx.standing_line("TOP-9900", "L1", "owner-verbatim", "x" * n)
            size = len(memidx.standing_digest_text([line]).encode())
            if size > cap - 10:
                break
            n += 1
        self.assertLessEqual(size, cap)
        self.setup_store(self.standing_topic(text="x" * n))
        out = self.standing()
        self.assertEqual(out["bytes"], size)
        self.assertEqual(out["anchor_line"], "")
        self.assertEqual(out["anchor_summary"].get("dropped"), "cap")
        self.assertEqual(len(out["lines"]), 1)

    def test_standing_is_read_only_and_chunker_free(self):
        self.setup_store(self.standing_topic(refs=("src/foo.py#beta",)))
        self.bind("src/foo.py#beta", "TOP-9900")
        self.reindex()
        self.code_reindex()

        def boom(*a, **kw):
            raise AssertionError("standing touched a chunker / writable code index")

        with mock.patch.object(memidx.chunkers, "get_chunker", boom), \
                mock.patch.object(memidx, "validate_chunk_result", boom), \
                mock.patch.object(memidx, "open_code_db", boom):
            out = self.standing()
        self.assertEqual(out["anchor_summary"]["changed"] + out["anchor_summary"]["missing"]
                         + out["anchor_summary"]["never"], 0)


class TestContextPackAnchors(DeliveryCase):
    def pack(self, *args, roots=True, cap=None):
        argv = ["context-pack", *args, "--project", "p", "--db", str(self.db), "--root", str(self.store),
                "--code-db", str(self.codedb)]
        if roots:
            argv += ["--code-root", str(self.code)]
        if cap:
            argv += ["--max-bytes", str(cap)]
        rc, out, err = run_memidx(argv)
        self.assertEqual(rc, 0, err)
        return out

    def two_topics(self):
        self.add_topic("x", plain_topic("TOP-9701", ["src/x.py"], "Changed one"))
        self.add_topic("y", plain_topic("TOP-9702", ["src/y.py"], "Never one"))
        self.code_file("src/x.py", "x\n")
        self.code_file("src/y.py", "y\n")
        self.bind("src/x.py", "TOP-9701")
        self.code_file("src/x.py", "x edited\n")
        self.reindex()

    def test_line_sits_between_the_record_file_line_and_the_chain_header(self):
        self.two_topics()
        lines = self.pack("src/x.py", "src/y.py").splitlines()
        i = next(n for n, l in enumerate(lines) if l.startswith("TOP-9701 Changed one"))
        self.assertEqual(lines[i - 1], "Anchor changed since capture (bind): src/x.py")
        self.assertTrue(lines[i - 2].startswith("[record file:"))
        j = next(n for n, l in enumerate(lines) if l.startswith("TOP-9702 Never one"))
        self.assertEqual(lines[j - 1], "Anchor never captured: src/y.py.")

    def test_no_code_root_leaves_the_pack_exactly_as_before(self):
        self.two_topics()
        plain = self.pack("src/x.py", "src/y.py", roots=False)
        self.assertNotIn("Anchor ", plain)

    def test_everything_else_in_the_pack_is_unchanged(self):
        self.two_topics()
        plain = self.pack("src/x.py", "src/y.py", roots=False).splitlines()
        annotated = self.pack("src/x.py", "src/y.py").splitlines()
        stripped = [l for l in annotated if not l.startswith("Anchor ")]
        # only the hash line (it covers the extra bytes) differs
        self.assertEqual(stripped[:-1], plain[:-1])
        self.assertNotEqual(stripped[-1], plain[-1])

    def test_annotation_bytes_are_inside_the_hash_and_reproducible(self):
        self.two_topics()
        first = self.pack("src/x.py", "src/y.py")
        self.assertEqual(self.pack("src/x.py", "src/y.py"), first)
        text, _, last = first.rstrip("\n").rpartition("\n")
        self.assertEqual(last, memidx.pack_hash_line(text + "\n")[2])
        self.code_file("src/x.py", "x edited again\n")
        changed_line = self.pack("src/x.py", "src/y.py").splitlines()[-1]
        self.assertEqual(changed_line, last)   # still "changed": same bytes, same hash
        self.code_file("src/x.py", "x\n")      # back to the captured bytes -> the line disappears
        self.assertNotEqual(self.pack("src/x.py", "src/y.py").splitlines()[-1], last)

    def test_cut_order_drops_never_captured_before_changed_and_never_a_topic(self):
        self.two_topics()
        base = self.pack("src/x.py", "src/y.py", roots=False)
        base_bytes = len(base.rsplit("pack sha256=", 1)[0].encode())
        changed_cost = len(("Anchor changed since capture (bind): src/x.py\n").encode())
        out = self.pack("src/x.py", "src/y.py", cap=base_bytes + changed_cost)
        self.assertIn("Anchor changed since capture (bind): src/x.py", out)
        self.assertNotIn("Anchor never captured", out)
        self.assertIn("TOP-9702 Never one", out)       # no topic was lost to make room
        self.assertNotIn("omitted by the size cap", out)
        tight = self.pack("src/x.py", "src/y.py", cap=base_bytes)
        self.assertNotIn("Anchor ", tight)

    def test_two_changed_annotations_with_room_for_one_keep_one(self):
        self.add_topic("x", plain_topic("TOP-9701", ["src/x.py"], "Changed one"))
        self.add_topic("z", plain_topic("TOP-9703", ["src/z.py"], "Changed two"))
        for name in ("x", "z"):
            self.code_file(f"src/{name}.py", f"{name}\n")
        self.bind("src/x.py", "TOP-9701")
        self.bind("src/z.py", "TOP-9703")
        for name in ("x", "z"):
            self.code_file(f"src/{name}.py", f"{name} edited\n")
        self.reindex()
        base = self.pack("src/x.py", "src/z.py", roots=False)
        base_bytes = len(base.rsplit("pack sha256=", 1)[0].encode())
        one_line = len("Anchor changed since capture (bind): src/x.py\n".encode())
        out = self.pack("src/x.py", "src/z.py", cap=base_bytes + one_line)
        kept = [ln for ln in out.splitlines() if ln.startswith("Anchor changed")]
        self.assertEqual(len(kept), 1, out)
        self.assertIn("TOP-9701 Changed one", out)
        self.assertIn("TOP-9703 Changed two", out)

    def test_cut_order_never_keeps_a_lower_priority_line_the_higher_one_could_not_fit(self):
        """Sol's probe: the changed line is the long one; a cap with room for the short
        never-captured line only must not admit it -- never captured is cut BEFORE
        changed, so nothing is annotated."""
        self.two_topics()
        base = self.pack("src/x.py", "src/y.py", roots=False)
        base_bytes = len(base.rsplit("pack sha256=", 1)[0].encode())
        never_cost = len("Anchor never captured: src/y.py.\n".encode())
        changed_cost = len("Anchor changed since capture (bind): src/x.py\n".encode())
        self.assertLess(never_cost, changed_cost)
        out = self.pack("src/x.py", "src/y.py", cap=base_bytes + never_cost)
        self.assertNotIn("Anchor ", out)
        self.assertIn("TOP-9702 Never one", out)

    def test_pack_is_read_only_and_chunker_free(self):
        self.add_topic("a", plain_topic("TOP-9703", ["src/foo.py"], "Sym"))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py#beta", "TOP-9703")
        self.reindex()
        self.code_reindex()
        self.code_file("src/foo.py", PY_SRC.replace("x = 2", "x = 9"))
        self.code_reindex()

        def boom(*a, **kw):
            raise AssertionError("context-pack touched a chunker / writable code index")

        with mock.patch.object(memidx.chunkers, "get_chunker", boom), \
                mock.patch.object(memidx, "validate_chunk_result", boom), \
                mock.patch.object(memidx, "open_code_db", boom):
            out = self.pack("src/foo.py")
        self.assertIn("Anchor changed since capture (bind): src/foo.py#beta", out)


def only_insertions(testcase, before: bytes, after: bytes):
    """Every line of `before` survives byte-for-byte and in order in
    `after`; `after` differs by inserted lines only."""
    import difflib
    a = re.findall(rb"[^\n]*\n|[^\n]+", before)
    b = re.findall(rb"[^\n]*\n|[^\n]+", after)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    bad = [op for op in ops if op[0] not in ("equal", "insert")]
    testcase.assertEqual(bad, [], f"existing bytes changed: {bad}")


class TestGateRound1Lint(unittest.TestCase):
    def test_captured_at_is_required(self):
        row = good_row()
        del row["captured_at"]
        errors, _ = lint_text(topic_text([row]))
        self.assertTrue(any("captured_at is required" in e for e in errors), errors)

    def test_baseline_without_captured_at_cannot_change_digest(self):
        row = good_row()
        del row["captured_at"]
        with tempfile.TemporaryDirectory() as td:
            root = git_store(td)
            (root / "topics" / "foo.md").write_text(topic_text([row]))
            commit_all(root)
            changed = good_row(digest=f'"{DIGEST2}"')
            del changed["captured_at"]
            (root / "topics" / "foo.md").write_text(topic_text([changed]))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("lacks an identity component", out)
            self.assertIn("anchor ('src/foo.py', 'L1')", out)

    def test_digest_with_trailing_newline_is_rejected(self):
        errors, _ = lint_text(topic_text([good_row(digest=f'"{DIGEST}\\n"')]))
        self.assertTrue(any("digest must be 64" in e for e in errors), errors)


class TestGateRound1Append(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.code = make_code_root(self._td.name, {"src/foo.py": PY_SRC, "src/bar.py": "b\n"})
        self.store = git_store(Path(self._td.name) / "store")
        patcher = mock.patch.object(memidx, "_anchor_today", return_value="2026-11-02")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _append_after(self, between):
        block = anchor_block([good_row(ref="src/foo.py")]) + between
        (self.store / "topics" / "foo.md").write_text(
            topic_text(anchors_text=block, code_refs=("src/foo.py",)))
        commit_all(self.store)
        rc, out, err = run_memidx(["bind", "TOP-1", "src/bar.py", "--root", str(self.store),
                                   "--code-root", str(self.code)])
        self.assertEqual(rc, 0, err)
        rc, lint = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, lint)
        text = (self.store / "topics" / "foo.md").read_text()
        self.assertLess(text.index("ref: \"src/bar.py\""), text.index("links:"))
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)

    def test_blank_line_between_last_anchor_and_links(self):
        self._append_after("\n")

    def test_comment_line_between_last_anchor_and_links(self):
        self._append_after("# a note about the anchors\n")

    def test_blank_and_comment_lines(self):
        self._append_after("\n# note\n\n")


class TestGateRound1ByteWriters(SessionCase):
    def _crlf_topic(self, name, refs):
        text = plain_topic("TOP-9001", refs).replace("kind: adopted\n", "kind: adopted   \n")
        raw = text.replace("\n", "\r\n").encode()
        (self.store / "topics" / f"{name}.md").write_bytes(raw)
        return raw

    def test_bind_preserves_crlf_and_trailing_whitespace(self):
        before = self._crlf_topic("a", ["src/other.py"])
        self.code_file("src/foo.py", PY_SRC)
        rc, out, err = run_memidx(["bind", "TOP-9001", "src/foo.py", "--root", str(self.store),
                                   "--code-root", str(self.code)])
        self.assertEqual(rc, 0, err)
        after = (self.store / "topics" / "a.md").read_bytes()
        only_insertions(self, before, after)
        self.assertNotRegex(after, rb"(?<!\r)\n")
        self.assertIn(b"kind: adopted   \r\n", after)
        self.assertIn(b'ref: "src/foo.py"\r\n', after)

    def test_backfill_preserves_crlf_and_trailing_whitespace(self):
        before = self._crlf_topic("a", ["src/foo.py"])
        self.code_file("src/foo.py", PY_SRC)
        self.session("s1", [self.ledger_row("src/foo.py")])
        rc, out, err = run_memidx(["anchor-backfill", "--root", str(self.store), "--project", "p",
                                   "--sessions-dir", str(self.sessions), "--code-root", str(self.code)])
        self.assertEqual(rc, 0, err)
        after = (self.store / "topics" / "a.md").read_bytes()
        self.assertNotEqual(after, before)
        only_insertions(self, before, after)
        self.assertNotRegex(after, rb"(?<!\r)\n")
        self.assertIn(b"kind: adopted   \r\n", after)

    def test_backfill_claims_no_capture_when_validation_fails(self):
        raw = plain_topic("TOP-9001", ["src/foo.py"]).encode()
        (self.store / "topics" / "a.md").write_bytes(raw)
        self.code_file("src/foo.py", PY_SRC)
        self.session("s1", [self.ledger_row("src/foo.py")])
        with mock.patch.object(memidx, "bound_edit_problem", return_value="the edit would not parse cleanly"):
            rc, out, err = run_memidx(["anchor-backfill", "--root", str(self.store), "--project", "p",
                                       "--sessions-dir", str(self.sessions), "--code-root", str(self.code)])
        self.assertEqual(rc, 1)
        self.assertNotIn("captured as backfill", out)
        self.assertIn("0 anchor(s) captured", out)
        self.assertEqual((self.store / "topics" / "a.md").read_bytes(), raw)

    def test_bind_refuses_invalid_utf8(self):
        raw = plain_topic("TOP-9001", []).encode() + b"\xff\xfe tail\n"
        (self.store / "topics" / "a.md").write_bytes(raw)
        self.code_file("src/foo.py", PY_SRC)
        rc, out, err = run_memidx(["bind", "TOP-9001", "src/foo.py", "--root", str(self.store),
                                   "--code-root", str(self.code)])
        self.assertEqual(rc, 1)
        self.assertIn("not valid UTF-8", err)
        self.assertEqual((self.store / "topics" / "a.md").read_bytes(), raw)

    def test_backfill_refuses_invalid_utf8(self):
        raw = plain_topic("TOP-9001", ["src/foo.py"]).encode() + b"\xff\xfe tail\n"
        (self.store / "topics" / "a.md").write_bytes(raw)
        self.code_file("src/foo.py", PY_SRC)
        self.session("s1", [self.ledger_row("src/foo.py")])
        rc, out, err = run_memidx(["anchor-backfill", "--root", str(self.store), "--project", "p",
                                   "--sessions-dir", str(self.sessions), "--code-root", str(self.code)])
        self.assertEqual(rc, 1)
        self.assertIn("not valid UTF-8", err)
        self.assertEqual((self.store / "topics" / "a.md").read_bytes(), raw)


class TestGateRound1QuotedRefs(SessionCase):
    def test_yaml_typed_names_bind_and_lint_clean(self):
        for name in ("yes", "on", "null", "1.0"):
            with self.subTest(name=name):
                (self.store / "topics" / "a.md").write_text(plain_topic("TOP-9001", []))
                self.code_file(name, "content of %s\n" % name)
                rc, out, err = run_memidx(["bind", "TOP-9001", name, "--root", str(self.store),
                                           "--code-root", str(self.code)])
                self.assertEqual(rc, 0, err)
                fm = memidx.parse_record_text((self.store / "topics" / "a.md").read_text(), "x").frontmatter
                self.assertEqual(fm["code_refs"], [name])
                self.assertEqual(fm["anchors"][0]["ref"], name)
                errors, _ = memlint.lint_root(self.store)
                self.assertEqual(errors, [], errors)

    def test_symbol_ref_with_hash_binds_and_lints_clean(self):
        (self.store / "topics" / "a.md").write_text(plain_topic("TOP-9001", []))
        self.code_file("src/foo.py", PY_SRC)
        rc, out, err = run_memidx(["bind", "TOP-9001", "src/foo.py#beta", "--root", str(self.store),
                                   "--code-root", str(self.code)])
        self.assertEqual(rc, 0, err)
        fm = memidx.parse_record_text((self.store / "topics" / "a.md").read_text(), "x").frontmatter
        self.assertEqual(fm["code_refs"], ["src/foo.py#beta"])
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)

    def test_round_trip_check_refuses_a_retyped_reference(self):
        text = plain_topic("TOP-9001", []).replace("code_refs: []", "code_refs:\n  - yes")
        problem = memidx.bound_edit_problem(text, "x.md", "yes", "L1", ["yes"])
        self.assertIn("round-trip", problem or "")


class TestLinkRawSpansUnchanged(unittest.TestCase):
    def test_default_key_walks_links(self):
        text = topic_text([good_row()])
        fm = memidx.parse_record_text(text, "x").frontmatter
        spans = memlint._link_raw_spans(text, fm["links"])
        self.assertEqual(list(spans), ["L1"])
        self.assertTrue(spans["L1"].startswith("  - link: L1"))

    def test_anchors_key_walks_anchor_rows(self):
        text = topic_text([good_row(), good_row(ref="src/b.py")], code_refs=("src/foo.py", "src/b.py"))
        fm = memidx.parse_record_text(text, "x").frontmatter
        spans = memlint._link_raw_spans(
            text, fm["anchors"], key="anchors", ident=memlint._anchor_ident
        )
        self.assertEqual(len(spans), 2)
        for span in spans.values():
            self.assertNotIn("links:", span)


if __name__ == "__main__":
    unittest.main()


class TestGateRound2Crlf(unittest.TestCase):
    """Item 1: a CRLF record at REF is freezable (links and anchors), judged on
    the original bytes; an LF twin behaves the same."""

    def _text(self, rows, crlf):
        # gate round 11 R11-2: a row added since REF meets the complete anchor validator, so the
        # appended row's ref (src/bar.py) must be in code_refs on BOTH sides
        text = topic_text(rows, code_refs=("src/foo.py", "src/bar.py"))
        return text.replace("\n", "\r\n") if crlf else text

    def _run(self, crlf, new_text_fn):
        with tempfile.TemporaryDirectory() as td:
            root = git_store(td)
            path = root / "topics" / "foo.md"
            path.write_bytes(self._text([good_row()], crlf).encode())
            commit_all(root)
            path.write_bytes(new_text_fn(crlf).encode())
            return run_memlint(["--against-ref", "HEAD", str(root)])

    def test_unchanged_history_passes_with_no_note(self):
        for crlf in (True, False):
            rc, out = self._run(crlf, lambda c: self._text([good_row()], c))
            self.assertEqual(rc, 0, out)
            self.assertNotIn("NOTE", out)

    def test_appended_row_passes(self):
        for crlf in (True, False):
            rc, out = self._run(crlf, lambda c: self._text(
                [good_row(), good_row(ref="src/bar.py", captured_at="2026-11-03")], c))
            self.assertEqual(rc, 0, out)
            self.assertNotIn("NOTE", out)

    def test_changed_digest_errors(self):
        for crlf in (True, False):
            rc, out = self._run(crlf, lambda c: self._text([good_row(digest=f'"{DIGEST2}"')], c))
            self.assertEqual(rc, 1, (crlf, out))
            self.assertIn("anchor ('src/foo.py', 'L1')", out)

    def test_deleted_anchor_row_errors(self):
        for crlf in (True, False):
            rc, out = self._run(crlf, lambda c: self._text([], c))
            self.assertEqual(rc, 1, (crlf, out))
            self.assertIn("anchor ('src/foo.py', 'L1')", out)

    def test_crlf_to_lf_rewrite_errors_on_every_frozen_span(self):
        rc, out = self._run(True, lambda c: self._text([good_row()], False))
        self.assertEqual(rc, 1, out)
        self.assertIn("anchor ('src/foo.py', 'L1')", out)
        self.assertIn("anchor", out)
        self.assertIn("reformatted", out)
        self.assertIn("L1", out)


class TestGateRound2Deadline(DeliveryCase):
    """Item 2: the budget is checked before AND after every expensive step and
    a verdict is accepted only if the clock has not passed after the step."""

    def _clocked(self, now, hashed):
        real = memidx.sha256_hex

        def counting(data):
            hashed.append(1)
            return real(data)
        return counting

    def test_crossing_after_the_code_index_open_never_hashes(self):
        row = {"ref": "src/foo.py#beta", "span": "symbol", "digest": "x" * 64}
        now, hashed = {"t": 0.0}, []
        checker = memidx.AnchorChecker("p", lambda _p: PY_SRC.encode(), code_db=self.codedb,
                                       deadline=50.0)

        def db():
            now["t"] = 100.0
            return object()

        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(memidx, "sha256_hex", self._clocked(now, hashed)), \
                mock.patch.object(checker, "_db", db):
            self.assertEqual(checker.check(row), ("missing", "check budget exhausted"))
        self.assertEqual(hashed, [])

    def test_crossing_on_the_final_file_hash_is_exhausted_not_ok(self):
        data = b"hello\n"
        row = {"ref": "src/foo.py", "span": "file", "digest": hashlib.sha256(data).hexdigest()}
        now = {"t": 0.0}
        real = memidx.sha256_hex

        def slow_hash(b):
            out = real(b)
            now["t"] = 100.0       # the hash itself blew the budget
            return out

        checker = memidx.AnchorChecker("p", lambda _p: data, deadline=50.0)
        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(memidx, "sha256_hex", slow_hash):
            self.assertEqual(checker.check(row), ("missing", "check budget exhausted"))
        now["t"] = 0.0
        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]):
            self.assertEqual(checker.check(row), ("ok", ""))

    def test_crossing_on_the_final_symbol_hash_is_exhausted(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py#beta")
        self.reindex()
        self.code_reindex()
        topic = memidx.parse_record(next((self.store / "topics").glob("*.md"))).frontmatter
        row = memidx.anchor_rows(topic)[0]
        now = {"t": 0.0}
        real = memidx.sha256_hex
        calls = []

        def slow_hash(b):
            out = real(b)
            calls.append(1)
            if len(calls) == 2:      # the span hash (the first is the whole file)
                now["t"] = 100.0
            return out

        data = (self.code / "src/foo.py").read_bytes()
        checker = memidx.AnchorChecker("p", lambda _p: data, code_db=self.codedb, deadline=50.0)
        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(memidx, "sha256_hex", slow_hash):
            self.assertEqual(checker.check(row), ("missing", "check budget exhausted"))
        checker.close()
        self.assertEqual(len(calls), 2)

    def test_standing_crossing_after_the_sql_lookup_counts_the_topic_unchecked(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.reindex()
        conn = memidx.open_db_noncreating(self.db, project="p")
        now = {"t": 0.0}
        parsed = []

        class Proxy:
            def execute(self_inner, *a, **k):
                out = conn.execute(*a, **k)
                now["t"] = 100.0
                return out

        real_parse = memidx.parse_record
        try:
            with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                    mock.patch.object(memidx, "parse_record",
                                      lambda p: parsed.append(p) or real_parse(p)):
                summary = memidx.standing_anchor_summary(Proxy(), "p", ["TOP-9001"], [self.code])
        finally:
            conn.close()
        self.assertEqual(summary.get("unchecked"), 1)
        self.assertEqual(parsed, [])
        self.assertEqual(summary["never"], 0)


class TestGateRound3WallClock(DeliveryCase):
    """A thread bound, not just clock checks between steps: a step that blocks
    (a slow query, a huge read) cannot hold the call past the budget."""

    SLACK = 0.3

    def _bound_standing(self):
        self.add_topic("s", TestStandingAnchorSummary.standing_topic(None, refs=("src/foo.py#beta",)))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py#beta", "TOP-9900")
        self.reindex()
        self.code_reindex()

    def _bound(self, symbol=True):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py#beta" if symbol else "src/foo.py")
        self.reindex()
        self.code_reindex()

    @staticmethod
    def _slow_db(seconds):
        real = memidx.AnchorChecker._db

        def slow(self_inner):
            time.sleep(seconds)
            return real(self_inner)
        return slow

    def test_for_path_returns_within_budget_when_the_index_is_slow(self):
        self._bound()
        with mock.patch.object(memidx.AnchorChecker, "_db", self._slow_db(1.5)):
            start = time.monotonic()
            out = self.for_path("src/foo.py")
            elapsed = time.monotonic() - start
        print(f"[round3 for-path slow _db 1.5s -> returned in {elapsed:.3f}s]")
        self.assertLess(elapsed, memidx.ANCHOR_BUDGET_SECONDS + self.SLACK + 0.5)
        self.assertLess(elapsed, 1.4)
        self.assertEqual(self.anchor_lines(out), ["Anchor missing: src/foo.py (check budget exhausted)."])
        self.assertIn("a ruling", out)
        self.assertTrue(out.splitlines()[0].startswith("TOP-9001 Alpha"))

    def test_standing_counts_unchecked_when_the_index_is_slow(self):
        self._bound_standing()
        conn = memidx.open_db_noncreating(self.db, project="p")
        try:
            with mock.patch.object(memidx.AnchorChecker, "_db", self._slow_db(1.5)):
                start = time.monotonic()
                summary = memidx.standing_anchor_summary(
                    conn, "p", ["TOP-9900"], [self.code], code_db=self.codedb)
                elapsed = time.monotonic() - start
        finally:
            conn.close()
        print(f"[round3 standing slow _db 1.5s -> returned in {elapsed:.3f}s]")
        self.assertLess(elapsed, 1.4)
        self.assertEqual(summary.get("unchecked"), 1)

    def test_standing_digest_is_intact_when_the_index_is_slow(self):
        self._bound_standing()
        with mock.patch.object(memidx.AnchorChecker, "_db", self._slow_db(1.5)):
            start = time.monotonic()
            rc, out, err = run_memidx(["standing", "--project", "p", "--db", str(self.db), "--json",
                                       "--root", str(self.store),
                                       "--code-root", str(self.code), "--code-db", str(self.codedb)])
            elapsed = time.monotonic() - start
        self.assertEqual(rc, 0, err)
        self.assertLess(elapsed, 1.4)
        obj = json.loads(out)
        self.assertEqual(obj["anchor_summary"].get("unchecked"), 1)
        self.assertEqual(obj["links"], 1)
        self.assertIn("stands", json.dumps(obj))

    def test_a_running_query_is_interrupted_and_reads_as_exhausted(self):
        row = {"ref": "src/foo.py#beta", "span": "symbol", "digest": "x" * 64}
        self.code_file("src/foo.py", PY_SRC)
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.bind("src/foo.py#beta")
        self.reindex()
        self.code_reindex()
        checker = memidx.AnchorChecker("p", lambda _p: PY_SRC.encode(), code_db=self.codedb,
                                       deadline=time.monotonic() + 0.2)
        spin = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 500000000) "
                "SELECT count(*) FROM c")
        real_db = checker._db

        def db():
            conn = real_db()
            real_exec = conn.execute

            class Wrapped:
                def execute(self_w, sql, *a, **k):
                    return real_exec(spin)
            return Wrapped()

        start = time.monotonic()
        with mock.patch.object(checker, "_db", db):
            verdict = checker.check(row)
        elapsed = time.monotonic() - start
        checker.close()
        print(f"[round3 spinning query, 0.2s deadline -> interrupted after {elapsed:.3f}s]")
        self.assertEqual(verdict, ("missing", "check budget exhausted"))
        self.assertLess(elapsed, 1.5)

    def test_an_exception_after_the_deadline_is_exhausted(self):
        row = {"ref": "src/foo.py#beta", "span": "symbol", "digest": "x" * 64}
        now = {"t": 0.0}
        checker = memidx.AnchorChecker("p", lambda _p: PY_SRC.encode(), deadline=50.0)

        def db():
            now["t"] = 100.0
            raise RuntimeError("boom")

        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(checker, "_db", db):
            self.assertEqual(checker.check(row), ("missing", "check budget exhausted"))
        now["t"] = 0.0

        def db_in_time():
            raise RuntimeError("boom")

        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(checker, "_db", db_in_time):
            with self.assertRaises(RuntimeError):
                checker.check(row)

    def test_a_sql_error_after_the_deadline_is_exhausted_not_snapshot_unavailable(self):
        row = {"ref": "src/foo.py#beta", "span": "symbol", "digest": "x" * 64}
        now = {"t": 0.0}
        checker = memidx.AnchorChecker("p", lambda _p: PY_SRC.encode(), deadline=50.0)

        class Conn:
            def execute(self_c, *a, **k):
                now["t"] = 49.0
                import sqlite3
                now["t"] = 100.0
                raise sqlite3.OperationalError("interrupted")

        # the clock is read before the verdict is chosen; the first checks run at t=0
        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(checker, "_db", lambda: Conn()):
            self.assertEqual(checker.check(row), ("missing", "check budget exhausted"))

    def test_standing_budget_miss_after_start_is_unchecked_never_missing_or_never(self):
        t1 = TestStandingAnchorSummary.standing_topic(None, refs=("src/foo.py", "src/unbound.py"))
        t2 = t1.replace("TOP-9900", "TOP-9901")
        self.add_topic("s1", t1)
        self.add_topic("s2", t2)
        self.code_file("src/foo.py", PY_SRC)
        self.code_file("src/unbound.py", PY_SRC)
        self.bind("src/foo.py", "TOP-9900")
        self.bind("src/foo.py", "TOP-9901")
        self.reindex()
        conn = memidx.open_db_noncreating(self.db, project="p")
        now = {"t": 0.0}
        real = memidx.sha256_hex
        hashed = []

        def crossing(b):
            out = real(b)
            if not hashed:
                now["t"] = 100.0       # the very first hash crosses the deadline
            hashed.append(1)
            return out

        try:
            with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                    mock.patch.object(memidx, "sha256_hex", crossing):
                summary = memidx.standing_anchor_summary(
                    conn, "p", ["TOP-9900", "TOP-9901"], [self.code])
        finally:
            conn.close()
        self.assertEqual(summary.get("unchecked"), 2, summary)
        self.assertEqual(summary["missing"], 0)
        self.assertEqual(summary["never"], 0)

    def test_for_path_unanchored_ref_after_exhaustion_is_not_never(self):
        row_fm = {"code_refs": ["src/foo.py"]}
        now = {"t": 100.0}
        checker = memidx.AnchorChecker("p", lambda _p: b"x", deadline=50.0)
        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]):
            st = memidx.anchor_statuses(row_fm, ["src/foo.py"], checker)
        self.assertEqual([(x["kind"], x["detail"]) for x in st], [("missing", "check budget exhausted")])

    def test_for_path_two_refs_second_hash_exhausts_gives_only_the_exhausted_header(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py")
        self.bind("src/foo.py#beta")
        self.reindex()
        self.code_reindex()
        self.code_file("src/foo.py", PY_SRC.replace("x = 2", "x = 3"))
        self.code_reindex()
        now = {"t": 0.0}
        real = memidx.sha256_hex
        calls = []

        def crossing(b):
            out = real(b)
            calls.append(1)
            if len(calls) == 2:       # the second ref's first hash
                now["t"] = 100.0
            return out

        with mock.patch.object(memidx.time, "monotonic", lambda: now["t"]), \
                mock.patch.object(memidx, "sha256_hex", crossing):
            out = self.for_path("src/foo.py")
        self.assertEqual(self.anchor_lines(out), ["Anchor missing: src/foo.py (check budget exhausted)."])
        self.assertNotIn("Anchor changed", out)

    def test_standing_without_a_worker_connection_counts_every_topic_unchecked(self):
        self._bound_standing()
        conn = memidx.open_db_noncreating(self.db, project="p")
        real = memidx.open_db_readonly

        def failing(path, *a, **k):
            # the worker reopens the path SQLite reports, which may be the
            # realpath spelling of the caller's (macOS /var -> /private/var)
            if os.path.realpath(path) == os.path.realpath(self.db):
                return None
            return real(path, *a, **k)

        try:
            with mock.patch.object(memidx, "open_db_readonly", failing):
                summary = memidx.standing_anchor_summary(
                    conn, "p", ["TOP-9900"], [self.code], code_db=self.codedb)
        finally:
            conn.close()
        self.assertEqual(summary.get("unchecked"), 1, summary)
        self.assertNotIn("skipped", summary)

    def test_a_file_over_8_mib_is_exhausted_without_a_read(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
        live = self.code_file("src/foo.py", PY_SRC)
        self.bind("src/foo.py")
        self.reindex()
        with open(live, "ab") as f:
            f.truncate(memidx.ANCHOR_MAX_FILE_BYTES + 1)
        reads = []
        real_read = Path.read_bytes

        def spy(path):
            reads.append(str(path))
            return real_read(path)

        with mock.patch.object(Path, "read_bytes", spy):
            out = self.for_path("src/foo.py")
        self.assertEqual(self.anchor_lines(out), ["Anchor missing: src/foo.py (check budget exhausted)."])
        self.assertNotIn(live, reads)
        self.assertIn("a ruling", out)

    def test_a_worker_finishing_in_time_changes_nothing(self):
        self._bound()
        (self.code / "src/foo.py").write_text(PY_SRC.replace("x = 2", "x = 3"))
        self.code_reindex()
        out = self.for_path("src/foo.py")
        self.assertEqual(self.anchor_lines(out), ["Anchor changed since capture (bind): src/foo.py#beta"])


class TestGateRound2Lookup(SessionCase):
    def test_valid_duplicate_wins_over_an_undecodable_one_in_either_order(self):
        for bad, good in (("a", "b"), ("b", "a")):
            with self.subTest(bad=bad):
                for f in (self.store / "topics").glob("*.md"):
                    f.unlink()
                bad_raw = plain_topic("TOP-9001", []).encode() + b"\xff\xfe tail\n"
                (self.store / "topics" / f"{bad}.md").write_bytes(bad_raw)
                (self.store / "topics" / f"{good}.md").write_bytes(
                    plain_topic("TOP-9001", ["src/other.py"]).encode())
                self.assertEqual(memidx._topic_file_for_id(self.store, "TOP-9001"),
                                 self.store / "topics" / f"{good}.md")
                self.code_file("src/foo.py", PY_SRC)
                rc, out, err = run_memidx(["bind", "TOP-9001", "src/foo.py", "--root", str(self.store),
                                           "--code-root", str(self.code)])
                self.assertEqual(rc, 0, err)
                self.assertEqual((self.store / "topics" / f"{bad}.md").read_bytes(), bad_raw)

    def test_only_an_undecodable_file_is_still_found_by_name(self):
        (self.store / "topics" / "a.md").write_bytes(plain_topic("TOP-9001", []).encode() + b"\xff")
        self.assertEqual(memidx._topic_file_for_id(self.store, "TOP-9001"), self.store / "topics" / "a.md")


class TestGateRound2DuplicateRowsAtRef(unittest.TestCase):
    def test_a_duplicate_pair_at_ref_reduced_to_one_row_errors_as_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = git_store(td)
            (root / "topics" / "foo.md").write_text(topic_text([good_row(), good_row()]))
            commit_all(root)
            (root / "topics" / "foo.md").write_text(topic_text([good_row()]))
            rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("row removed or changed after being recorded", out)
            self.assertIn("anchor ('src/foo.py', 'L1')", out)


class SymlinkedTmpMixin:
    """Every inherited test runs with the fixture under a symlinked temp dir."""

    SYMLINKED_TMP = True

    def test_fixture_really_is_behind_a_symlink(self):
        self.assertNotEqual(os.path.realpath(self.td), str(self.td))


class TestAnchorBackfillSymlinkedTmp(SymlinkedTmpMixin, TestAnchorBackfill):
    pass


class TestProposeBindingsSymlinkedTmp(SymlinkedTmpMixin, TestProposeBindings):
    pass


class TestForPathAnchorDeliverySymlinkedTmp(SymlinkedTmpMixin, TestForPathAnchorDelivery):
    pass


class TestGateRound1ByteWritersSymlinkedTmp(SymlinkedTmpMixin, TestGateRound1ByteWriters):
    pass


class TestGateRound2LookupSymlinkedTmp(SymlinkedTmpMixin, TestGateRound2Lookup):
    pass


class TestGateRound3WallClockSymlinkedTmp(SymlinkedTmpMixin, TestGateRound3WallClock):
    pass


class TestLedgerRowSpelling(SessionCase):
    """A tool-edit ledger row's `path` and its `root` may be spelled differently
    (tool spelling vs the configured realpath root); the row must survive."""

    def spellings(self):
        link, real = str(self.code), os.path.realpath(self.code)
        return (("path-link-root-real", link, real), ("path-real-root-link", real, link))

    def rows_for(self, path_root, rel="src/foo.py"):
        _label, path_spelling, root_spelling = path_root
        row = self.ledger_row(rel)
        row["path"] = path_spelling + "/" + rel
        row["root"] = root_spelling
        return row

    def test_backfill_keeps_rows_whose_path_and_root_are_spelled_differently(self):
        for spelling in self.spellings():
            with self.subTest(spelling[0]):
                for f in (self.store / "topics").glob("*.md"):
                    f.unlink()
                self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"]))
                self.code_file("src/foo.py", PY_SRC)
                self.session("s1", [self.rows_for(spelling)])
                rc, out, err = run_memidx(["anchor-backfill", "--root", str(self.store), "--project", "p",
                                           "--sessions-dir", str(self.sessions), "--dry-run"])
                self.assertEqual(rc, 0, err)
                self.assertIn("1 anchor(s) would be captured", out)

    def test_propose_bindings_keeps_rows_whose_path_and_root_are_spelled_differently(self):
        for spelling in self.spellings():
            with self.subTest(spelling[0]):
                self.add_topic("a", plain_topic("TOP-9001", ["src/other.py"], "Alpha"))
                self.code_file("src/foo.py", PY_SRC)
                self.reindex()
                row = self.rows_for(spelling)
                self.session("s1", [row], [{"file": row["path"], "id": "TOP-9001", "title": "Alpha",
                                            "hook": "pre-edit-chain"}])
                rc, out, err = self.propose()
                self.assertEqual(rc, 0, err)
                self.assertIn("TOP-9001  src/foo.py", out)

    def test_rel_is_root_relative_for_a_mixed_spelling_row(self):
        for spelling in self.spellings():
            rows = memidx.ledger_code_rows({"ledger": [self.rows_for(spelling)]})
            self.assertEqual([r["rel"] for r in rows], ["src/foo.py"], spelling[0])


class TestLedgerRowSpellingSymlinkedTmp(SymlinkedTmpMixin, TestLedgerRowSpelling):
    pass
