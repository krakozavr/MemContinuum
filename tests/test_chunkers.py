import json
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

GOLDEN_PATH = TESTS_DIR / "goldens" / "swift_chunks_pre_extraction.json"
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
    against tests/goldens/swift_chunks_pre_extraction.json -- captured from
    memidx.py BEFORE the Swift lexer/walker moved to chunkers/swift.py
    (Task 2 of the Anatomy M1 milestone). Any diff here means the move
    wasn't mechanical: fix the move, never the golden."""

    def test_index_matches_pre_extraction_golden(self):
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
