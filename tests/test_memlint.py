import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import chunkers  # noqa: E402
import chunkers.treesitter  # noqa: E402
import memidx  # noqa: E402
import memlint  # noqa: E402

VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
_SKIP_NO_VENV = ("MEMCONTINUUM_PYTHON not set -- tree-sitter tests need the fixed venv "
                 "with the seven pins installed (Task 1's coordinator step)")

FIXTURES = TOOLS_DIR / "fixtures"


def concept_md(cid: str, ref: str, title: str = "Fixture") -> str:
    """Shared concept-record fixture text: one implemented_by ref, minimal
    frontmatter, a body carrying the required "not this concept" sentence.
    Used by every test class below that needs a concept record with a
    single implemented_by/tested_by-shaped path to lint."""
    return (
        "---\n"
        "type: concept\n"
        f"id: {cid}\n"
        f"title: {title}\n"
        "owner_boundary: fixture\n"
        "implemented_by:\n"
        f"  - {ref}\n"
        "tested_by: []\n"
        "governed_by: []\n"
        "involved_in: []\n"
        "---\n\n"
        "Fixture. NOT this concept: nothing else.\n"
    )


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

    def test_owner_verbatim_question_mark_is_error(self):
        # lint-question-mark-verbatim: an owner-verbatim ruling whose text
        # ends with "?" is a question, not a ruling -- schema-usage
        # laundering (a question dressed up as a citable owner ruling).
        errors, _warnings = lint_single_file("bad_owner_verbatim_question.md")
        self.assertTrue(
            any("owner-verbatim" in e and "question" in e for e in errors), errors
        )

    def test_owner_verbatim_question_mark_trims_quotes_and_whitespace_first(self):
        # The text ends "...instead?\" " (a trailing quote-then-space
        # artifact) in the raw YAML value -- the check must trim that
        # before deciding the text ends with "?", not require the
        # question mark to be the literal last character.
        errors, _warnings = lint_single_file("bad_owner_verbatim_question_trailing_quote.md")
        self.assertTrue(
            any("owner-verbatim" in e and "question" in e for e in errors), errors
        )

    def test_owner_ratified_question_mark_is_not_flagged(self):
        # The rule is owner-verbatim ONLY -- owner-ratified is the
        # orchestrator's own paraphrase of what the owner affirmed (SCHEMA
        # section 5), never a literal transcript of the owner's own words,
        # so a question mark in it is not the same laundering risk.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "topics").mkdir()
            (root / "topics" / "t.md").write_text(
                "---\ntype: topic\nid: TOP-9008\ntitle: Ratified question mark\n"
                "current: L1\nlinks:\n  - link: L1\n    date: 2026-08-29\n"
                "    status: active\n    kind: adopted\n    ruling:\n"
                "      text: \"is this the right call?\"\n"
                "      authority: owner-ratified\n"
                "      source: \"owner message 2026-08-29\"\n"
                "    recorded_by: agent\n    recorded_at: 2026-08-29\n---\n\nBody.\n"
            )
            errors, _warnings = memlint.lint_root(root)
        self.assertFalse(any("question" in e for e in errors), errors)

    def test_reversed_link_pointing_at_a_still_active_target_is_error(self):
        # lint-two-active-links replacement (owner ruling 2026-09-06 10:41,
        # TOP-0122 L5): a kind: reversed link must name a reverses target
        # whose status has moved off active/provisional.
        errors, _warnings = lint_single_file("bad_reversed_target_still_active.md")
        self.assertTrue(
            any("reverses" in e and "L1" in e and "still active" in e for e in errors),
            errors,
        )

    def test_reversed_link_pointing_at_a_still_provisional_target_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "topics").mkdir()
            (root / "topics" / "t.md").write_text(
                "---\ntype: topic\nid: TOP-9012\ntitle: Reverses a provisional link\n"
                "current: L2\nlinks:\n"
                "  - link: L2\n    date: 2026-08-30\n    status: active\n"
                "    kind: reversed\n    reverses: L1\n"
                "    reason_for_change: new-evidence\n"
                "    ruling: {text: \"changed my mind\", authority: agent-inference}\n"
                "    recorded_by: agent\n    recorded_at: 2026-08-30\n"
                "  - link: L1\n    date: 2026-08-01\n    status: provisional\n"
                "    kind: adopted\n"
                "    ruling: {text: \"a provisional first answer\", authority: agent-inference}\n"
                "    recorded_by: agent\n    recorded_at: 2026-08-29\n---\n\nBody.\n"
            )
            errors, _warnings = memlint.lint_root(root)
        self.assertTrue(
            any("reverses" in e and "L1" in e and "still provisional" in e for e in errors),
            errors,
        )

    def test_amended_link_leaves_predecessor_active_with_no_rule(self):
        # kind: amended is exactly the case where the predecessor stays
        # active on purpose (SCHEMA section 3) -- no rule fires.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "topics").mkdir()
            (root / "topics" / "t.md").write_text(
                "---\ntype: topic\nid: TOP-9013\ntitle: Amended, not reversed\n"
                "current: L2\nlinks:\n"
                "  - link: L2\n    date: 2026-08-30\n    status: active\n"
                "    kind: amended\n    reverses: L1\n"
                "    reason_for_change: new-evidence\n"
                "    ruling: {text: \"refines the earlier ruling\", authority: agent-inference}\n"
                "    recorded_by: agent\n    recorded_at: 2026-08-30\n"
                "  - link: L1\n    date: 2026-08-01\n    status: active\n"
                "    kind: adopted\n"
                "    ruling: {text: \"the original, still-active ruling\", authority: agent-inference}\n"
                "    recorded_by: agent\n    recorded_at: 2026-08-29\n---\n\nBody.\n"
            )
            errors, _warnings = memlint.lint_root(root)
        self.assertFalse(any("reverses" in e and "still" in e for e in errors), errors)


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

    def _lint(self, ref: str, cid: str, py_source: str):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            (code_root / "thing.py").write_text(py_source)
            (td / "concept.md").write_text(
                concept_md(cid, ref, title="Fixture -- Python #symbol vocabulary")
            )
            return memlint.lint_root(td, code_roots=[code_root])

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


class TestMemlintMultipleCodeRoots(unittest.TestCase):
    """Task 8: --code-root is repeatable and the code index is root-scoped,
    so a concept's implemented_by/tested_by path is checked against every
    configured root, not just one:

    - found under exactly one root -> fine, no matter which root.
    - found under NONE of the roots -> error naming every root tried.
    - found under MORE THAN ONE root -> ERROR (not a warning): one
      reference must name one file, so a path that resolves inside two
      code roots is an unresolved ambiguity about which file the concept
      actually claims, exactly like the existing duplicate-implemented_by-
      claim check treats two concepts claiming the same symbol.
    """

    def _two_roots(self, td: Path):
        root_a = td / "root_a"
        root_b = td / "root_b"
        root_a.mkdir()
        root_b.mkdir()
        return root_a, root_b

    def test_ref_found_under_second_root(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            root_a, root_b = self._two_roots(td)
            (root_b / "thing.py").write_text("def f():\n    pass\n")
            (td / "concept.md").write_text(concept_md("CON-MULTIROOT-OK", "thing.py#f"))
            errors, _warnings = memlint.lint_root(td, code_roots=[root_a, root_b])
            self.assertFalse(any("CON-MULTIROOT-OK" in e for e in errors), errors)

    def test_ref_under_two_roots_is_an_error(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            root_a, root_b = self._two_roots(td)
            (root_a / "thing.py").write_text("def f():\n    pass\n")
            (root_b / "thing.py").write_text("def f():\n    pass\n")
            (td / "concept.md").write_text(concept_md("CON-MULTIROOT-AMBIG", "thing.py#f"))
            errors, _warnings = memlint.lint_root(td, code_roots=[root_a, root_b])
            hit = [e for e in errors if "CON-MULTIROOT-AMBIG" in e]
            self.assertTrue(hit, errors)
            self.assertIn("exists under several code roots", hit[0])
            self.assertIn("one reference must name one file", hit[0])
            self.assertIn(str(root_a.resolve()), hit[0])
            self.assertIn(str(root_b.resolve()), hit[0])

    def test_ref_found_under_no_root_names_every_root_tried(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            root_a, root_b = self._two_roots(td)
            (td / "concept.md").write_text(concept_md("CON-MULTIROOT-MISSING", "thing.py"))
            errors, _warnings = memlint.lint_root(td, code_roots=[root_a, root_b])
            hit = [e for e in errors if "CON-MULTIROOT-MISSING" in e]
            self.assertTrue(hit, errors)
            self.assertIn(str(root_a.resolve()), hit[0])
            self.assertIn(str(root_b.resolve()), hit[0])


class TestMemlintCLIRepeatableCodeRoot(unittest.TestCase):
    def test_cli_accepts_repeated_code_root(self):
        root, code_roots, unknown = memlint.parse_argv(
            ["S", "--code-root", "A", "--code-root", "B"]
        )
        self.assertEqual(root, "S")
        self.assertEqual(code_roots, ["A", "B"])
        self.assertIsNone(unknown)

    def test_single_code_root_still_returns_a_one_item_list(self):
        root, code_roots, unknown = memlint.parse_argv(["S", "--code-root", "A"])
        self.assertEqual(root, "S")
        self.assertEqual(code_roots, ["A"])
        self.assertIsNone(unknown)

    def test_no_code_root_returns_empty_list(self):
        root, code_roots, unknown = memlint.parse_argv(["S"])
        self.assertEqual(root, "S")
        self.assertEqual(code_roots, [])
        self.assertIsNone(unknown)


class TestF3MemlintChecks(unittest.TestCase):
    def _lint(self, frontmatter_yaml, body="Body.\n"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "topics" / "reference"; root.mkdir(parents=True)
            (root / "t.md").write_text(f"---\n{frontmatter_yaml}\n---\n{body}")
            return memlint.lint_root(Path(td))

    def test_rationale_authority_unknown_is_error(self):
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n  - link: L1\n    status: active\n"
            "    ruling: {text: r, authority: owner-verbatim, source: s}\n"
            "    rationale: {text: w, authority: bogus}\n"
        )
        self.assertTrue(any("rationale.authority" in e for e in errors), errors)

    def test_alternatives_authority_unknown_is_error(self):
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n  - link: L1\n    status: active\n"
            "    ruling: {text: r, authority: owner-verbatim, source: s}\n"
            "    alternatives:\n      - {option: o, rejected_because: b, authority: bogus}\n"
        )
        self.assertTrue(any("alternatives" in e for e in errors), errors)

    def test_duplicate_link_id_within_topic_is_error(self):
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n"
            "  - link: L1\n    status: active\n    ruling: {text: a, authority: owner-verbatim, source: s}\n"
            "  - link: L1\n    status: active\n    ruling: {text: b, authority: owner-verbatim, source: s}\n"
        )
        self.assertTrue(any("used 2 times" in e for e in errors), errors)

    def test_dangling_reverses_is_error(self):
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n"
            "  - link: L1\n    status: active\n    reverses: L99\n    reason_for_change: new-evidence\n"
            "    ruling: {text: a, authority: owner-verbatim, source: s}\n"
        )
        self.assertTrue(any("does not match any link id" in e for e in errors), errors)

    def test_agent_inference_with_validated_evidence_is_clean(self):
        # Ruling 76 (overrides this task's original agent-inference
        # exclusion): agent-inference is HOLD-eligible exactly like
        # reviewer-finding/code-derived -- validated evidence makes the
        # link clean, not an automatic error.
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n  - link: L1\n    status: active\n"
            "    ruling: {text: r, authority: agent-inference, source: s}\n"
            "    evidence: [\"a real citation\"]\n"
            "    invariant: {kind: no-bypass, pattern: x}\n"
        )
        self.assertFalse(any("evidence" in e or "agent-inference" in e for e in errors), errors)

    def test_agent_inference_with_empty_evidence_is_error(self):
        # Same authority, no validated evidence -- gets the same ERROR
        # every other non-CONSTRAINT authority gets.
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n  - link: L1\n    status: active\n"
            "    ruling: {text: r, authority: agent-inference, source: s}\n"
            "    evidence: []\n"
            "    invariant: {kind: no-bypass, pattern: x}\n"
        )
        self.assertTrue(any("evidence" in e for e in errors), errors)

    def test_reviewer_finding_with_only_blank_evidence_is_error(self):
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n  - link: L1\n    status: active\n"
            "    ruling: {text: r, authority: reviewer-finding, source: s}\n"
            "    evidence: [\"\", \"   \"]\n"
            "    invariant: {kind: no-bypass, pattern: x}\n"
        )
        self.assertTrue(any("evidence" in e for e in errors), errors)

    def test_reviewer_finding_with_scalar_evidence_is_error(self):
        # Ruling 70: evidence must be a parsed LIST of non-blank strings. A
        # bare YAML scalar ("evidence: commit abc123" instead of a list) is
        # not that shape -- and must not be silently accepted by iterating
        # its characters as if it were a list of one-letter "citations".
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n  - link: L1\n    status: active\n"
            "    ruling: {text: r, authority: reviewer-finding, source: s}\n"
            "    evidence: \"commit abc123\"\n"
            "    invariant: {kind: no-bypass, pattern: x}\n"
        )
        self.assertTrue(any("evidence" in e for e in errors), errors)

    def test_reviewer_finding_with_real_evidence_is_clean(self):
        errors, _ = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\nlinks:\n  - link: L1\n    status: active\n"
            "    ruling: {text: r, authority: reviewer-finding, source: s}\n"
            "    evidence: [\"commit abc123 -- verified\"]\n"
            "    invariant: {kind: no-bypass, pattern: x}\n"
        )
        self.assertFalse(any("evidence" in e or "agent-inference" in e for e in errors), errors)


