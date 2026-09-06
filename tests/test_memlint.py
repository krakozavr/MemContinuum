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


if __name__ == "__main__":
    unittest.main()
