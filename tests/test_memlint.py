import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memlint  # noqa: E402

FIXTURES = TOOLS_DIR / "fixtures"


def lint_single_file(fixture_name: str):
    """Copy one violation fixture into an isolated temp root and lint it."""
    src = FIXTURES / "memlint_violations" / fixture_name
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "topics" / "reference").mkdir(parents=True, exist_ok=True)
        dst = root / "topics" / "reference" / fixture_name
        shutil.copy(src, dst)
        errors, warnings = memlint.lint_root(root)
        return errors, warnings


class TestMemlintViolations(unittest.TestCase):
    def test_owner_verbatim_without_source_is_error(self):
        errors, _warnings = lint_single_file("bad_owner_verbatim_no_source.md")
        self.assertTrue(errors, "expected an error for owner-verbatim ruling missing source")
        self.assertTrue(any("owner-verbatim" in e and "source" in e for e in errors), errors)

    def test_superseded_without_pointer_is_error(self):
        errors, _warnings = lint_single_file("bad_superseded_no_pointer.md")
        self.assertTrue(any("superseded" in e and "superseded_by" in e for e in errors), errors)

    def test_reverses_without_reason_is_error(self):
        errors, _warnings = lint_single_file("bad_reverses_no_reason.md")
        self.assertTrue(any("reverses" in e and "reason_for_change" in e for e in errors), errors)

    def test_current_mismatch_names_correct_value(self):
        errors, _warnings = lint_single_file("bad_current_mismatch.md")
        self.assertTrue(any("current" in e and "L2" in e for e in errors), errors)

    def test_unknown_enum_value_is_error(self):
        errors, _warnings = lint_single_file("bad_unknown_enum.md")
        self.assertTrue(any("maybe" in e for e in errors), errors)

    def test_processing_topic_without_code_refs_is_warning_not_error(self):
        errors, warnings = lint_single_file("warn_no_code_refs.md")
        self.assertEqual(errors, [], errors)
        self.assertTrue(any("code_refs" in w for w in warnings), warnings)


class TestMemlintClean(unittest.TestCase):
    def test_clean_fixtures_pass(self):
        root = FIXTURES / "memlint_clean"
        errors, _warnings = memlint.lint_root(root)
        self.assertEqual(errors, [], errors)

    def test_clean_fixtures_exit_zero_via_main(self):
        rc = memlint.main([str(FIXTURES / "memlint_clean")])
        self.assertEqual(rc, 0)

    def test_violation_exits_one_via_main(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "topics").mkdir()
            shutil.copy(
                FIXTURES / "memlint_violations" / "bad_superseded_no_pointer.md",
                root / "topics" / "bad.md",
            )
            rc = memlint.main([str(root)])
            self.assertEqual(rc, 1)