class TestF4EmptyCodeRefIsRejected(unittest.TestCase):
    def _lint(self, frontmatter_yaml, body="Body.\n"):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "topics" / "reference"; root.mkdir(parents=True)
            (root / "t.md").write_text(f"---\n{frontmatter_yaml}\n---\n{body}")
            return memlint.lint_root(Path(td))

    def test_empty_code_ref_entry_is_error(self):
        errors, _warnings = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\ncode_refs: [\"\"]\nlinks: []\n"
        )
        self.assertTrue(any("t.md" in e and "code_refs" in e for e in errors), errors)
        self.assertTrue(any("TOP-1" in e for e in errors), errors)

    def test_fragment_only_code_ref_entry_is_error(self):
        errors, _warnings = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\ncode_refs: [\"#Foo\"]\nlinks: []\n"
        )
        self.assertTrue(any("code_refs" in e and "#Foo" in e for e in errors), errors)
        self.assertTrue(any("TOP-1" in e for e in errors), errors)

    def test_real_code_ref_entry_stays_clean(self):
        errors, _warnings = self._lint(
            "type: topic\nid: TOP-1\ntitle: T\ncode_refs: [\"src/foo.py\"]\nlinks: []\n"
        )
        self.assertFalse(any("code_refs" in e for e in errors), errors)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestJavaScriptSymbolRouting(unittest.TestCase):
    def test_js_symbol_fragment_routes_through_the_registry(self):
        text = "function widget_loader() {\n  return 1;\n}\n"
        self.assertTrue(memidx.fragment_declared_in_text("widget_loader", text, rel_path="a.js"))
        self.assertFalse(memidx.fragment_declared_in_text("nonexistent", text, rel_path="a.js"))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTypeScriptSymbolRouting(unittest.TestCase):
    def test_ts_symbol_fragment_routes_through_the_registry(self):
        text = "function widget_loader(): number {\n  return 1;\n}\n"
        self.assertTrue(memidx.fragment_declared_in_text("widget_loader", text, rel_path="a.ts"))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTsxSymbolRouting(unittest.TestCase):
    def test_tsx_symbol_fragment_routes_through_the_registry(self):
        text = "function WidgetLoader(): JSX.Element {\n  return null;\n}\n"
        self.assertTrue(memidx.fragment_declared_in_text("WidgetLoader", text, rel_path="a.tsx"))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestJavaSymbolRouting(unittest.TestCase):
    def test_java_symbol_fragment_routes_through_the_registry(self):
        text = "public class W {\n  public int getValue() {\n    return 1;\n  }\n}\n"
        self.assertTrue(memidx.fragment_declared_in_text("W.getValue", text, rel_path="W.java"))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPhpSymbolRouting(unittest.TestCase):
    def test_php_symbol_fragment_routes_through_the_registry(self):
        text = "<?php\nfunction widget_loader() {\n  return 1;\n}\n"
        self.assertTrue(memidx.fragment_declared_in_text("widget_loader", text, rel_path="a.php"))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestRustSymbolRouting(unittest.TestCase):
    def test_rust_symbol_fragment_routes_through_the_registry(self):
        text = "fn widget_loader() -> i32 {\n    1\n}\n"
        self.assertTrue(memidx.fragment_declared_in_text("widget_loader", text, rel_path="a.rs"))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestLuaSymbolRouting(unittest.TestCase):
    def test_lua_symbol_fragment_routes_through_the_registry(self):
        text = "function obj:widget_loader(a)\n  return a\nend\n"
        self.assertTrue(memidx.fragment_declared_in_text("obj.widget_loader", text, rel_path="a.lua"))
        self.assertTrue(memidx.fragment_declared_in_text("widget_loader", text, rel_path="a.lua"))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestMissingGrammarWheelIsAWarningNotAnError(unittest.TestCase):
    """Whole-branch review, finding 1. A tree-sitter grammar wheel is an
    OPTIONAL dependency: an engine set up with `--python` at an interpreter
    that lacks one is a supported install, and every other surface fails
    open on it. So a `#symbol` fragment on a `.js` path must degrade to a
    WARNING naming the wheel when javascript's backend cannot run here --
    not an error that fails the lint on a record nothing is wrong with.
    The error stays reserved for a symbol the AVAILABLE backend proves
    absent.

    The wheel is made unavailable with the same `sys.modules` shim
    tests/test_chunkers.py's registry tests use, around a
    treesitter.reset_cache() on both sides -- a successful chunker
    instance is cached per (lang, chunker_version), so a shim with no cache
    reset would be a no-op against an already-built instance."""

    JS_SOURCE = "function widget_loader() {\n  return 1;\n}\n"

    @contextlib.contextmanager
    def _javascript_wheel_absent(self):
        chunkers.treesitter.reset_cache()
        try:
            with mock.patch.dict(sys.modules, {"tree_sitter_javascript": None}):
                yield
        finally:
            chunkers.treesitter.reset_cache()

    def _lint(self, ref: str, js_source: str):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            (code_root / "widget.js").write_text(js_source)
            (td / "concept.md").write_text(
                concept_md("CON-js-wheel", ref, title="Fixture -- missing grammar wheel")
            )
            return memlint.lint_root(td, code_roots=[code_root])

    def test_predicate_is_none_with_the_wheel_absent_and_names_it(self):
        with self._javascript_wheel_absent():
            verdict, reason, remedy = memidx.fragment_declaration_status(
                "widget_loader", self.JS_SOURCE, rel_path="widget.js"
            )
        self.assertIsNone(verdict)
        self.assertIn("tree_sitter_javascript", reason)
        # A missing wheel is the ONE case backend-preflight answers: it
        # reports the same absence for the whole machine.
        self.assertIn("backend-preflight", remedy)
        # The thin verdict-only wrapper forwards the same None, so `why`'s
        # disk-scan fallback still reads it as falsy.
        with self._javascript_wheel_absent():
            self.assertIsNone(
                memidx.fragment_declared_in_text(
                    "widget_loader", self.JS_SOURCE, rel_path="widget.js"
                )
            )

    def test_predicate_is_true_with_the_wheel_present(self):
        verdict, reason, remedy = memidx.fragment_declaration_status(
            "widget_loader", self.JS_SOURCE, rel_path="widget.js"
        )
        self.assertTrue(verdict)
        self.assertEqual((reason, remedy), ("", ""))

    def test_lint_warns_and_stays_clean_with_the_wheel_absent(self):
        with self._javascript_wheel_absent():
            errors, warnings = self._lint("widget.js#widget_loader", self.JS_SOURCE)
        self.assertEqual(
            [e for e in errors if "widget_loader" in e], [],
            f"a missing optional grammar wheel must not fail the lint: {errors}",
        )
        named = [w for w in warnings if "tree_sitter_javascript" in w]
        self.assertEqual(len(named), 1, warnings)
        self.assertIn("widget_loader", named[0])
        self.assertIn("run backend-preflight", named[0])

    def test_lint_still_errors_on_a_symbol_the_available_backend_proves_absent(self):
        errors, _warnings = self._lint("widget.js#no_such_symbol", self.JS_SOURCE)
        self.assertTrue(
            any("no_such_symbol" in e for e in errors),
            f"with the wheel present, an absent symbol is still a hard error: {errors}",
        )

    def test_a_file_over_the_byte_cap_warns_and_names_the_reason(self):
        """External gate finding 7. A file the backend could not chunk at
        all -- here, one over the per-file byte cap -- is UNCHECKABLE, not
        proof its symbols are absent. The symbol below really is declared in
        the file; before this the empty vocabulary made it a hard error.

        The cap is lowered rather than a megabyte of filler written, and the
        instance cache is reset on both sides because a chunker instance is
        cached per (lang, chunker_version) and the cap is part of that
        fingerprint."""
        chunkers.treesitter.reset_cache()
        try:
            with mock.patch.dict(os.environ, {"MEMCONTINUUM_MAX_PARSE_BYTES": "8"}):
                errors, warnings = self._lint("widget.js#widget_loader", self.JS_SOURCE)
        finally:
            chunkers.treesitter.reset_cache()
        self.assertEqual(
            [e for e in errors if "widget_loader" in e], [],
            f"a file that could not be chunked must not fail the lint: {errors}",
        )
        named = [w for w in warnings if "widget_loader" in w]
        self.assertEqual(len(named), 1, warnings)
        self.assertIn("uncheckable", named[0])
        self.assertIn("TreeSitterFileTooLarge", named[0])
        # The remedy names the CAP, not backend-preflight: the backend runs
        # here, and preflight would report this language ok.
        self.assertIn("MEMCONTINUUM_MAX_PARSE_BYTES", named[0])
        self.assertNotIn("backend-preflight", named[0])


