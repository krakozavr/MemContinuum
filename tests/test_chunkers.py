import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import chunkers
import memidx

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_code_index import FIXTURES, code_reindex  # noqa: E402 -- reuse the Swift fixture
# corpus and code-reindex helper test_code_index.py already builds, rather than duplicating
# the corpus text here.

GOLDEN_PATH = TESTS_DIR / "goldens" / "swift_chunks_pre_extraction.json"


def _backends_available():
    return bool(
        importlib.util.find_spec("chunkers.swift")
        and importlib.util.find_spec("chunkers.python_ast")
    )


class TestRegistry(unittest.TestCase):
    def test_kinds_frozen(self):
        self.assertEqual(chunkers.KINDS,
            frozenset({"function", "method", "constructor", "accessor", "closure"}))

    def test_lang_for_path(self):
        self.assertEqual(chunkers.lang_for_path("a/b.swift"), "swift")
        self.assertEqual(chunkers.lang_for_path("x.py"), "python")
        self.assertIsNone(chunkers.lang_for_path("x.rs"))       # not in M1 table
        self.assertIsNone(chunkers.lang_for_path("x.blade.php"))  # compound ext never a plain match

    @unittest.skipUnless(_backends_available(),
        "chunkers.swift / chunkers.python_ast land in Tasks 2/4")
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