class TestMemlintRejectsUnknownFlags(unittest.TestCase):
    """H6: an unrecognised `--flag` used to fall through parse_argv's
    `elif root is None: root = a` arm and get linted as a PATH --
    `memlint.py --anything` walked a nonexistent directory named
    "--anything", found nothing to complain about, and printed
    "memlint: clean" at exit 0. An unknown flag must be refused, not
    silently treated as the ROOT positional."""

    def test_unknown_flag_is_rejected_not_treated_as_root(self):
        rc = memlint.main(["--anything"])
        self.assertNotEqual(rc, 0, "an unknown flag must not exit 0")

    def test_unknown_flag_message_names_it(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = memlint.main(["--anything"])
        self.assertNotEqual(rc, 0)
        self.assertIn("unknown argument: --anything", buf.getvalue())

    def test_known_root_with_code_root_still_works(self):
        # The fix must not regress the one real flag memlint.py has.
        rc = memlint.main([str(FIXTURES / "memlint_clean"), "--code-root", str(FIXTURES)])
        self.assertEqual(rc, 0)


class TestDuplicateIdErrors(unittest.TestCase):
    """A record id is a citation target; two records sharing one makes every
    chain/edge/citation lookup by that id silently ambiguous. Observed live
    2026-08-31: a store carried two TOP-0042s and two TOP-0101s, and an
    amendment landed on the wrong record's chain."""

    TOPIC = "---\ntype: topic\nid: {rid}\ntitle: {title}\nlinks: []\n---\n# {title}\n"

    def lint_tree(self, files):
        td = tempfile.mkdtemp()
        for rel, body in files.items():
            f = Path(td) / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8")
        try:
            return memlint.lint_root(Path(td))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    def test_two_records_sharing_an_id_is_an_error(self):
        errors, _ = self.lint_tree({
            "topics/a/one.md": self.TOPIC.format(rid="TOP-0042", title="One"),
            "topics/b/two.md": self.TOPIC.format(rid="TOP-0042", title="Two"),
        })
        dup = [e for e in errors if "duplicate id 'TOP-0042'" in e]
        self.assertEqual(len(dup), 1, errors)
        self.assertIn("one.md", dup[0])
        self.assertIn("two.md", dup[0])

    def test_unique_ids_are_clean(self):
        errors, _ = self.lint_tree({
            "topics/a/one.md": self.TOPIC.format(rid="TOP-0001", title="One"),
            "topics/b/two.md": self.TOPIC.format(rid="TOP-0002", title="Two"),
        })
        self.assertEqual([e for e in errors if "duplicate id" in e], [])

    def test_stem_collision_without_explicit_ids_warns_but_never_errors(self):
        """No id: falls back to the file stem, so `chain same-slug` is just as
        ambiguous -- but renaming a topic's area must not become an ERROR, so
        this is a warning steering toward explicit ids (round-3 reviewer
        finding 8)."""
        body = "---\ntype: topic\ntitle: T\nlinks: []\n---\n# T\n"
        errors, warnings = self.lint_tree({
            "topics/a/same-slug.md": body,
            "topics/b/same-slug.md": body,
        })
        self.assertEqual([e for e in errors if "duplicate id" in e], [])
        stem_warns = [w for w in warnings if "same-slug" in w and "ambiguous" in w]
        self.assertEqual(len(stem_warns), 1, warnings)

    def test_stem_collision_with_brace_in_stem_does_not_crash(self):
        """F4 regression: the stem-collision warning used to build the message
        as an f-string concatenated with a literal `.format(stem)` applied to
        the WHOLE result, so any `{...}` / bare `}` or `{` already inside the
        stem (or a listed path) was re-interpreted as a format placeholder and
        raised KeyError/ValueError, aborting the entire lint run. Two id-less
        structured records sharing a stem containing `{x}` must still just
        produce the warning."""
        body = "---\ntype: topic\ntitle: T\nlinks: []\n---\n# T\n"
        stem = "weird{x}stem"
        errors, warnings = self.lint_tree({
            f"topics/a/{stem}.md": body,
            f"topics/b/{stem}.md": body,
        })
        self.assertEqual([e for e in errors if "duplicate id" in e], [])
        stem_warns = [w for w in warnings if stem in w and "ambiguous" in w]
        self.assertEqual(len(stem_warns), 1, warnings)

    def test_concepts_and_topics_share_one_id_namespace(self):
        """`chain`/`for-path` resolve ids across record types; a concept and a
        topic sharing an id is exactly as ambiguous as two topics."""
        concept = "---\ntype: concept\nid: TOP-0042\ntitle: C\n---\n# C\nNOT about x.\n"
        errors, _ = self.lint_tree({
            "topics/a/one.md": self.TOPIC.format(rid="TOP-0042", title="One"),
            "concepts/c.md": concept,
        })
        self.assertEqual(len([e for e in errors if "duplicate id 'TOP-0042'" in e]), 1, errors)


class TestMemlintPythonSymbolVocabulary(unittest.TestCase):
    """Task 6: memlint's #symbol vocabulary check (lint_concept, via
    _symbol_declared) forwards the record's own ref_path to
    memidx.fragment_declared_in_text, so a Python implemented_by/tested_by
    path is checked against chunkers.python_ast.declared_symbols instead of
    always assuming Swift (the pre-Task-6 default, which would reject every
    real Python `def` -- the Swift lexer has no notion of it)."""

    CONCEPT = (
        "---\n"
        "type: concept\n"
        "id: {cid}\n"
        "title: Fixture -- Python #symbol vocabulary\n"
        "owner_boundary: fixture\n"
        "implemented_by:\n"
        "  - {ref}\n"
        "tested_by: []\n"
        "governed_by: []\n"
        "involved_in: []\n"
        "---\n\n"
        "Fixture. NOT this concept: nothing else.\n"
    )

    def _lint(self, ref: str, cid: str, py_source: str):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            (code_root / "thing.py").write_text(py_source)
            (td / "concept.md").write_text(self.CONCEPT.format(cid=cid, ref=ref))
            return memlint.lint_root(td, code_root=code_root)

    def test_python_symbol_declared_is_not_an_error(self):
        # If the call site ever regresses to not forwarding ref_path (i.e.
        # falls back to the default "x.swift"), this becomes an error --
        # the Swift lexer scan of `def declared_func(): pass` finds nothing.
        errors, _warnings = self._lint(
            "thing.py#declared_func",
            "CON-PYVOCAB-GOOD",
            "def declared_func():\n    pass\n",
        )
        self.assertFalse(any("CON-PYVOCAB-GOOD" in e for e in errors), errors)

    def test_python_symbol_not_declared_is_an_error(self):
        errors, _warnings = self._lint(
            "thing.py#missingFunc",
            "CON-PYVOCAB-BAD",
            "def declared_func():\n    pass\n",
        )
        self.assertTrue(
            any("CON-PYVOCAB-BAD" in e and "missingFunc" in e for e in errors), errors
        )

    def test_python_class_container_symbol_declared_is_not_an_error(self):
        errors, _warnings = self._lint(
            "thing.py#Widget",
            "CON-PYVOCAB-CLASS",
            "class Widget:\n    def __init__(self):\n        pass\n",
        )
        self.assertFalse(any("CON-PYVOCAB-CLASS" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