class TestPythonSyntaxErrorIsAWarningNotAnError(unittest.TestCase):
    """Follow-up round, external-fix-report residual 6: chunkers.python_ast
    (the native backend, no grammar wheel involved at all) returned `[]`
    from `declared_symbols` for a file it could not even parse -- the same
    dishonesty class TestMissingGrammarWheelIsAWarningNotAnError's
    over-the-cap case already fixed for tree-sitter (external gate finding
    7), just never carried to the native backends. `[]` reads as "this file
    parsed and declares nothing", so a `.py#symbol` reference into a file
    with a genuine syntax error used to fail the lint on a record that may
    be perfectly correct -- the symbol below really is declared in the
    fixture text; a syntax error two lines later is what breaks the parse.

    No grammar-wheel shim needed here (python_ast has none to shim) --
    the syntax error itself is the failure this test drives."""

    PY_SOURCE = (
        "def widget_loader():\n"
        "    return 1\n"
        "\n"
        "def (:\n"   # syntax error: an unparseable def
    )

    def _lint(self, ref: str, py_source: str):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            (code_root / "widget.py").write_text(py_source)
            (td / "concept.md").write_text(
                concept_md("CON-py-syntax-error", ref, title="Fixture -- python syntax error")
            )
            return memlint.lint_root(td, code_roots=[code_root])

    def test_predicate_is_none_on_a_syntax_error_and_names_it(self):
        verdict, reason, remedy = memidx.fragment_declaration_status(
            "widget_loader", self.PY_SOURCE, rel_path="widget.py"
        )
        self.assertIsNone(verdict)
        self.assertIn("python", reason)
        self.assertIn("SyntaxError", reason)
        self.assertIn("syntax", remedy)
        self.assertNotIn("backend-preflight", remedy)
        # The thin verdict-only wrapper forwards the same None.
        self.assertIsNone(
            memidx.fragment_declared_in_text(
                "widget_loader", self.PY_SOURCE, rel_path="widget.py"
            )
        )

    def test_a_parseable_file_still_proves_a_symbol_present_or_absent(self):
        clean = "def widget_loader():\n    return 1\n"
        verdict, reason, remedy = memidx.fragment_declaration_status(
            "widget_loader", clean, rel_path="widget.py"
        )
        self.assertTrue(verdict)
        self.assertEqual((reason, remedy), ("", ""))
        verdict, _reason, _remedy = memidx.fragment_declaration_status(
            "no_such_symbol", clean, rel_path="widget.py"
        )
        self.assertFalse(verdict)

    def test_lint_warns_and_stays_clean_with_a_syntax_error(self):
        errors, warnings = self._lint("widget.py#widget_loader", self.PY_SOURCE)
        self.assertEqual(
            [e for e in errors if "widget_loader" in e], [],
            f"a file that failed to parse must not fail the lint: {errors}",
        )
        named = [w for w in warnings if "widget_loader" in w]
        self.assertEqual(len(named), 1, warnings)
        self.assertIn("uncheckable", named[0])
        self.assertIn("SyntaxError", named[0])
        # The remedy names the FILE's syntax, not backend-preflight: python
        # has no wheel to be missing and preflight reports it ok.
        self.assertIn("syntax", named[0])
        self.assertNotIn("backend-preflight", named[0])

    def test_lint_still_errors_on_a_symbol_a_parseable_file_proves_absent(self):
        clean = "def widget_loader():\n    return 1\n"
        errors, _warnings = self._lint("widget.py#no_such_symbol", clean)
        self.assertTrue(
            any("no_such_symbol" in e for e in errors),
            f"a parseable file still fails the lint on a genuinely absent symbol: {errors}",
        )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestMalformedRecordDiagnostics(unittest.TestCase):
    """audit MC-P1-03 / design R2 (TOP-0123 L2): every malformed-record
    diagnostic becomes an `ERROR: <path>: <field>: <message>` line, never
    a traceback; a note under the lenient fallback is a WARNING, not an
    error, and stays exit 0."""

    def test_links_flow_open_is_an_error_naming_links(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root / "topics" / "bad.md", "---\ntype: topic\nid: TOP-9201\ntitle: Bad\nlinks: [\n---\nBody.\n")
            errors, _warnings = memlint.lint_root(root)
            self.assertTrue(any("links" in e for e in errors), errors)
            rc = memlint.main([str(root)])
            self.assertEqual(rc, 1)

    def test_valid_yaml_wrong_shapes_are_errors_naming_the_field(self):
        cases = [
            ("links_not_a_list", "id: TOP-9202\ntype: topic\nlinks: some text\n", "links"),
            ("tags_not_a_list", "id: TOP-9203\ntype: topic\ntags: a-string\n", "tags"),
            ("code_refs_scalar", "id: TOP-9204\ntype: topic\ncode_refs: src/x.py\n", "code_refs"),
            (
                "ruling_plain_text",
                "id: TOP-9205\ntype: topic\nlinks:\n  - link: L1\n    status: active\n    ruling: plain text\n",
                "links[0].ruling",
            ),
        ]
        for name, fm_body, expected_field in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _write(root / "topics" / "bad.md", f"---\n{fm_body}title: Bad\n---\nBody.\n")
                    errors, _warnings = memlint.lint_root(root)
                    self.assertTrue(any(expected_field in e for e in errors), errors)
                    rc = memlint.main([str(root)])
                    self.assertEqual(rc, 1)

    def test_link_with_no_id_is_an_error_naming_links_link(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "topics" / "bad.md",
                "---\nid: TOP-9206\ntype: topic\ntitle: Bad\nlinks:\n"
                '  - status: active\n    ruling: {text: "r", authority: owner-verbatim, source: s}\n'
                "---\nBody.\n",
            )
            errors, _warnings = memlint.lint_root(root)
            self.assertTrue(any("links[0].link" in e for e in errors), errors)

    def test_duplicate_link_id_within_one_topic_is_an_error(self):
        """Codex 2 (BLOCKING): schema mode errors on two links sharing one
        id within one topic (memidx.validate_record_shape's own
        diagnostic, surfaced here as an ordinary ERROR: line, exactly like
        every other malformed-record diagnostic in this class)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "topics" / "bad.md",
                "---\nid: TOP-9207\ntype: topic\ntitle: Bad\nlinks:\n"
                '  - link: L2\n    status: active\n    ruling: {text: "a", authority: agent-inference}\n'
                '  - link: L2\n    status: active\n    ruling: {text: "b", authority: owner-verbatim, source: s}\n'
                "---\nBody.\n",
            )
            errors, _warnings = memlint.lint_root(root)
            self.assertTrue(any("duplicate link id 'L2'" in e for e in errors), errors)
            rc = memlint.main([str(root)])
            self.assertEqual(rc, 1)

    def test_unreadable_and_non_utf8_files_are_errors_naming_file_never_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root / "topics" / "bad.md"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_bytes(b"---\ntitle: Bad\n---\n\xff\xfe broken bytes\n")
            errors, _warnings = memlint.lint_root(root)
            self.assertTrue(any(": file:" in e for e in errors), errors)
            rc = memlint.main([str(root)])
            self.assertEqual(rc, 1)

    def test_memlint_never_tracebacks_on_any_malformed_case(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root / "topics" / "bad1.md", "---\ntype: topic\nid: TOP-9207\ntitle: Bad\nlinks: [\n---\nBody.\n")
            _write(root / "topics" / "bad2.md", "---\nid: TOP-9208\ntype: topic\ntags: a-string\ntitle: Bad\n---\nBody.\n")
            # Fix wave 1, G1 (Grok BLOCKING 1): a note whose own complex
            # field parses to the wrong shape used to crash `lint_file`'s
            # dispatch -- `links: see TOP-1` made `is_topic` true off the
            # unvalidated frontmatter, then `lint_topic` iterated the
            # string character by character (`bool("see TOP-1").get`);
            # `metadata: foo`/`metadata: [a, b]` crashed `infer_type`
            # inside `build_record` on the reindex side of the same
            # unvalidated shape. These three must warn, never traceback.
            _write(root / "notes" / "note-links.md", "---\ntitle: my note\nlinks: see TOP-1\n---\nBody.\n")
            _write(root / "notes" / "note-metadata-scalar.md", "---\ntitle: my note\nmetadata: foo\n---\nBody.\n")
            _write(root / "notes" / "note-metadata-list.md", "---\ntitle: my note\nmetadata: [a, b]\n---\nBody.\n")
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memlint.main([str(root)])
            self.assertEqual(rc, 1)
            self.assertNotIn("Traceback", buf_out.getvalue())
            self.assertNotIn("Traceback", buf_err.getvalue())
            self.assertIn("WARNING: ", buf_out.getvalue())

    def test_note_with_shape_diagnostic_is_a_warning_not_an_error(self):
        """Fix wave 1, G1 (Grok BLOCKING 1, MINOR 6-7; design R2 as
        amended, ruling 133): a note's own wrongly-shaped complex field is
        a WARNING naming the field as dropped/ignored, never an ERROR --
        and `lint_file` must dispatch on the VALIDATED frontmatter (the
        field already dropped), never the raw one."""
        cases = [
            ("links_scalar", "links: see TOP-1", "links", "not a list of mappings; ignored"),
            ("metadata_scalar", "metadata: foo", "metadata", "not a mapping; ignored"),
            ("metadata_list", "metadata: [a, b]", "metadata", "not a mapping; ignored"),
        ]
        for name, fm_line, field, message in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _write(root / "notes" / "note.md", f"---\ntitle: my note\n{fm_line}\n---\nBody.\n")
                    errors, warnings = memlint.lint_root(root)
                    self.assertEqual(errors, [], errors)
                    self.assertTrue(
                        any(f"{field}: {message}" in w for w in warnings), warnings
                    )
                    buf_out, buf_err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                        rc = memlint.main([str(root)])
                    self.assertEqual(rc, 0)
                    self.assertNotIn("Traceback", buf_out.getvalue())
                    self.assertNotIn("Traceback", buf_err.getvalue())

    def test_note_with_malformed_yaml_is_a_warning_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(
                root / "notes" / "note.md",
                "---\n"
                "title: 'Unterminated quote note\n"
                "name: my-note\n"
                "description: no quotes here at all\n"
                "metadata:\n"
                "  node_type: memory\n"
                "permalink: sandbox/notes/my-note\n"
                "---\n"
                "Body text of the note.\n",
            )
            errors, warnings = memlint.lint_root(root)
            self.assertEqual(errors, [], errors)
            self.assertTrue(any("malformed YAML" in w for w in warnings), warnings)
            rc = memlint.main([str(root)])
            self.assertEqual(rc, 0)

    def test_a1_regressions_are_errors_naming_the_field_never_a_clean_note(self):
        """Fix round 1, finding A1 (BLOCKING): a fully schema-conformant
        topic (id, type, block-style links) undone by only ONE unrelated
        parse defect must be an ERROR, never silently waved through as a
        clean note (`memlint: clean`)."""
        cases = {
            "unterminated": (
                "---\ntype: topic\nid: TOP-9304\ntitle: T\nlinks:\n"
                '  - link: L1\n    status: active\n    ruling: {authority: owner-verbatim, text: t, source: s}\n'
                "Body.\n"
            ),
            "yaml_error": (
                "---\ntype: topic\nid: TOP-9305\ntitle: a: b\nlinks:\n"
                '  - link: L1\n    status: active\n    ruling: {authority: owner-verbatim, text: t, source: s}\n'
                "---\nBody.\n"
            ),
            "parses_to_list": (
                "---\n- type: topic\n- id: TOP-9306\n- links:\n"
                "    - link: L1\n      status: active\n"
                '      ruling: {authority: owner-verbatim, text: t, source: s}\n'
                "---\nBody.\n"
            ),
            "links_only_no_id_type": (
                "---\ntitle: a: b\nlinks:\n"
                '  - link: TOP-0001\n    status: active\n    ruling: {authority: owner-verbatim, text: something, source: s}\n'
                "---\nBody.\n"
            ),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _write(root / "topics" / "bad.md", text)
                    errors, _warnings = memlint.lint_root(root)
                    self.assertTrue(errors, f"{name}: expected at least one ERROR, got none")
                    self.assertTrue(
                        any("frontmatter" in e or "links" in e for e in errors), errors
                    )
                    rc = memlint.main([str(root)])
                    self.assertEqual(rc, 1, f"{name}: memlint must exit 1, never wave this through clean")


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _git_store(td):
    """A throwaway git repo, isolated from this machine's real gitconfig
    (a bare -c user.name/user.email pair rather than requiring one to be
    globally configured -- same reasoning tests/test_repo_init.py's own
    git helpers use)."""
    root = Path(td)
    _git(["init", "-q", str(root)], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
    return root


def _commit_all(root, message):
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-q", "-m", message], cwd=root)


TOPIC_L1_L2 = (
    "---\n"
    "type: topic\n"
    "id: TOP-1\n"
    "title: Test topic\n"
    "area: memory\n"
    "current: L2\n"
    "tags: []\n"
    "links:\n"
    "  - link: L2\n"
    "    date: '2024-01-02'\n"
    "    status: active\n"
    "    kind: adopted\n"
    "    ruling:\n"
    "      text: \"second ruling\"\n"
    "      authority: agent-inference\n"
    "  - link: L1\n"
    "    date: '2024-01-01'\n"
    "    status: historical\n"
    "    kind: adopted\n"
    "    ruling:\n"
    "      text: \"first ruling\"\n"
    "      authority: agent-inference\n"
    "---\n\nBody.\n"
)


def _run_memlint(args):
    """memlint.main([...]) is a pure function of argv (no subprocess) --
    this just captures stdout so the caller can assert on the printed
    ERROR:/summary lines without a subprocess round trip."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memlint.main(args)
    return rc, buf.getvalue()


