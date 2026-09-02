import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import chunkers
import chunkers.python_ast as python_ast
import memidx

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_code_index import FIXTURES, code_reindex  # noqa: E402 -- reuse the Swift fixture
# corpus and code-reindex helper test_code_index.py already builds, rather than duplicating
# the corpus text here.

GOLDEN_PATH = TESTS_DIR / "goldens" / "swift_chunks_kind_v2.json"
PY_FIXTURES = TESTS_DIR / "fixtures" / "python_corpus"


class TestRegistry(unittest.TestCase):
    def test_kinds_frozen(self):
        self.assertEqual(chunkers.KINDS,
            frozenset({"function", "method", "constructor", "accessor", "closure"}))

    def test_lang_for_path(self):
        self.assertEqual(chunkers.lang_for_path("a/b.swift"), "swift")
        self.assertEqual(chunkers.lang_for_path("x.py"), "python")
        self.assertIsNone(chunkers.lang_for_path("x.rs"))       # not in M1 table
        self.assertIsNone(chunkers.lang_for_path("x.blade.php"))  # compound ext never a plain match

    def test_get_chunker_has_chunk_file(self):
        for lang in ("swift", "python"):
            self.assertTrue(callable(chunkers.get_chunker(lang).chunk_file))

    def test_chunker_version_changes_with_impl_version(self):
        v1 = chunkers.chunker_version("swift")
        self.assertRegex(v1, r"^[0-9a-f]{12}$")
        old = chunkers.LANGUAGE_TABLE["swift"]["impl_version"]
        try:
            chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = "TEST-BUMP"
            self.assertNotEqual(chunkers.chunker_version("swift"), v1)
        finally:
            chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = old

    def test_kind_values_are_in_KINDS(self):
        """Task 12 (Anatomy M1 milestone): every chunk BOTH chunkers emit,
        over their whole fixture corpora, uses only the frozen
        chunkers.KINDS vocabulary -- never a raw source-language keyword
        (Swift's "func"/"init"/"subscript"/"var" no longer leak through;
        see chunkers/swift.py's _map_kind)."""
        swift_kinds = set()
        swift = chunkers.get_chunker("swift")
        for f in sorted(FIXTURES.glob("*.swift")):
            result = swift.chunk_file(f.read_text(), f.name)
            for chunk in result.chunks:
                self.assertIn(chunk["kind"], chunkers.KINDS, (f.name, chunk))
                swift_kinds.add(chunk["kind"])
        python_kinds = set()
        for f in sorted(PY_FIXTURES.glob("*.py")):
            result = python_ast.chunk_file(f.read_text(), f.name)
            for chunk in result.chunks:
                self.assertIn(chunk["kind"], chunkers.KINDS, (f.name, chunk))
                python_kinds.add(chunk["kind"])
        # Not a vacuous pass on EITHER side: each corpus, independently,
        # actually exercises more than one kind value (a regression that
        # collapsed one chunker onto a single kind, e.g. Python losing its
        # function/method/constructor/accessor split, would still pass a
        # combined-set check if the other chunker alone covered >= 2).
        self.assertGreaterEqual(len(swift_kinds), 2, swift_kinds)
        self.assertGreaterEqual(len(python_kinds), 2, python_kinds)


class TestNestedFunctionImmediateParentKind(unittest.TestCase):
    """Ruling 8 (fix round 1 on Task 12, Anatomy M1 milestone): kind
    semantics are uniform across chunkers -- a function whose IMMEDIATE
    lexical parent is a function/method is kind "function" in every
    language, matching Python's rule (chunkers/python_ast.py's `_kind_for`
    looks at `parent_is_class`, the immediate parent only, not the full
    enclosing chain). Swift's original Task 12 mapping used the full
    enclosing-TYPE chain instead, so a func nested directly inside a
    method's body (never itself callable on the type -- a local function)
    was wrongly reported as "method" whenever some type enclosed the pair
    further out. Inline source, not a fixtures/code/*.swift file: adding
    a fixture there would pull this case into
    TestSwiftExtractionGolden's whole-corpus golden, which Ruling 8
    requires to stay byte-identical (no existing fixture has a func
    nested in a func body)."""

    NESTED_IN_METHOD = """
    class Foo {
        func outer() -> Int {
            func inner() -> Int { return 1 }
            return inner()
        }
    }
    """

    NESTED_IN_TOP_LEVEL_FUNC = """
    func topOuter() -> Int {
        func topInner() -> Int { return 2 }
        return topInner()
    }
    """

    @staticmethod
    def _by_qualified_name(text):
        swift = chunkers.get_chunker("swift")
        result = swift.chunk_file(text, "inline.swift")
        return {c["qualified_name"]: c["kind"] for c in result.chunks}

    def test_func_nested_in_a_method_body_is_function_not_method(self):
        by_name = self._by_qualified_name(self.NESTED_IN_METHOD)
        # The outer method itself: immediate parent IS the type -> method.
        self.assertEqual(by_name["Foo.outer"], "method")
        # The nested func: immediate parent is outer()'s OWN body, not
        # Foo directly -- never callable on Foo, so "function", even
        # though Foo still encloses it further out and its qualified_name
        # (unaffected by this ruling) keeps the "Foo.inner" form.
        self.assertEqual(by_name["Foo.inner"], "function")

    def test_func_nested_in_a_top_level_func_body_is_function(self):
        by_name = self._by_qualified_name(self.NESTED_IN_TOP_LEVEL_FUNC)
        self.assertEqual(by_name["topOuter"], "function")
        self.assertEqual(by_name["topInner"], "function")


