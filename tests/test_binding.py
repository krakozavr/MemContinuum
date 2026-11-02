"""0.5.0 PR 2 -- binding proposals and anchors: the `anchors:` schema and its
append-only freeze (memlint), the capture commands, and the delivery routes.
Every fixture builds its own throwaway store, code root and session files."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
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


def topic_text(rows=None, code_refs=("src/foo.py",), extra_links="", anchors_text=None):
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
                topic_text([good_row(), new_row], code_refs=("src/foo.py", "src/bar.py"))
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
        self.assertIn("code_refs:\n  - src/foo.py\n", after)

    def test_existing_identical_code_ref_is_left_alone(self):
        self.put_topic(topic_text(code_refs=("src/foo.py",)))
        rc, out, err = self.bind("src/foo.py")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.topic().count("- src/foo.py"), 1)

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

    def test_prefix_and_glob_are_not_anchorable_rc_2(self):
        self.put_topic(topic_text())
        before = self.topic()
        for ref in ("src/", "src/*.py"):
            with self.subTest(ref=ref):
                rc, out, err = self.bind(ref)
                self.assertEqual(rc, 2)
                self.assertIn("not anchorable", err)
        self.assertEqual(self.topic(), before)

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
    """A tmp store + indexed db, a code root and a sessions dir."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = Path(self._td.name)
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

    def test_topic_that_already_matches_the_path_is_not_proposed(self):
        self.add_topic("a", plain_topic("TOP-9001", ["src/foo.py"]))
        self.reindex()
        self.session("s1", [self.ledger_row("src/foo.py")], [self.fallback("src/foo.py", "TOP-9001")])
        rc, out, err = self.propose()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("TOP-9001", out)

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