class TestMemlintAgainstRef(unittest.TestCase):
    """Task A2-1: `memlint --against-ref REF [--staged] ROOT` -- append-only
    history enforcement. Each test builds its own throwaway git store (never
    the fixtures/ or the engine's own store)."""

    def _store_with_base_text(self, td, text):
        root = _git_store(td)
        (root / "topics").mkdir()
        (root / "topics" / "foo.md").write_text(text)
        _commit_all(root, "base")
        return root

    def _base_store(self, td):
        return self._store_with_base_text(td, TOPIC_L1_L2)

    def test_a_prepend_new_link_is_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            text = TOPIC_L1_L2.replace(
                "current: L2\n",
                "current: L3\n",
            ).replace(
                "links:\n",
                "links:\n"
                "  - link: L3\n"
                "    date: '2024-01-03'\n"
                "    status: active\n"
                "    kind: adopted\n"
                "    ruling:\n"
                "      text: \"third ruling\"\n"
                "      authority: agent-inference\n",
            )
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_b_edit_existing_ruling_text_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            text = TOPIC_L1_L2.replace("first ruling", "EDITED first ruling")
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("foo.md", out)
            self.assertIn("L1", out)
            self.assertIn("changed after being recorded", out)

    def test_c_forward_status_change_with_superseded_by_is_clean(self):
        """Ruling 142 (TOP-0122 L3): status may move active/provisional ->
        superseded/historical/declined, once, and superseded_by may be
        ADDED in that same move -- this is the honest way to mark a link
        superseded, not an append-only violation."""
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            text = TOPIC_L1_L2.replace(
                "    status: active\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
                "    status: superseded\n    superseded_by: L1\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
            )
            self.assertNotEqual(text, TOPIC_L1_L2, "fixture edit must actually change the text")
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_c_terminal_status_reverting_to_active_is_error(self):
        """Ruling 142: never back to active/provisional."""
        with tempfile.TemporaryDirectory() as td:
            base = TOPIC_L1_L2.replace(
                "    status: active\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
                "    status: superseded\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
            )
            root = self._store_with_base_text(td, base)
            text = base.replace(
                "    status: superseded\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
                "    status: active\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
            )
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L2", out)
            self.assertIn("status", out)

    def test_c_between_terminal_statuses_is_error(self):
        """Ruling 142: never between the three terminal values. L1 is
        already `status: historical` in the fixture -- move it sideways
        to `declined`."""
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            text = TOPIC_L1_L2.replace(
                "    status: historical\n    kind: adopted\n    ruling:\n      text: \"first ruling\"",
                "    status: declined\n    kind: adopted\n    ruling:\n      text: \"first ruling\"",
            )
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L1", out)
            self.assertIn("status", out)

    def test_c_provisional_promoted_to_active_in_place_is_error(self):
        """Ruling 142 / docs/SCHEMA.md section 5: promotion is a NEW link
        (owner-ratified/owner-verbatim), never an edit of the provisional
        link's own status to active -- verified against section 5's own
        text (see the coordinator response for the exact quote)."""
        with tempfile.TemporaryDirectory() as td:
            base = TOPIC_L1_L2.replace(
                "    status: active\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
                "    status: provisional\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
            )
            root = self._store_with_base_text(td, base)
            text = base.replace(
                "    status: provisional\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
                "    status: active\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
            )
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L2", out)
            self.assertIn("status", out)

    def test_c_superseded_by_changed_after_being_set_is_error(self):
        """Ruling 142: superseded_by is immutable once set."""
        with tempfile.TemporaryDirectory() as td:
            base = TOPIC_L1_L2.replace(
                "    status: active\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
                "    status: superseded\n    superseded_by: L1\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
            )
            root = self._store_with_base_text(td, base)
            text = base.replace("superseded_by: L1", "superseded_by: L9")
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L2", out)
            self.assertIn("superseded_by", out)

    def test_c_promoted_by_added_the_schema_way_is_clean(self):
        """Ruling 143 (TOP-0122 L1, task A2-2): `promoted_by` is a third
        forward-once field -- SCHEMA sec5 step 3's literal procedure adds
        it to the OLD (agent-inference/provisional) link when a NEW
        owner-ratified link promotes it. Adding it alone (no other field
        on L1 touched) must be clean, same as a superseded_by add."""
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            text = TOPIC_L1_L2.replace(
                "    status: historical\n    kind: adopted\n    ruling:\n      text: \"first ruling\"",
                "    status: historical\n    promoted_by: L2\n    kind: adopted\n    ruling:\n      text: \"first ruling\"",
            )
            self.assertNotEqual(text, TOPIC_L1_L2, "fixture edit must actually change the text")
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_c_promoted_by_changed_after_being_set_is_error(self):
        """Ruling 143: promoted_by is immutable once set, exactly like
        superseded_by."""
        with tempfile.TemporaryDirectory() as td:
            base = TOPIC_L1_L2.replace(
                "    status: historical\n    kind: adopted\n    ruling:\n      text: \"first ruling\"",
                "    status: historical\n    promoted_by: L2\n    kind: adopted\n    ruling:\n      text: \"first ruling\"",
            )
            root = self._store_with_base_text(td, base)
            text = base.replace("promoted_by: L2", "promoted_by: L9")
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L1", out)
            self.assertIn("promoted_by", out)

    def test_c_status_change_plus_body_edit_is_error(self):
        """Ruling 142: a lifecycle move must be the ONLY change on a
        recorded link -- combined with a body edit, both the body field
        and the status field get their own error message."""
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            text = TOPIC_L1_L2.replace(
                "    status: active\n    kind: adopted\n    ruling:\n      text: \"second ruling\"",
                "    status: superseded\n    superseded_by: L1\n    kind: adopted\n    ruling:\n      text: \"EDITED second ruling\"",
            )
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L2", out)
            self.assertIn("ruling", out)
            self.assertIn("status", out)

    def test_d_delete_a_link_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            # Drop the whole L1 entry, keep L2 and the rest of the shape valid.
            lines = TOPIC_L1_L2.splitlines(keepends=True)
            start = next(i for i, l in enumerate(lines) if l.strip() == "- link: L1")
            end = next(
                i for i in range(start + 1, len(lines))
                if lines[i].strip() == "---"
            )
            text = "".join(lines[:start] + lines[end:])
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L1", out)
            self.assertIn("removed after being recorded", out)

    def test_e_delete_topic_file_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            (root / "topics" / "foo.md").unlink()
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("foo.md", out)
            self.assertIn("deleted or renamed", out)

    def test_f_rename_topic_file_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            (root / "topics" / "foo.md").rename(root / "topics" / "bar.md")
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("foo.md", out)
            self.assertIn("deleted or renamed", out)

    def test_g_free_fields_stay_clean(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            text = (
                TOPIC_L1_L2
                .replace("title: Test topic", "title: Renamed title")
                .replace("tags: []", "tags: [a, b]")
                .replace("area: memory\n", "area: memory\ncode_refs:\n  - src/x.py\n")
                .replace("Body.\n", "New body text entirely.\n")
            )
            (root / "topics" / "foo.md").write_text(text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_h_staged_judges_the_index_not_the_working_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            bad_text = TOPIC_L1_L2.replace("first ruling", "EDITED first ruling")
            (root / "topics" / "foo.md").write_text(bad_text)
            _git(["add", "-A"], cwd=root)
            # Working tree now reverts back to the ORIGINAL (unstaged) --
            # the index still carries the bad edit.
            (root / "topics" / "foo.md").write_text(TOPIC_L1_L2)

            rc_worktree, out_worktree = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc_worktree, 0, out_worktree)

            rc_staged, out_staged = _run_memlint(["--against-ref", "HEAD", "--staged", str(root)])
            self.assertEqual(rc_staged, 1, out_staged)
            self.assertIn("changed after being recorded", out_staged)

    def test_i_not_a_git_repo_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "topics").mkdir()
            (root / "topics" / "foo.md").write_text(TOPIC_L1_L2)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 2, out)

    def test_i_unknown_ref_exits_2(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            rc, out = _run_memlint(["--against-ref", "not-a-real-ref-xyz", str(root)])
            self.assertEqual(rc, 2, out)

    def test_i_against_ref_with_no_ref_exits_2(self):
        """A2-1 review finding L1: `--against-ref` at the end of argv (no
        REF token follows) used to leave `against_ref` None and fall
        through to the ORDINARY schema-lint mode instead of refusing --
        `memlint.py ROOT --against-ref` exited 0 printing `memlint: clean`,
        never mentioning the missing REF. Now exits 2 with a usage
        message, same as every other malformed invocation."""
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memlint.main([str(root), "--against-ref"])
            self.assertEqual(rc, 2, buf_out.getvalue() + buf_err.getvalue())
            self.assertIn("--against-ref", buf_err.getvalue())
            self.assertIn("usage:", buf_err.getvalue())
            self.assertNotIn("memlint: clean", buf_out.getvalue())

    def test_j_malformed_frontmatter_old_side_is_diagnostic_not_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = _git_store(td)
            (root / "topics").mkdir()
            # Canonical-shaped (has links:) but an unterminated flow
            # collection -- the audit's own classic malformed-YAML
            # reproducer.
            (root / "topics" / "bad.md").write_text(
                "---\ntype: topic\nid: TOP-2\nlinks: [\n---\nBody.\n"
            )
            _commit_all(root, "base (malformed)")
            (root / "topics" / "bad.md").write_text(
                "---\ntype: topic\nid: TOP-2\nlinks: [\ntitle: changed\n---\nBody.\n"
            )
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("bad.md", out)

    def test_j_malformed_frontmatter_new_side_is_diagnostic_not_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            (root / "topics" / "foo.md").write_text(
                "---\ntype: topic\nid: TOP-1\nlinks: [\n---\nBody.\n"
            )
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("foo.md", out)

    def test_k_dash_prefixed_ref_is_rejected_not_swallowed_as_ref(self):
        """Grok M8: `--against-ref --staged HEAD` used to hand git the
        literal ref '--staged' (a GitError), silently discarding the real
        --staged flag that followed. The dash-shaped token must never be
        consumed as REF."""
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memlint.main(["--against-ref", "--staged", "HEAD", str(root)])
            self.assertEqual(rc, 2, buf_out.getvalue() + buf_err.getvalue())
            self.assertIn("--against-ref", buf_err.getvalue())
            self.assertNotIn("memlint: clean", buf_out.getvalue())

    def test_k_code_root_rejected_under_against_ref(self):
        """NIT-3 (whole-branch-review): --code-root used to be silently
        ignored under --against-ref (accepted, ran only the append-only
        pass, rc=0) -- now rejected with a message naming the conflict."""
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)
            code_root = Path(td) / "code"; code_root.mkdir()
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memlint.main(
                    ["--against-ref", "HEAD", str(root), "--code-root", str(code_root)]
                )
            self.assertEqual(rc, 2, buf_out.getvalue() + buf_err.getvalue())
            self.assertIn("--code-root", buf_err.getvalue())
            self.assertIn("--against-ref", buf_err.getvalue())

    def test_l_duplicate_link_id_on_old_side_is_refused_naming_the_id(self):
        """Codex 2 (BLOCKING): a duplicate link id must refuse comparison
        (naming the id, rc 1) rather than silently comparing whichever
        occurrence a dict comprehension happens to keep."""
        dup_base = (
            "---\ntype: topic\nid: TOP-1\ntitle: T\nlinks:\n"
            '  - link: L2\n    date: "2024-01-01"\n    status: active\n'
            '    ruling: {text: "first", authority: agent-inference}\n'
            '  - link: L2\n    date: "2024-01-02"\n    status: active\n'
            '    ruling: {text: "second", authority: owner-verbatim, source: s}\n'
            "---\n\nBody.\n"
        )
        with tempfile.TemporaryDirectory() as td:
            root = self._store_with_base_text(td, dup_base)
            (root / "topics" / "foo.md").write_text(dup_base.replace("Body.", "Body edited."))
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("duplicate link id 'L2'", out)

    def test_l_duplicate_link_id_on_new_side_is_refused_naming_the_id(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._base_store(td)  # TOP_L1_L2, clean ids L1/L2
            dup_text = TOPIC_L1_L2.replace("link: L1", "link: L2", 1)
            (root / "topics" / "foo.md").write_text(dup_text)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("duplicate link id 'L2'", out)

    def test_m_investigation_record_repair_is_not_blocked_at_all(self):
        """Grok M2 / whole-branch-review MODERATE-1, the real repro: a
        partner store's own commit that fixed an unquoted colon in a
        `type: investigation` record's title (no links at all -- there is no
        recorded link history for append-only to protect). _is_topic_like
        now recognizes the recovered `type: investigation` and treats it
        as out of scope for this mechanism entirely -- not even a note,
        since there was never anything here to repair FROM this
        mechanism's point of view."""
        with tempfile.TemporaryDirectory() as td:
            root = _git_store(td)
            (root / "investigations").mkdir()
            (root / "investigations" / "gate.md").write_text(
                "---\ntitle: broken: colon\ntype: investigation\nid: INV-9300\n---\nBody.\n"
            )
            _commit_all(root, "base (malformed title, unquoted colon)")
            (root / "investigations" / "gate.md").write_text(
                "---\ntitle: 'fixed: colon'\ntype: investigation\nid: INV-9300\n---\nBody.\n"
            )
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)
            self.assertNotIn("ERROR:", out)

    def test_m_repair_of_a_broken_topic_that_now_parses_is_a_note_not_an_error(self):
        """The topic-kind analogue: a genuinely topic-shaped record (kind
        recovers as "topic" via the lenient fallback, same shape test_j
        already uses) that never parsed at REF IS topic-relevant, so the
        repair path actually runs -- a note, never an error, once the new
        blob parses cleanly."""
        with tempfile.TemporaryDirectory() as td:
            root = _git_store(td)
            (root / "topics").mkdir()
            (root / "topics" / "bad.md").write_text(
                "---\ntype: topic\nid: TOP-9301\nlinks: [\n---\nBody.\n"
            )
            _commit_all(root, "base (malformed)")
            (root / "topics" / "bad.md").write_text(
                "---\ntype: topic\nid: TOP-9301\ntitle: Fixed\nlinks: []\n---\nBody.\n"
            )
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)
            self.assertIn("NOTE:", out)
            self.assertIn("repaired", out)
            self.assertNotIn("ERROR:", out)

    def test_m_deleted_record_with_nothing_recoverable_is_reported_as_record_not_topic(self):
        """Grok N11: when NOTHING at all could be recovered from the old
        side (an unterminated frontmatter block -- frontmatter stays `{}`,
        unlike the lenient-fallback cases above, which always recover
        type/id scalars), the conservative default still treats it as
        protected (a genuinely corrupted topic must never go silently
        unprotected), but the message must not claim it was specifically a
        "topic" when the real kind could not be determined -- "record" is
        the honest generic label."""
        with tempfile.TemporaryDirectory() as td:
            root = _git_store(td)
            (root / "concepts").mkdir()
            (root / "concepts" / "c.md").write_text(
                "---\ntype: concept\nid: CON-9302\ntitle: Something\n"
                # No closing "---" at all -- parse_record_text's
                # unterminated-block branch, which never populates fm.
            )
            _commit_all(root, "base (malformed concept, no closing ---)")
            (root / "concepts" / "c.md").unlink()
            _commit_all(root, "delete it")
            rc, out = _run_memlint(["--against-ref", "HEAD~1", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("record file deleted", out)
            self.assertNotIn("topic file deleted", out)

    # -- Grok re-gate MAJOR 1: a shape error unrelated to links (or a
    # duplicate link id) still leaves `links` fully populated in the
    # recovered frontmatter -- the repair/skip path must not be taken
    # just because `old_result.valid` is False; it must be taken only
    # when NO links were actually recovered.

    def test_n_shape_error_recovers_links_and_still_freezes_them(self):
        """(a): REF has a shape error on an UNRELATED field (`tags:
        not-a-list`) plus a clean L1 link -- links WERE recovered, so this
        is not a free repair. Fixing `tags` AND rewriting L1's recorded
        ruling text in the same commit must still be refused."""
        with tempfile.TemporaryDirectory() as td:
            base = (
                "---\ntype: topic\nid: TOP-9401\ntitle: T\ntags: not-a-list\nlinks:\n"
                '  - link: L1\n    status: active\n'
                '    ruling: {text: "first", authority: agent-inference}\n'
                "---\n\nBody.\n"
            )
            root = self._store_with_base_text(td, base)
            fixed = base.replace("tags: not-a-list", "tags: []").replace(
                '{text: "first", authority: agent-inference}',
                '{text: "EDITED first", authority: agent-inference}',
            )
            (root / "topics" / "foo.md").write_text(fixed)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L1", out)
            self.assertIn("changed after being recorded", out)

    def test_n_shape_error_recovers_links_clean_field_fix_is_ok(self):
        """(b): same REF as (a), but the new blob fixes ONLY `tags` --
        L1's recorded body is untouched, so this must be clean."""
        with tempfile.TemporaryDirectory() as td:
            base = (
                "---\ntype: topic\nid: TOP-9401\ntitle: T\ntags: not-a-list\nlinks:\n"
                '  - link: L1\n    status: active\n'
                '    ruling: {text: "first", authority: agent-inference}\n'
                "---\n\nBody.\n"
            )
            root = self._store_with_base_text(td, base)
            fixed = base.replace("tags: not-a-list", "tags: []")
            (root / "topics" / "foo.md").write_text(fixed)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

    def test_n_duplicate_id_on_ref_recovers_first_occurrence_as_history(self):
        """(c): REF has two links both `link: L1` -- the FIRST occurrence
        in file order is the recorded history (the same first-wins
        reading memidx.validate_record_shape's own duplicate-count
        diagnostic is built from). Deduping to that first body is clean;
        landing on the second body is an append-only violation."""
        with tempfile.TemporaryDirectory() as td:
            dup_base = (
                "---\ntype: topic\nid: TOP-9402\ntitle: T\nlinks:\n"
                '  - link: L1\n    status: active\n'
                '    ruling: {text: "first", authority: agent-inference}\n'
                '  - link: L1\n    status: active\n'
                '    ruling: {text: "second", authority: owner-verbatim, source: s}\n'
                "---\n\nBody.\n"
            )
            root = self._store_with_base_text(td, dup_base)
            clean_first = (
                "---\ntype: topic\nid: TOP-9402\ntitle: T\nlinks:\n"
                '  - link: L1\n    status: active\n'
                '    ruling: {text: "first", authority: agent-inference}\n'
                "---\n\nBody.\n"
            )
            (root / "topics" / "foo.md").write_text(clean_first)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 0, out)

            clean_second = (
                "---\ntype: topic\nid: TOP-9402\ntitle: T\nlinks:\n"
                '  - link: L1\n    status: active\n'
                '    ruling: {text: "second", authority: owner-verbatim, source: s}\n'
                "---\n\nBody.\n"
            )
            (root / "topics" / "foo.md").write_text(clean_second)
            rc, out = _run_memlint(["--against-ref", "HEAD", str(root)])
            self.assertEqual(rc, 1, out)
            self.assertIn("L1", out)
            self.assertIn("changed after being recorded", out)


class TestQuestionMarkRuleScope(unittest.TestCase):
    """Ruling 149 / whole-branch-review MODERATE-2: the question-mark rule
    applies only to links whose status is active or provisional -- a
    superseded question is history, and append-only forbids rewriting it
    (so the rule could never be cleared by superseding); the trim set
    gained `)]}` so a trailing bracket never hides a real question."""

    def _topic(self, status: str, text: str) -> str:
        return (
            "---\ntype: topic\nid: TOP-9400\ntitle: T\nlinks:\n"
            f"  - link: L1\n    status: {status}\n    kind: adopted\n"
            f'    ruling: {{text: "{text}", authority: owner-verbatim, source: s}}\n'
            + ("    superseded_by: L2\n" if status == "superseded" else "")
            + ("  - link: L2\n    status: active\n    kind: adopted\n"
               '    ruling: {text: "a real ruling", authority: owner-verbatim, source: s}\n'
               if status == "superseded" else "")
            + "---\n\nBody.\n"
        )

    def _lint(self, text):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write(root / "topics" / "t.md", text)
            return memlint.lint_root(root)

    def test_active_question_mark_is_an_error(self):
        errors, _ = self._lint(self._topic("active", "should we do X?"))
        self.assertTrue(any("question, not a ruling" in e for e in errors), errors)

    def test_provisional_question_mark_is_an_error(self):
        errors, _ = self._lint(self._topic("provisional", "should we do X?"))
        self.assertTrue(any("question, not a ruling" in e for e in errors), errors)

    def test_superseded_question_mark_is_clean(self):
        """A superseded link is history -- flagging it can never be
        cleared (append-only forbids rewriting a superseded link's own
        body), so the rule must not fire on it at all."""
        errors, _ = self._lint(self._topic("superseded", "should we do X instead?"))
        self.assertFalse(any("question, not a ruling" in e for e in errors), errors)

    def test_trailing_bracket_after_question_mark_is_still_caught(self):
        errors, _ = self._lint(self._topic("active", "does this affect decisionmaking?)"))
        self.assertTrue(any("question, not a ruling" in e for e in errors), errors)


def _marker_topic(tid: str, code_refs: list, link_yaml: str, area: str = "memory") -> str:
    """A minimal topic record for marker-verification tests: one topic id,
    a code_refs list (any mix of forms), and caller-supplied link YAML
    (already indented as `links:` entries)."""
    refs_block = "".join(f"  - {r}\n" for r in code_refs)
    code_refs_yaml = f"code_refs:\n{refs_block}" if code_refs else ""
    return (
        "---\n"
        "type: topic\n"
        f"id: {tid}\n"
        f"title: Fixture -- {tid}\n"
        f"area: {area}\n"
        f"{code_refs_yaml}"
        "links:\n"
        f"{link_yaml}"
        "---\n\nBody.\n"
    )


_CONSTRAINT_LINK_L1 = (
    "  - link: L1\n"
    "    date: '2026-01-01'\n"
    "    status: active\n"
    "    kind: adopted\n"
    "    ruling:\n"
    '      text: "the constraint"\n'
    "      authority: owner-verbatim\n"
    '      source: "s"\n'
)

_CONTEXT_LINK_L1 = (
    "  - link: L1\n"
    "    date: '2026-01-01'\n"
    "    status: active\n"
    "    kind: adopted\n"
    "    ruling:\n"
    '      text: "an inference, no evidence"\n'
    "      authority: agent-inference\n"
)


class TestMemlintDecisionMarkers(unittest.TestCase):
    """Task A2-2 (TOP-0122 L1 rule 2b): `decision: TOP-xxxx Ln` comments
    verified both ways. Each test builds its own throwaway store + code
    tree (never fixtures/ or the engine's own store)."""

    def _lint(self, topic_files: dict, code_files: dict, code_roots=None):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            root = td / "store"
            (root / "topics").mkdir(parents=True)
            for name, text in topic_files.items():
                (root / "topics" / name).write_text(text)
            code_root = td / "code"
            code_root.mkdir()
            for rel, text in code_files.items():
                p = code_root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
            roots = code_roots if code_roots is not None else [code_root]
            return memlint.lint_root(root, code_roots=roots)

    # (a) constraint link, marker present and matching -> clean.
    def test_a_marker_matching_a_constraint_link_is_clean(self):
        topic = _marker_topic("TOP-0042", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        code = {"src/x.py": "# decision: TOP-0042 L1\ndef alpha():\n    return 1\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertEqual(warnings, [], warnings)

    # (b) same, marker absent -> warning, no error.
    def test_b_marker_absent_is_a_warning_not_an_error(self):
        topic = _marker_topic("TOP-0042", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        code = {"src/x.py": "def alpha():\n    return 1\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertTrue(
            any("no marker at src/x.py#alpha" in w for w in warnings), warnings
        )

    # (c) marker pointing at a non-existent link -> error.
    def test_c_marker_at_nonexistent_link_is_error(self):
        topic = _marker_topic("TOP-0042", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        code = {"src/x.py": "# decision: TOP-0042 L99\ndef alpha():\n    return 1\n"}
        errors, _warnings = self._lint({"t.md": topic}, code)
        self.assertTrue(
            any("TOP-0042" in e and "L99" in e and "no such link" in e for e in errors), errors
        )

    # (c) marker pointing at a CONTEXT-only link -> error.
    def test_c_marker_at_context_only_link_is_error(self):
        topic = _marker_topic("TOP-0042", ["src/x.py#alpha"], _CONTEXT_LINK_L1)
        code = {"src/x.py": "# decision: TOP-0042 L1\ndef alpha():\n    return 1\n"}
        errors, _warnings = self._lint({"t.md": topic}, code)
        self.assertTrue(
            any("TOP-0042" in e and "L1" in e and "CONTEXT" in e for e in errors), errors
        )

    # (c) marker pointing at a topic whose code_refs do not name the file -> error.
    def test_c_marker_at_topic_whose_code_refs_do_not_name_the_file_is_error(self):
        topic_a = _marker_topic("TOP-0042", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        topic_b = _marker_topic("TOP-0043", ["other/file.py"], _CONSTRAINT_LINK_L1)
        code = {"src/x.py": "# decision: TOP-0043 L1\ndef alpha():\n    return 1\n"}
        errors, _warnings = self._lint({"a.md": topic_a, "b.md": topic_b}, code)
        self.assertTrue(
            any(
                "TOP-0043" in e and "L1" in e and "code_refs do not name" in e
                for e in errors
            ),
            errors,
        )

    # (d) a marker at a symbol under a glob-only ref -> error naming globs.
    def test_d_marker_under_glob_only_ref_is_error_naming_globs(self):
        topic = _marker_topic("TOP-0044", ["src/*.py"], _CONSTRAINT_LINK_L1)
        code = {"src/x.py": "# decision: TOP-0044 L1\ndef alpha():\n    return 1\n"}
        errors, _warnings = self._lint({"t.md": topic}, code)
        hit = [e for e in errors if "TOP-0044" in e and "L1" in e]
        self.assertTrue(hit, errors)
        self.assertIn("never marker-verified", hit[0])

    # (e) a dangling path#symbol -> error.
    def test_e_dangling_path_symbol_is_error(self):
        topic = _marker_topic("TOP-0045", ["src/x.py#missing_symbol"], _CONSTRAINT_LINK_L1)
        code = {"src/x.py": "def alpha():\n    return 1\n"}
        errors, _warnings = self._lint({"t.md": topic}, code)
        self.assertTrue(
            any(
                "TOP-0045" in e and "L1" in e and "dangling" in e and "missing_symbol" in e
                for e in errors
            ),
            errors,
        )

    # (f) a language without a chunker -> warning, no traceback.
    def test_f_language_without_a_chunker_is_warning_not_traceback(self):
        """Round 2b (Grok re-gate NIT 4): this file both CARRIES a marker
        (direction 1) and is named by a path#symbol ref on an active
        CONSTRAINT link (direction 2) -- both used to independently warn
        about the same underlying fact (no chunker for .rb), printing it
        twice. Exactly one "no chunker" line for this file now: direction
        1's attribution warning suppresses direction 2's generic per-file
        line in favor of naming this specific ref's own inability to
        locate its symbol."""
        topic = _marker_topic("TOP-0046", ["src/x.rb#thing"], _CONSTRAINT_LINK_L1)
        code = {"src/x.rb": "# decision: TOP-0046 L1\ndef thing\nend\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        no_chunker_warnings = [w for w in warnings if "no chunker for this file's language" in w and "x.rb" in w]
        self.assertEqual(len(no_chunker_warnings), 1, warnings)

    def test_g_no_chunker_marker_failing_store_checks_plus_path_symbol_ref_still_falls_back_to_generic(self):
        """Round 2b (NIT 4) coverage gap: the marker in this no-chunker
        file names a MISSING link, so direction 1 produces an ERROR, not
        the attribution warning -- this file never enters
        `chunkerless_pending`. Direction 2's own path#symbol ref into the
        same file must still fall through to the ordinary generic
        uncheckable-file warning; it has nothing more specific to defer
        to."""
        topic = _marker_topic("TOP-0049", ["src/x.rb#thing"], _CONSTRAINT_LINK_L1)
        code = {"src/x.rb": "# decision: TOP-0049 L99\ndef thing\nend\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(len(errors), 1, errors)
        self.assertTrue(
            "TOP-0049" in errors[0] and "L99" in errors[0] and "no such link" in errors[0], errors
        )
        no_chunker_warnings = [w for w in warnings if "no chunker for this file's language" in w and "x.rb" in w]
        self.assertEqual(len(no_chunker_warnings), 1, warnings)
        self.assertIn("markers not checked", no_chunker_warnings[0])

    # Fix round 2 R6: a no-chunker file with plain-path/glob-only code_refs
    # and no marker at all must never warn -- the old blanket "markers not
    # checked (no chunker for this file's language)" fired once per such
    # file in scope regardless of whether it held a marker, which meant a
    # real store with plain-path-only code_refs into bash/markdown/toml/
    # json files (no markers used anywhere yet) carried a warning for every
    # single one of them.

    def test_g_no_chunker_file_named_by_a_plain_path_with_no_marker_is_clean(self):
        topic = _marker_topic("TOP-0047", ["src/deploy.sh"], _CONSTRAINT_LINK_L1)
        code = {"src/deploy.sh": "#!/usr/bin/env bash\necho hello\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertEqual(warnings, [], warnings)

    def test_g_no_chunker_file_with_a_valid_marker_gets_one_attribution_warning(self):
        topic = _marker_topic("TOP-0047", ["src/deploy.sh"], _CONSTRAINT_LINK_L1)
        code = {"src/deploy.sh": "#!/usr/bin/env bash\n# decision: TOP-0047 L1\necho hello\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("cannot be attributed to a symbol", warnings[0])
        self.assertIn("no chunker for this file's language", warnings[0])
        self.assertIn("deploy.sh", warnings[0])

    def test_g_no_chunker_file_with_a_marker_naming_a_missing_link_is_error(self):
        topic = _marker_topic("TOP-0047", ["src/deploy.sh"], _CONSTRAINT_LINK_L1)
        code = {"src/deploy.sh": "#!/usr/bin/env bash\n# decision: TOP-0047 L99\necho hello\n"}
        errors, _warnings = self._lint({"t.md": topic}, code)
        self.assertTrue(
            any("TOP-0047" in e and "L99" in e and "no such link" in e for e in errors), errors
        )

    def test_g_path_symbol_ref_into_a_no_chunker_file_keeps_its_own_warning(self):
        """direction 2 (store -> code) is unaffected by this fix: a
        CONSTRAINT/HOLD link's own path#symbol ref still cannot locate its
        symbol without a chunker, and still warns -- even with no marker
        anywhere in the file (direction 1 stays silent here, per the test
        above)."""
        topic = _marker_topic("TOP-0048", ["src/deploy.sh#main"], _CONSTRAINT_LINK_L1)
        code = {"src/deploy.sh": "#!/usr/bin/env bash\necho hello\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertTrue(
            any(
                "markers not checked" in w
                and "no chunker for this file's language" in w
                and "deploy.sh" in w
                for w in warnings
            ),
            warnings,
        )

    def test_codex13_scan_never_opens_an_unreferenced_file(self):
        """Codex 13: the marker scan must open only files at least one
        topic's code_refs names -- a probe observed an unrelated file
        being opened (for its binary-file check) purely because it sat
        in the code root, never because anything referenced it."""
        topic = _marker_topic("TOP-0066", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": "# decision: TOP-0066 L1\ndef alpha():\n    return 1\n",
            "src/not-referenced.txt": "nothing to see here\n",
        }
        checked = []
        original = memlint.is_binary_file

        def spy(path):
            checked.append(str(path))
            return original(path)

        with mock.patch.object(memlint, "is_binary_file", side_effect=spy):
            errors, _warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertTrue(any(p.endswith("x.py") for p in checked), checked)
        self.assertFalse(any("not-referenced.txt" in p for p in checked), checked)

    def test_macos_duplicate_warning_is_keyed_by_physical_path(self):
        """The macOS duplicate warning (whole-branch-review, reproduced on
        CI, test_mem3...): a code root reached through a symlink (macOS's
        own /var -> /private/var, reproduced here with an explicit
        symlink) must never turn ONE physical file's own no-chunker
        warning into two -- direction 1's held-back attribution warning
        and direction 2's own dedup bookkeeping (round 2b, NIT 4:
        `chunkerless_pending`/`chunkerless_covered`, same shared-
        `warned_uncheckable`-style keying) must key on the SAME (physical,
        .resolve()'d) path, whichever spelling of the root each happened
        to walk through."""
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            store = td / "store"
            (store / "topics").mkdir(parents=True)
            topic = _marker_topic("TOP-0065", ["src/x.rb#thing"], _CONSTRAINT_LINK_L1)
            (store / "topics" / "t.md").write_text(topic)
            real_code = td / "real_code"
            (real_code / "src").mkdir(parents=True)
            (real_code / "src" / "x.rb").write_text("# decision: TOP-0065 L1\ndef thing\nend\n")
            link_code = td / "link_code"
            link_code.symlink_to(real_code, target_is_directory=True)
            errors, warnings = memlint.lint_root(store, code_roots=[link_code])
            self.assertEqual(errors, [], errors)
            hits = [w for w in warnings if "no chunker for this file's language" in w and "x.rb" in w]
            self.assertEqual(len(hits), 1, warnings)

    # Mem-3 (task-a2-2-review.md): the marker-scan "markers not checked"
    # warning must carry the SAME remedy lint_concept's identical wheel-
    # absent failure already gives (backend-preflight) -- the marker path
    # used to compute and then discard it.
    def test_mem3_marker_uncheckable_warning_carries_remedy(self):
        topic = _marker_topic("TOP-0054", ["widget.js#widget_loader"], _CONSTRAINT_LINK_L1)
        code = {"widget.js": "function widget_loader() {\n  return 1;\n}\n"}
        chunkers.treesitter.reset_cache()
        try:
            with mock.patch.dict(sys.modules, {"tree_sitter_javascript": None}):
                errors, warnings = self._lint({"t.md": topic}, code)
        finally:
            chunkers.treesitter.reset_cache()
        self.assertEqual(errors, [], errors)
        named = [w for w in warnings if "widget.js" in w and "markers not checked" in w]
        self.assertEqual(len(named), 1, warnings)
        self.assertIn("tree_sitter_javascript", named[0])
        self.assertIn("run backend-preflight", named[0])

    # (g) two roots -- the ref resolved against the right one.
    def test_g_two_roots_ref_resolved_against_the_right_one(self):
        topic = _marker_topic("TOP-0047", ["thing.py#f"], _CONSTRAINT_LINK_L1)
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            root = td / "store"
            (root / "topics").mkdir(parents=True)
            (root / "topics" / "t.md").write_text(topic)
            root_a = td / "root_a"
            root_b = td / "root_b"
            root_a.mkdir()
            root_b.mkdir()
            (root_b / "thing.py").write_text(
                "# decision: TOP-0047 L1\ndef f():\n    return 1\n"
            )
            errors, warnings = memlint.lint_root(root, code_roots=[root_a, root_b])
            self.assertEqual(errors, [], errors)
            self.assertFalse(
                any("TOP-0047" in w for w in warnings), warnings
            )

    # (h) three lines above the definition counts; four does not.
    def test_h_marker_three_lines_above_counts(self):
        topic = _marker_topic("TOP-0048", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": (
                "# decision: TOP-0048 L1\n"
                "# filler 1\n"
                "# filler 2\n"
                "def alpha():\n"
                "    return 1\n"
            )
        }
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertEqual(warnings, [], warnings)

    def test_h_marker_four_lines_above_does_not_count(self):
        topic = _marker_topic("TOP-0048", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": (
                "# decision: TOP-0048 L1\n"
                "# filler 1\n"
                "# filler 2\n"
                "# filler 3\n"
                "def alpha():\n"
                "    return 1\n"
            )
        }
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertTrue(
            any("no marker at src/x.py#alpha" in w for w in warnings), warnings
        )

    # Mem-5 (task-a2-2-review.md): a marker naming a TOPIC id that does not
    # exist anywhere in the store at all (distinct from test_c's "topic
    # exists, link does not").
    def test_mem5_marker_at_nonexistent_topic_is_error(self):
        topic = _marker_topic("TOP-0049", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        code = {"src/x.py": "# decision: TOP-9999 L1\ndef alpha():\n    return 1\n"}
        errors, _warnings = self._lint({"t.md": topic}, code)
        self.assertTrue(
            any("TOP-9999" in e and "no such topic" in e for e in errors), errors
        )

    # Mem-1 (task-a2-2-review.md): a topic's code_refs DOES carry a
    # path#symbol ref for this file -- it just names a DIFFERENT symbol
    # than the one the marker actually sits on. The old message denied any
    # path#symbol ref existed at all (the glob/bare-path wording); the
    # fixed one names the mismatch truthfully.
    def test_mem1_marker_names_a_path_symbol_ref_for_the_wrong_symbol(self):
        topic = _marker_topic("TOP-0050", ["src/x.py#beta"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": (
                "def beta():\n"
                "    return 2\n"
                "# decision: TOP-0050 L1\n"
                "def alpha():\n"
                "    return 1\n"
            )
        }
        errors, _warnings = self._lint({"t.md": topic}, code)
        hit = [e for e in errors if "TOP-0050" in e and "L1" in e]
        self.assertTrue(hit, errors)
        self.assertIn("src/x.py#beta", hit[0])
        self.assertIn("not src/x.py#alpha", hit[0])
        self.assertNotIn("glob", hit[0])
        self.assertNotIn("never marker-verified", hit[0])

    # Mem-1, the container variant, corrected (Codex 8, fix wave 1 G2): a
    # marker meant for a container (`class Foo:`) used to be attributed to
    # a MEMBER starting within 3 lines below it (chunk_file never gives a
    # container its own chunk), reported as a "wrong symbol" ERROR -- which
    # conflicted with ruling 144's own container carve-out (direction 2,
    # below, already treats this container ref as unverifiable-by-marker,
    # a WARNING, never an error). Direction 1 must agree: `Foo` names no
    # chunk anywhere in the file (only `method` does), and
    # fragment_declaration_status confirms `Foo` really is declared (a
    # container) -- so this ref is dropped from consideration entirely,
    # never misattributed to `method`, and never invents the wrong fix
    # (there is no member to point the code_ref at).
    def test_mem1_marker_above_container_is_not_misattributed_to_nearby_member(self):
        topic = _marker_topic("TOP-0051", ["src/x.py#Foo"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": (
                "# decision: TOP-0051 L1\n"
                "class Foo:\n"
                "    def method(self):\n"
                "        return 1\n"
            )
        }
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual([e for e in errors if "TOP-0051" in e], [], errors)
        self.assertTrue(
            any(
                "'Foo' is a container type" in w and "src/x.py" in w
                for w in warnings
            ),
            warnings,
        )

    # Codex 8: two adjacent short declarations, each with its own marker
    # (or none) -- a marker window must never cross into the PREVIOUS
    # declaration's own line, even when the flat 3-lines-above count would
    # otherwise reach it.
    def test_codex8_marker_window_never_crosses_into_the_previous_declaration(self):
        topic_alpha = _marker_topic("TOP-0060", ["src/x.py#alpha"], _CONSTRAINT_LINK_L1)
        topic_beta = _marker_topic("TOP-0061", ["src/x.py#beta"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": (
                "# decision: TOP-0060 L1\n"
                "def alpha():\n"
                "    pass\n"
                "def beta():\n"
                "    pass\n"
            )
        }
        errors, warnings = self._lint({"a.md": topic_alpha, "b.md": topic_beta}, code)
        # alpha's own marker (one line above it) matches cleanly.
        self.assertEqual([e for e in errors if "TOP-0060" in e], [], errors)
        # beta's window must never reach alpha's marker three lines up --
        # beta gets the plain "no marker yet" warning, never a false
        # match on alpha's TOP-0060.
        self.assertEqual([e for e in errors if "TOP-0061" in e], [], errors)
        self.assertTrue(
            any("TOP-0061" in w and "no marker at src/x.py#beta" in w for w in warnings),
            warnings,
        )

    # Codex 7: every marker in the window is examined, not just the first
    # (nearest) one found -- a valid marker (on the definition line) must
    # not shadow a bogus one sitting farther up, or vice versa.
    def test_codex7_valid_marker_followed_by_a_bogus_one_errors_on_the_bogus_one(self):
        # Valid marker FARTHEST (topmost), bogus one NEAREST gamma's own
        # definition line: the old single-match scan (forward, farthest
        # match wins) found only the valid one and stopped, so the bogus
        # marker's error went entirely unreported.
        topic = _marker_topic("TOP-0062", ["src/x.py#gamma"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": (
                "# decision: TOP-0062 L1\n"  # valid: matches gamma below, farthest
                "# decision: TOP-9997 L1\n"  # bogus: no such topic, nearest
                "def gamma():\n"
                "    return 3\n"
            )
        }
        errors, _warnings = self._lint({"t.md": topic}, code)
        self.assertEqual([e for e in errors if "TOP-0062" in e], [], errors)
        self.assertTrue(
            any("TOP-9997" in e and "no such topic" in e for e in errors), errors
        )

    # Codex 7: two valid markers in one window each satisfy their own
    # topic -- a member constrained by two independent rules at once.
    def test_codex7_two_valid_markers_each_satisfy_their_own_topic(self):
        topic_a = _marker_topic("TOP-0063", ["src/x.py#delta"], _CONSTRAINT_LINK_L1)
        topic_b = _marker_topic("TOP-0064", ["src/x.py#delta"], _CONSTRAINT_LINK_L1)
        code = {
            "src/x.py": (
                "# decision: TOP-0063 L1\n"
                "# decision: TOP-0064 L1\n"
                "def delta():\n"
                "    return 4\n"
            )
        }
        errors, warnings = self._lint({"a.md": topic_a, "b.md": topic_b}, code)
        self.assertEqual(errors, [], errors)
        self.assertEqual(warnings, [], warnings)

    # Ruling 144 (TOP-0122 L4): a path#symbol ref whose symbol the chunker
    # never reports as its own chunk (a Swift protocol requirement --
    # signature only, no body) is a WARNING, not an error, when the name is
    # genuinely present in the file text -- the declaration cannot be
    # VERIFIED by this engine's parser layer, which is not the same claim
    # as DISPROVEN.
    def test_ruling144_swift_protocol_requirement_name_present_is_warning(self):
        topic = _marker_topic("TOP-0052", ["proto.swift#cleanup"], _CONSTRAINT_LINK_L1)
        code = {
            "proto.swift": (
                "protocol Cleanup {\n"
                "    func cleanup()\n"
                "}\n"
            )
        }
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertEqual(errors, [], errors)
        self.assertTrue(
            any(
                "TOP-0052:L1: proto.swift#cleanup cannot be verified by the chunker"
                in w and "name present, no declaration reported" in w
                for w in warnings
            ),
            warnings,
        )
        self.assertFalse(any("dangling" in w for w in warnings), warnings)

    # Ruling 144's other half: the symbol's NAME is genuinely absent from
    # the file text (not merely unreported as a declaration) -- this stays
    # the ERROR ruling 144 keeps (ordinary dangling-ref behavior,
    # unchanged; ties this class's coverage explicitly to the ruling, not
    # just to the pre-existing test (e) above).
    def test_ruling144_name_genuinely_absent_stays_error(self):
        topic = _marker_topic("TOP-0053", ["src/y.py#totally_absent_name"], _CONSTRAINT_LINK_L1)
        code = {"src/y.py": "def alpha():\n    return 1\n"}
        errors, warnings = self._lint({"t.md": topic}, code)
        self.assertTrue(
            any(
                "TOP-0053" in e and "dangling" in e and "totally_absent_name" in e
                for e in errors
            ),
            errors,
        )
        self.assertFalse(
            any("cannot be verified by the chunker" in w for w in warnings), warnings
        )


if __name__ == "__main__":
    unittest.main()