def _dump_chunks(conn):
    rows = conn.execute(
        "SELECT path, lang, kind, symbol, qualified_name, signature, doc, start_line, end_line "
        "FROM chunks ORDER BY path, start_line, qualified_name"
    ).fetchall()
    return [dict(r) for r in rows]


class TestSwiftExtractionGolden(unittest.TestCase):
    """Index the whole fixtures/code/*.swift corpus (the same fixture files
    test_code_index.py's TestChunker* classes read individually) through the
    real code-reindex path and compare the resulting chunks-table rows
    against tests/goldens/swift_chunks_kind_v2.json -- regenerated (Task 12,
    Anatomy M1 milestone) from the NEW code once Swift's raw kinds
    (func/init/subscript/var) were mapped at emission onto the frozen
    chunkers.KINDS vocabulary (function/method/constructor/accessor). This
    golden replaces swift_chunks_pre_extraction.json (captured BEFORE the
    Swift lexer/walker moved to chunkers/swift.py, Task 2); the two goldens
    are IDENTICAL except for the "kind" column -- verified once, at
    generation time, by a one-off diff-with-kind-stripped script (see the
    Task 12 report) rather than re-checked here on every run, since the old
    golden no longer exists to diff against. Any further diff against
    THIS golden means the move/mapping wasn't mechanical: fix the code,
    never the golden."""

    def test_index_matches_kind_v2_golden(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            for f in sorted(FIXTURES.glob("*.swift")):
                shutil.copy(f, root / f.name)
            db = Path(td) / "idx-code.sqlite"
            rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)
            conn = memidx.open_code_db(db)
            got = _dump_chunks(conn)
            conn.close()
        with open(GOLDEN_PATH) as f:
            expected = json.load(f)
        self.assertEqual(got, expected)


