"""0.5.0 PR 2 -- binding proposals and anchors: the `anchors:` schema and its
append-only freeze (memlint), the capture commands, and the delivery routes.
Every fixture builds its own throwaway store, code root and session files."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