class TestImplVersionBumpForcesSwiftRechunk(unittest.TestCase):
    """Task 12 (Anatomy M1 milestone): the whole point of this task being
    LAST -- the impl_version bump ("1" -> "2") alone must force every Swift
    file to re-chunk on the next reindex with NO source change, proving the
    Task 3 chunker_version-skip mechanism live end-to-end on a real
    taxonomy migration (not just a synthetic impl_version mutation, as
    TestChunkerVersionSkipDecision in test_code_index.py already covers).

    Simplest honest form (brief): pre-stamp file_sha.chunker_version with
    the OLD ("1"-era) chunker_version string computed the same way
    chunkers.chunker_version does, reindex unchanged Swift source, and
    assert changed > 0 -- then assert the re-chunked rows carry the NEW
    kind vocabulary, not the old raw Swift keywords."""

    def test_old_stamp_forces_rechunk_with_new_kind_vocabulary(self):
        # The "1"-era chunker_version string -- what a real pre-Task-12
        # database has stamped on every row -- computed via
        # chunkers.chunker_version itself (same pattern as
        # TestRegistry.test_chunker_version_changes_with_impl_version above)
        # rather than reimplementing its hash formula inline, so this test
        # can't silently drift from that formula.
        old_impl_version = chunkers.LANGUAGE_TABLE["swift"]["impl_version"]
        try:
            chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = "1"
            old_chunker_version = chunkers.chunker_version("swift")
        finally:
            chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = old_impl_version
        new_chunker_version = chunkers.chunker_version("swift")
        self.assertNotEqual(old_chunker_version, new_chunker_version)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            conn.execute("UPDATE file_sha SET chunker_version=?", (old_chunker_version,))
            conn.commit()
            conn.close()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)
            m = re.search(
                r"(\d+) files scanned, (\d+) added, (\d+) changed, (\d+) unchanged",
                buf.getvalue(),
            )
            self.assertIsNotNone(m, buf.getvalue())
            self.assertGreater(int(m.group(3)), 0,
                "an old-era chunker_version stamp must force a re-chunk even "
                "though the source file itself never changed")
            self.assertEqual(int(m.group(4)), 0)

            conn = memidx.open_code_db(db)
            stamp = conn.execute(
                "SELECT chunker_version FROM file_sha WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()
            kind_rows = conn.execute(
                "SELECT qualified_name, kind FROM chunks WHERE path=?", ("NestedTypes.swift",)
            ).fetchall()
            conn.close()
            self.assertEqual(stamp["chunker_version"], new_chunker_version)
            # The whole point: no raw Swift keyword ("func") survives the
            # re-chunk. An exact-dict comparison (not just "each kind is
            # SOME chunkers.KINDS member", which even a stray leftover raw
            # value could accidentally satisfy if KINDS itself were ever
            # misdefined) pins every one of NestedTypes.swift's three
            # methods to its precise new-taxonomy kind.
            by_qname = {r["qualified_name"]: r["kind"] for r in kind_rows}
            self.assertEqual(by_qname, {
                "Outer.Inner.innerFunc": "method",
                "Outer.outerFunc": "method",
                "Outer.extFunc": "method",
            })


def _recall_tuples(chunks):
    """(kind, qualified_name, start_line, end_line) per chunk, in the order
    chunk_file returned them -- the exact-tuple recall contract the brief
    requires; catches kind-mapping, qualification, and line-number drift
    together (query/AST drift trips the suite per the brief's capture-count
    golden requirement)."""
    return [(c["kind"], c["qualified_name"], c["start_line"], c["end_line"]) for c in chunks]


class TestPythonAstChunker(unittest.TestCase):
    """Gold fixtures under tests/fixtures/python_corpus/ (Task 4 of the
    Anatomy M1 milestone): exact (kind, qualified_name, start_line, end_line)
    recall per fixture file, a capture-count golden per file, and one exact
    ChunkResult assertion for the syntactically broken file (fail-open, no
    exception escapes)."""

    def test_basic_functions_recall(self):
        text = (PY_FIXTURES / "basic_functions.py").read_text()
        result = python_ast.chunk_file(text, "basic_functions.py")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.gaps, [])
        self.assertEqual(len(result.chunks), 5)  # capture-count golden
        self.assertEqual(
            _recall_tuples(result.chunks),
            [
                ("function", "plain_function", 4, 9),
                ("function", "fetch_data", 12, 14),
                ("function", "decorated_standalone", 17, 20),  # first decorator's line
                ("function", "outer_with_nested", 23, 29),
                ("function", "outer_with_nested.inner", 26, 27),  # nested, parent-qualified
            ],
        )
        for chunk in result.chunks:
            self.assertEqual(chunk["lang"], "python")
            self.assertIn(chunk["kind"], chunkers.KINDS)

    def test_basic_functions_doc_and_signature(self):
        text = (PY_FIXTURES / "basic_functions.py").read_text()
        result = python_ast.chunk_file(text, "basic_functions.py")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        # doc = ast.get_docstring's first line only -- the second docstring
        # paragraph ("More detail...") must NOT leak into `doc`.
        self.assertEqual(by_qname["plain_function"]["doc"], "First line of the docstring.")
        self.assertEqual(by_qname["plain_function"]["signature"], "def plain_function(a, b=1)")
        self.assertEqual(
            by_qname["fetch_data"]["signature"], "async def fetch_data(url: str) -> str"
        )
        self.assertEqual(by_qname["decorated_standalone"]["symbol"], "decorated_standalone")

    def test_classes_recall(self):
        text = (PY_FIXTURES / "classes.py").read_text()
        result = python_ast.chunk_file(text, "classes.py")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 6)  # capture-count golden
        self.assertEqual(
            _recall_tuples(result.chunks),
            [
                ("constructor", "Widget.__init__", 7, 9),
                ("method", "Widget.render", 11, 13),
                ("constructor", "Gadget.__init__", 19, 20),
                ("accessor", "Gadget.value", 22, 25),   # @property getter
                ("accessor", "Gadget.value", 27, 29),   # @value.setter
                ("method", "Outer.Inner.greet", 38, 39),  # nested class, never itself a chunk
            ],
        )
        # ClassDef is NEVER a chunk -- no symbol/qualified_name in the result
        # names a bare class ("Widget", "Gadget", "Outer", "Inner").
        qnames = {c["qualified_name"] for c in result.chunks}
        self.assertNotIn("Widget", qnames)
        self.assertNotIn("Outer", qnames)
        self.assertNotIn("Outer.Inner", qnames)

    def test_broken_file_fails_open(self):
        text = (PY_FIXTURES / "broken.py").read_text()
        result = python_ast.chunk_file(text, "broken.py")
        self.assertEqual(result.chunks, [])
        self.assertEqual(result.gaps, [(1, text.count("\n") + 1, "syntax-error")])
        self.assertEqual(result.status, "failed")

    def test_declared_symbols_functions(self):
        text = (PY_FIXTURES / "basic_functions.py").read_text()
        got = python_ast.declared_symbols(text)
        self.assertEqual(
            got,
            [
                ("plain_function", "plain_function"),
                ("fetch_data", "fetch_data"),
                ("decorated_standalone", "decorated_standalone"),
                ("outer_with_nested", "outer_with_nested"),
                ("inner", "outer_with_nested.inner"),
            ],
        )

    def test_declared_symbols_includes_class_container_names(self):
        # Mirrors chunkers.swift's declared_symbol_names container-name
        # inclusion (memidx.py): a #symbol fragment may name the class
        # itself, not just a member.
        text = (PY_FIXTURES / "classes.py").read_text()
        got = python_ast.declared_symbols(text)
        self.assertIn(("Widget", "Widget"), got)
        self.assertIn(("Outer", "Outer"), got)
        self.assertIn(("Inner", "Outer.Inner"), got)
        self.assertIn(("greet", "Outer.Inner.greet"), got)
        self.assertIn(("__init__", "Widget.__init__"), got)


class TestFragmentDeclaredInTextDispatch(unittest.TestCase):
    """Task 6: memidx.fragment_declared_in_text(frag, text, rel_path=...)
    dispatches per chunkers.lang_for_path(rel_path) -- a .py rel_path routes
    through chunkers.python_ast.declared_symbols (already includes
    class-container names per its own docstring/tests above, so no separate
    container pass is added here) evaluated with the SAME fragment_matches_symbol
    predicate the Swift path and code-search's runtime attachment both use.
    Every other rel_path (including the default "x.swift") keeps the
    Swift-lexer path byte-identical to before this task."""

    def test_python_function_fragment_recognized_with_py_rel_path(self):
        # Step 1's red test (brief): fails today -- fragment_declared_in_text
        # takes no rel_path param at all, and a Swift-syntax lexer scan of
        # Python text never recognizes `def name():`.
        text = "def parse_frontmatter():\n    pass\n"
        self.assertTrue(
            memidx.fragment_declared_in_text("parse_frontmatter", text, rel_path="memidx.py")
        )

    def test_python_function_fragment_not_recognized_without_py_rel_path(self):
        # Same text, default rel_path ("x.swift") -- the Swift lexer path,
        # which has no notion of `def`. Documents that dispatch is driven by
        # rel_path, not a guess from the text's own contents.
        text = "def parse_frontmatter():\n    pass\n"
        self.assertFalse(memidx.fragment_declared_in_text("parse_frontmatter", text))

    def test_python_class_container_name_recognized_via_declared_symbols(self):
        text = (PY_FIXTURES / "classes.py").read_text()
        self.assertTrue(memidx.fragment_declared_in_text("Widget", text, rel_path="classes.py"))
        self.assertTrue(
            memidx.fragment_declared_in_text("Outer.Inner.greet", text, rel_path="classes.py")
        )

    def test_swift_path_regression_unchanged(self):
        # Existing behavior preserved exactly: a Swift fragment case that
        # passes today (default rel_path, and an explicit .swift rel_path)
        # still passes after the dispatch is added.
        text = (FIXTURES / "NestedTypes.swift").read_text()
        self.assertTrue(memidx.fragment_declared_in_text("Outer.outerFunc", text))
        self.assertTrue(
            memidx.fragment_declared_in_text("Outer.outerFunc", text, rel_path="NestedTypes.swift")
        )
