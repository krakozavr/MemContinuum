import contextlib
import hashlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chunkers
import chunkers.python_ast as python_ast
import chunkers.treesitter
import memidx

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_code_index import FIXTURES, code_reindex, ns  # noqa: E402 -- reuse the Swift fixture
# corpus and code-reindex helper test_code_index.py already builds, rather than duplicating
# the corpus text here.

GOLDEN_PATH = TESTS_DIR / "goldens" / "swift_chunks_kind_v2.json"
PRE_EXTRACTION_GOLDEN_PATH = TESTS_DIR / "goldens" / "swift_chunks_pre_extraction.json"
FINGERPRINT_GOLDEN_PATH = TESTS_DIR / "goldens" / "chunker_source_fingerprints.json"
PY_FIXTURES = TESTS_DIR / "fixtures" / "python_corpus"
REPO_ROOT = TESTS_DIR.parent

VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
_SKIP_NO_VENV = ("MEMCONTINUUM_PYTHON not set -- tree-sitter tests need the fixed venv "
                 "with the seven pins installed (Task 1's coordinator step)")


class TestRegistry(unittest.TestCase):
    def test_kinds_frozen(self):
        self.assertEqual(chunkers.KINDS,
            frozenset({"function", "method", "constructor", "accessor", "closure"}))

    def test_lang_for_path(self):
        self.assertEqual(chunkers.lang_for_path("a/b.swift"), "swift")
        self.assertEqual(chunkers.lang_for_path("x.py"), "python")
        self.assertIsNone(chunkers.lang_for_path("x.go"))       # not in the table
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
    golden supersedes swift_chunks_pre_extraction.json (captured BEFORE the
    Swift lexer/walker moved to chunkers/swift.py, Task 2); the two goldens
    are IDENTICAL except for the "kind" column -- a claim that used to rest
    on a one-off script and a report, and is now re-checked on every run by
    TestSwiftGoldenByteIdentityProof below, the old golden having been
    restored (fix wave C4). Any further diff against THIS golden means the
    move/mapping wasn't mechanical: fix the code, never the golden."""

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


class TestSwiftGoldenByteIdentityProof(unittest.TestCase):
    """C4 (Anatomy M1 fix wave, Codex): the proof that moving the Swift
    walker out of memidx.py into chunkers/swift.py changed NOTHING but the
    `kind` column was, until now, a one-off script run at Task 12 time and
    a sentence in a report. Anyone auditing this branch had to take that
    on trust, or go digging in git history for a golden that had been
    deleted.

    tests/goldens/swift_chunks_pre_extraction.json is restored (recovered
    verbatim from commit 6c98c7a) and the proof now runs on every suite
    invocation: strip "kind" from both goldens, assert the rest is equal.
    The two files together are the permanent, checkable record that the
    extraction was mechanical and the taxonomy change touched exactly one
    column."""

    def _load(self, path):
        with open(path) as f:
            return json.load(f)

    def test_goldens_are_identical_apart_from_the_kind_column(self):
        pre = self._load(PRE_EXTRACTION_GOLDEN_PATH)
        v2 = self._load(GOLDEN_PATH)

        def strip_kind(rows):
            return [{k: v for k, v in row.items() if k != "kind"} for row in rows]

        self.assertEqual(len(pre), len(v2))
        self.assertEqual(strip_kind(pre), strip_kind(v2))

    def test_the_kind_column_is_exactly_what_changed(self):
        """Guards the other direction: the two goldens must genuinely
        DIFFER in `kind` (raw Swift keywords before, the frozen vocabulary
        after), so the equality above can never be satisfied by two copies
        of the same file."""
        pre = self._load(PRE_EXTRACTION_GOLDEN_PATH)
        v2 = self._load(GOLDEN_PATH)
        self.assertEqual({row["kind"] for row in pre}, {"func", "init", "subscript", "var"})
        self.assertTrue({row["kind"] for row in v2} <= chunkers.KINDS)
        self.assertNotEqual(pre, v2)


class TestChunkerSourceFingerprints(unittest.TestCase):
    """I4 (Anatomy M1 fix wave): impl_version stays a MANUAL field --
    deriving it from the module's bytes would force a full reindex of every
    project on a whitespace edit or a comment fix, which is exactly the
    cost the version stamp exists to avoid. The risk that leaves is the
    discipline one: someone changes a chunker's OUTPUT and forgets to bump
    impl_version, so every already-indexed project silently keeps serving
    chunks the current code would no longer produce.

    This test is the tripwire for that. It fingerprints each backend's
    source and compares against a recorded golden; any edit to those files
    fails here with instructions. Editing a comment will trip it too --
    that is deliberate. A noisy prompt to think about the version stamp
    costs one golden regeneration; a missed bump costs every project's
    index silently."""

    BACKENDS = ("chunkers/swift.py", "chunkers/python_ast.py")

    @staticmethod
    def _fingerprint(rel):
        return hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()

    def test_backend_sources_match_the_recorded_fingerprints(self):
        with open(FINGERPRINT_GOLDEN_PATH) as f:
            golden = json.load(f)
        got = {rel: self._fingerprint(rel) for rel in self.BACKENDS}
        self.assertEqual(
            got, golden,
            "chunker source changed: bump impl_version AND regenerate the "
            "fingerprint golden (tests/goldens/chunker_source_fingerprints.json) "
            "-- if the change cannot alter any chunk this backend emits (a "
            "comment, a docstring), regenerate the golden alone and say so in "
            "the commit message.",
        )

    def test_the_golden_covers_every_native_backend_in_the_table(self):
        """A new LANGUAGE_TABLE row must not slip past the tripwire just by
        not being listed here."""
        with open(FINGERPRINT_GOLDEN_PATH) as f:
            golden = json.load(f)
        expected = {
            row["module"].replace(".", "/") + ".py"
            for row in chunkers.LANGUAGE_TABLE.values()
            if row["backend"] == "native"
        }
        self.assertEqual(set(golden), expected)


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

    def test_nested_def_in_method_recall(self):
        """T2 (Anatomy M1 fix wave): the one qualification shape the other
        fixtures miss -- a def nested inside a METHOD. Pinned as the
        chunker ACTUALLY emits it: the immediate parent is a function, not
        a class, so `inner` is kind "function" (never "method"), and its
        qualified_name carries the whole mixed stack, "Outer.method.inner".
        """
        text = (PY_FIXTURES / "nested_in_method.py").read_text()
        result = python_ast.chunk_file(text, "nested_in_method.py")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.gaps, [])
        self.assertEqual(len(result.chunks), 3)  # capture-count golden
        self.assertEqual(
            _recall_tuples(result.chunks),
            [
                ("method", "Outer.method", 16, 23),
                ("function", "Outer.method.inner", 19, 21),
                ("method", "Outer.plain", 25, 26),
            ],
        )

    def test_nested_def_in_method_declared_symbols(self):
        text = (PY_FIXTURES / "nested_in_method.py").read_text()
        self.assertEqual(
            python_ast.declared_symbols(text),
            [
                ("Outer", "Outer"),
                ("method", "Outer.method"),
                ("inner", "Outer.method.inner"),
                ("plain", "Outer.plain"),
            ],
        )

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
        # Mirrors chunkers.swift.declared_symbols' container-name
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
    """Task 6: memidx.fragment_declared_in_text(frag, text, rel_path) --
    rel_path is REQUIRED (Task 6 drops the old "x.swift" default) --
    dispatches per chunkers.lang_for_path(rel_path), falling back to a
    shebang sniff of text's first line for an extensionless rel_path, and
    returning False when neither resolves a language (no vocabulary). A
    .py rel_path routes through chunkers.python_ast.declared_symbols
    (already includes class-container names per its own docstring/tests
    above, so no separate container pass is added here) evaluated with the
    SAME fragment_matches_symbol predicate the Swift path and
    code-search's runtime attachment both use."""

    def test_python_function_fragment_recognized_with_py_rel_path(self):
        # Step 1's red test (brief): fails today -- fragment_declared_in_text
        # takes no rel_path param at all, and a Swift-syntax lexer scan of
        # Python text never recognizes `def name():`.
        text = "def parse_frontmatter():\n    pass\n"
        self.assertTrue(
            memidx.fragment_declared_in_text("parse_frontmatter", text, rel_path="memidx.py")
        )

    def test_python_function_fragment_not_recognized_with_swift_rel_path(self):
        # Same text, an explicit .swift rel_path -- the Swift lexer path,
        # which has no notion of `def`. Documents that dispatch is driven by
        # rel_path, not a guess from the text's own contents.
        text = "def parse_frontmatter():\n    pass\n"
        self.assertFalse(memidx.fragment_declared_in_text("parse_frontmatter", text, rel_path="x.swift"))

    def test_python_class_container_name_recognized_via_declared_symbols(self):
        text = (PY_FIXTURES / "classes.py").read_text()
        self.assertTrue(memidx.fragment_declared_in_text("Widget", text, rel_path="classes.py"))
        self.assertTrue(
            memidx.fragment_declared_in_text("Outer.Inner.greet", text, rel_path="classes.py")
        )

    def test_swift_path_regression_unchanged(self):
        # Existing behavior preserved exactly: a Swift fragment case that
        # passed before this task still passes after the dispatch is added.
        text = (FIXTURES / "NestedTypes.swift").read_text()
        self.assertTrue(
            memidx.fragment_declared_in_text("Outer.outerFunc", text, rel_path="NestedTypes.swift")
        )

    def test_no_extension_no_shebang_returns_false(self):
        # rel_path has no extension and text carries no recognizable
        # shebang -- neither resolution path finds a language, so the
        # answer is False (no vocabulary), not an exception.
        text = "def parse_frontmatter():\n    pass\n"
        self.assertFalse(memidx.fragment_declared_in_text("parse_frontmatter", text, rel_path="tool"))

    def test_unknown_extension_returns_false_without_shebang_fallback(self):
        # An extension IS present but matches no LANGUAGE_TABLE row -- the
        # shebang fallback only applies to an EXTENSIONLESS rel_path, so
        # this must not fall through to sniffing the text either.
        text = "#!/usr/bin/env python3\ndef parse_frontmatter():\n    pass\n"
        self.assertFalse(memidx.fragment_declared_in_text("parse_frontmatter", text, rel_path="tool.txt"))


class TestSwiftDeclaredSymbolsBackend(unittest.TestCase):
    """I3 (Anatomy M1 fix wave): `declared_symbols` is now part of the
    registry contract, exposed by every backend, so
    memidx.fragment_declared_in_text can route generically instead of
    carrying an `if lang == "python"` branch and an inline Swift
    container-name pass. Swift's implementation returns the same
    (symbol, qualified_name) pair shape Python's does, and memidx no
    longer imports any Swift lexer internals."""

    def test_swift_declared_symbols_returns_pairs_covering_members(self):
        text = (FIXTURES / "NestedTypes.swift").read_text()
        pairs = chunkers.swift.declared_symbols(text)
        for pair in pairs:
            self.assertIsInstance(pair, tuple)
            self.assertEqual(len(pair), 2)
        self.assertIn(("outerFunc", "Outer.outerFunc"), pairs)
        self.assertIn(("innerFunc", "Outer.Inner.innerFunc"), pairs)

    def test_swift_declared_symbols_includes_container_type_names(self):
        text = (FIXTURES / "NestedTypes.swift").read_text()
        names = {symbol for symbol, _q in chunkers.swift.declared_symbols(text)}
        self.assertIn("Outer", names)
        self.assertIn("Inner", names)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_every_language_table_backend_exposes_declared_symbols(self):
        # Method-level, not class-level: this is the one test in this class
        # that loops every LANGUAGE_TABLE row (tree-sitter rows included)
        # and calls get_chunker on each, so it alone needs the grammar
        # wheels -- the other four tests in this class are pure Swift/
        # memidx and must keep running in a no-venv checkout.
        for lang in chunkers.LANGUAGE_TABLE:
            backend = chunkers.get_chunker(lang)
            self.assertTrue(
                callable(getattr(backend, "declared_symbols", None)),
                f"{lang} backend must expose declared_symbols (registry contract)",
            )
            self.assertTrue(callable(getattr(backend, "chunk_file", None)))

    def test_memidx_no_longer_imports_swift_lexer_internals(self):
        for name in ("_build_mask_and_match_dict", "_KEYWORD_RE",
                     "_container_type_name", "_extract_decls",
                     "declared_symbol_names"):
            self.assertFalse(
                hasattr(memidx, name),
                f"memidx.{name} should be gone -- the vocabulary check routes "
                "through the backend's own declared_symbols now (I3)",
            )
        # chunk_source stays: existing tests import it as memidx.chunk_source.
        self.assertTrue(callable(memidx.chunk_source))

    def test_dispatch_has_no_per_language_branch_in_the_source(self):
        """The point of I3 is structural, so assert the structure: the
        function body must not name a language."""
        import inspect
        src = inspect.getsource(memidx.fragment_declared_in_text)
        body = src.split('"""')[-1]
        self.assertNotIn('== "python"', body, body)
        self.assertIn("get_chunker", body, body)


class TestGetChunkerFailsOpenOnAnyBackendException(unittest.TestCase):
    """Fix-wave item 2: get_chunker wrapped only ImportError before this
    fix -- any OTHER exception a backend module raises at import time (a
    provider's own init code raising RuntimeError, OSError, a custom
    exception type, ...) propagated straight through importlib.import_module
    and out of get_chunker uncaught, crashing backend_availability() and
    everything built on it (code_index_report, cmd_code_search, `why`'s
    code-index fast path) instead of degrading gracefully."""

    def _broken_python_import(self):
        """Patches chunkers.importlib.import_module so importing
        chunkers.python_ast specifically raises a RuntimeError (a stand-in
        for "this backend's own init code failed", not a missing module --
        the real chunkers.python_ast module exists and imports fine
        outside this patch); every other import_module call (swift, or
        python_ast's own internal imports once it's already loaded)
        passes through to the real importlib unchanged."""
        real_import_module = chunkers.importlib.import_module

        def fake(name, *a, **kw):
            if name == "chunkers.python_ast":
                raise RuntimeError("provider init failed")
            return real_import_module(name, *a, **kw)

        return mock.patch.object(chunkers.importlib, "import_module", side_effect=fake)

    def test_get_chunker_wraps_any_exception_not_just_import_error(self):
        with self._broken_python_import():
            with self.assertRaises(chunkers.BackendUnavailable) as ctx:
                chunkers.get_chunker("python")
            self.assertIn("RuntimeError", str(ctx.exception))
            self.assertIn("provider init failed", str(ctx.exception))
            avail = chunkers.backend_availability()  # must not raise either
        self.assertIn("python=missing", avail)
        self.assertIn("swift=ok", avail)

    def test_code_search_and_why_fail_open_when_a_backend_raises_on_import(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            root = Path(td) / "code"
            root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            project = "fail-open-backend-import"
            db_path = home / f"{project}-code.sqlite"
            prev_home = os.environ.get("MEMCONTINUUM_HOME")
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                with self._broken_python_import():
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = code_reindex(root, db_path, project=project, no_embed=True, lang="python")
                    self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
                    self.assertIn("1 not indexed", out.getvalue())

                    conn = memidx.open_code_db(db_path)
                    report = memidx.code_index_report(conn, project)
                    conn.close()
                    self.assertEqual(report["state"], "degraded", report)

                    buf, errbuf = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(errbuf):
                        rc = memidx.cmd_code_search(
                            ns(db=str(db_path), project=project, query="f", mode="fts", limit=5, json=False)
                        )
                    self.assertEqual(rc, 0, buf.getvalue() + errbuf.getvalue())
                    self.assertIn("not indexed", errbuf.getvalue())

                    # `why`'s code-index fast path consults MEMCONTINUUM_HOME's
                    # own <project>-code.sqlite (never --db) -- db_path above
                    # IS that path, so this exercises the real fast path, not
                    # a disk-scan fallback called in isolation. The point
                    # here is fail-OPEN, not a successful resolution: with
                    # the python backend genuinely broken, the disk-scan
                    # fallback's own declared-symbol check
                    # (fragment_declared_in_text) can't chunk python either,
                    # so the honest answer is still None -- what matters,
                    # and what the pre-fix get_chunker broke, is that this
                    # call returns None instead of letting the RuntimeError
                    # propagate all the way up through backend_availability()
                    # -> code_index_report() -> _resolve_symbol_via_code_index.
                    resolved = memidx.resolve_symbol_to_path(root, "f", project=project)
                    self.assertIsNone(resolved)
            finally:
                if prev_home is None:
                    os.environ.pop("MEMCONTINUUM_HOME", None)
                else:
                    os.environ["MEMCONTINUUM_HOME"] = prev_home


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTreeSitterRegistry(unittest.TestCase):
    TS_LANGS = ("javascript", "typescript", "tsx", "java", "php", "rust", "lua")

    def test_every_tree_sitter_row_present_with_required_keys(self):
        required = {"backend", "module", "grammar_module", "grammar_pin", "runtime_pin",
                    "query_file", "extensions", "impl_version", "containers"}
        for lang in self.TS_LANGS:
            row = chunkers.LANGUAGE_TABLE[lang]
            self.assertEqual(row["backend"], "tree-sitter")
            self.assertEqual(row["module"], "chunkers.treesitter")
            self.assertIsInstance(row["language_fn"], str)   # ruling 83: never a dict, ts/tsx are separate rows
            missing = required - set(row)
            self.assertFalse(missing, f"{lang} row missing {missing}")

    def test_typescript_and_tsx_are_distinct_rows_sharing_the_query_file(self):
        ts_row = chunkers.LANGUAGE_TABLE["typescript"]
        tsx_row = chunkers.LANGUAGE_TABLE["tsx"]
        self.assertEqual(ts_row["language_fn"], "language_typescript")
        self.assertEqual(tsx_row["language_fn"], "language_tsx")
        self.assertEqual(ts_row["query_file"], tsx_row["query_file"])
        self.assertEqual(ts_row["extensions"], (".ts",))
        self.assertEqual(tsx_row["extensions"], (".tsx",))

    def test_chunkers_treesitter_imports_with_no_grammar_wheel_present(self):
        # Binding point 1: importing the registry module itself must never
        # require tree_sitter -- BackendUnavailable is raised lazily, at
        # for_language() time, not at import time.
        with mock.patch.dict(sys.modules, {"tree_sitter": None, "tree_sitter_lua": None}):
            importlib.reload(chunkers.treesitter)
        importlib.reload(chunkers.treesitter)   # restore normal state for later tests

    def test_get_chunker_wraps_missing_wheel_as_backend_unavailable(self):
        chunkers.treesitter.reset_cache()
        with mock.patch.dict(sys.modules, {"tree_sitter_lua": None}):
            with self.assertRaises(chunkers.BackendUnavailable):
                chunkers.get_chunker("lua")
        chunkers.treesitter.reset_cache()

    def test_backend_availability_reports_all_seven_rows_including_both_ts_dialects(self):
        chunkers.treesitter.reset_cache()
        with mock.patch.dict(sys.modules, {"tree_sitter_lua": None}):
            avail = chunkers.backend_availability()
        chunkers.treesitter.reset_cache()
        self.assertIn("lua=missing", avail)
        # every wheel IS present (Task 1's coordinator step) except the one
        # mocked out above -- proving this isn't a blanket
        # "everything unavailable" false negative, and specifically proving
        # ruling 83's fix: typescript AND tsx both report ok independently.
        for lang in ("javascript", "typescript", "tsx", "java", "php", "rust"):
            self.assertIn(f"{lang}=ok", avail, avail)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTreeSitterFingerprint(unittest.TestCase):
    def test_chunker_version_never_imports_tree_sitter(self):
        with mock.patch.dict(sys.modules, {"tree_sitter": None, "tree_sitter_javascript": None}):
            v = chunkers.chunker_version("javascript")
        self.assertEqual(len(v), 12)

    def test_chunker_version_changes_when_query_file_changes(self):
        qpath = Path(chunkers.treesitter.QUERY_DIR) / chunkers.LANGUAGE_TABLE["javascript"]["query_file"]
        original = qpath.read_bytes()
        before = chunkers.chunker_version("javascript")
        try:
            qpath.write_bytes(original + b"\n; probe\n")
            after = chunkers.chunker_version("javascript")
            self.assertNotEqual(before, after)
        finally:
            qpath.write_bytes(original)

    def test_typescript_and_tsx_are_independent_cached_instances(self):
        chunkers.treesitter.reset_cache()
        ts_chunker = chunkers.treesitter.for_language("typescript")
        tsx_chunker = chunkers.treesitter.for_language("tsx")
        self.assertIsNot(ts_chunker, tsx_chunker)
        self.assertIs(chunkers.treesitter.for_language("typescript"), ts_chunker)   # cached
        chunkers.treesitter.reset_cache()


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTreeSitterFileSizeCap(unittest.TestCase):
    """Revision 4, binding ruling 87: signal.alarm is GONE (measured: it
    does not bound wall-clock parse time -- see the module docstring and
    binding ruling 87's own probe). The replacement is a per-file byte
    cap checked BEFORE `parser.parse()` is ever called."""

    def test_default_cap_is_one_mebibyte(self):
        self.assertEqual(chunkers.treesitter.DEFAULT_MAX_PARSE_BYTES, 1024 * 1024)

    def test_file_under_cap_parses_normally(self):
        chunkers.treesitter.reset_cache()
        c = chunkers.treesitter.for_language("javascript")
        result = c.chunk_file("function f(){}\n", "f.js")
        self.assertEqual(result.status, "ok")
        chunkers.treesitter.reset_cache()

    def test_file_over_cap_raises_too_large_before_parsing_ever_runs(self):
        chunkers.treesitter.reset_cache()
        c = chunkers.treesitter.for_language("javascript")
        big = "function f(){}\n" + ("// pad\n" * 200000)   # > 1 MiB
        self.assertGreater(len(big.encode("utf-8")), chunkers.treesitter.DEFAULT_MAX_PARSE_BYTES)
        with mock.patch("tree_sitter.Parser.parse") as spy:
            with self.assertRaises(chunkers.treesitter.TreeSitterFileTooLarge) as ctx:
                c.chunk_file(big, "big.js")
            spy.assert_not_called()   # the cap check runs BEFORE the parser is ever touched
        self.assertIn("too large", str(ctx.exception))
        self.assertIn(str(chunkers.treesitter.DEFAULT_MAX_PARSE_BYTES), str(ctx.exception))
        chunkers.treesitter.reset_cache()

    def test_env_override_lowers_the_cap(self):
        chunkers.treesitter.reset_cache()
        c = chunkers.treesitter.for_language("javascript")
        text = "function f(){}\n" * 100   # well under 1 MiB, well over 10 bytes
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_MAX_PARSE_BYTES": "10"}):
            with self.assertRaises(chunkers.treesitter.TreeSitterFileTooLarge):
                c.chunk_file(text, "f.js")
        # env override does not leak into a later call once unset
        result = c.chunk_file("function f(){}\n", "f.js")
        self.assertEqual(result.status, "ok")
        chunkers.treesitter.reset_cache()

    def test_per_language_row_override_wins_over_env_and_default(self):
        row = dict(chunkers.LANGUAGE_TABLE["javascript"])
        row["max_bytes"] = 5
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_MAX_PARSE_BYTES": "999999999"}):
            self.assertEqual(chunkers.treesitter.max_parse_bytes(row), 5)   # row wins over env
        no_override_row = dict(chunkers.LANGUAGE_TABLE["javascript"])
        no_override_row.pop("max_bytes", None)
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_MAX_PARSE_BYTES": "42"}):
            self.assertEqual(chunkers.treesitter.max_parse_bytes(no_override_row), 42)   # env wins over default


class TestTreeSitterHookIsolation(unittest.TestCase):
    """Revision 3, binding addition c (fixed in revision 4, binding ruling
    87): no hook may ever import chunkers.treesitter (directly, or
    transitively through `import memidx`/`import chunkers`)."""

    def test_hook_reachable_subcommands_never_load_chunkers_treesitter(self):
        # Revision 4: REPLACES revision 3's substring grep of hooks/*.sh,
        # which false-positived against hooks/newfile-nudge.sh's own
        # human-facing message text (it tells a PERSON to run `code-search`
        # themselves -- the hook never invokes it) and would have failed on
        # day one, before Task 2 changes a single line of hook code. This
        # version runs the actual memidx.py subcommands hooks/*.sh invoke
        # (grep-confirmed against hooks/*.sh at plan-write time: `for-path`,
        # `reindex` -- with `--auto`, the internal/hook-use flag -- and
        # `unmapped`; never `code-reindex`/`code-search`/`backend-preflight`)
        # in one subprocess, then checks sys.modules directly -- the exact
        # invariant, not a proxy for it. Command exit codes are irrelevant
        # here; only whether the import happened matters.
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            store.mkdir()
            note = store / "note.md"
            note.write_text("---\nid: T-0001\nkind: decision\n---\n\n# A note\n\nBody text.\n")
            db = str(Path(td) / "idx.sqlite")
            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['reindex', '--root', %r, '--project', 'p', '--db', %r, '--auto']); "
                "memidx.main(['for-path', %r, '--root', %r, '--project', 'p', '--db', %r]); "
                "memidx.main(['unmapped', '--root', %r, '--project', 'p', '--db', %r, %r]); "
                "print('RESULT:' + str('chunkers.treesitter' in sys.modules))"
            ) % (
                str(REPO_ROOT),
                str(store), db,
                str(note), str(store), db,
                str(store), db, str(note),
            )
            r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
            self.assertIn("RESULT:False", r.stdout, r.stdout + r.stderr)

    def test_fresh_import_of_memidx_never_loads_chunkers_treesitter(self):
        # A subprocess, not an in-process reload, so this reflects what a
        # hook's own cold-start `import memidx` actually pulls in.
        script = (
            "import sys; sys.path.insert(0, %r); import memidx; "
            "print('chunkers.treesitter' in sys.modules)"
        ) % (str(REPO_ROOT),)
        r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False", r.stdout + r.stderr)


class TestTreeSitterDedupPriority(unittest.TestCase):
    def test_same_span_two_kinds_keeps_the_more_specific_one(self):
        entries = [
            {"key": (10, 40), "kind": "method", "symbol": "value", "start_line": 2, "end_line": 2},
            {"key": (10, 40), "kind": "accessor", "symbol": "value", "start_line": 2, "end_line": 2},
        ]
        deduped = chunkers.treesitter.dedup_by_priority(entries)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["kind"], "accessor")

    def test_containing_span_with_the_same_symbol_is_dropped(self):
        # Ruling 84: an outer wrapper match (e.g. export_statement) and an
        # inner definition match (e.g. function_declaration) can both name
        # symbol "Named" with DIFFERENT spans, the outer containing the
        # inner -- keep only the innermost.
        entries = [
            {"key": (0, 50), "kind": "function", "symbol": "Named", "start_line": 1, "end_line": 3},
            {"key": (7, 45), "kind": "function", "symbol": "Named", "start_line": 1, "end_line": 3},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["key"], (7, 45))

    def test_containing_span_with_a_different_symbol_is_kept(self):
        # A method inside a class is CONTAINED by the class's own span, but
        # they name different symbols -- containment alone must never merge
        # unrelated captures (this is not the same hazard as #2 above).
        entries = [
            {"key": (0, 100), "kind": "function", "symbol": "Outer", "start_line": 1, "end_line": 10},
            {"key": (10, 40), "kind": "method", "symbol": "Outer.inner", "start_line": 2, "end_line": 4},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 2)

    def test_containing_span_with_the_same_symbol_but_a_different_kind_is_kept(self):
        # Revision 3, binding addition b: the drop trigger is symbol AND
        # kind, not symbol alone. Two entries sharing a symbol but
        # disagreeing on kind (a shape no shipped query actually produces,
        # but the engine rule must not assume that) must both survive --
        # this is the discriminating test proving `and k["kind"] ==
        # e["kind"]` actually gates the drop, not just documents it.
        entries = [
            {"key": (0, 60), "kind": "function", "symbol": "value", "start_line": 1, "end_line": 5},
            {"key": (10, 40), "kind": "method", "symbol": "value", "start_line": 2, "end_line": 4},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 2)

    def test_legitimately_nested_callable_is_never_merged(self):
        # Revision 3, binding addition b: `function outer(){ function
        # inner(){} }` -- a nested function inside another function, the
        # same shape as tests/fixtures/python_corpus/nested_in_method.py's
        # def-inside-a-method case -- must produce TWO chunks, never one.
        # Different symbols (outer vs inner) already guarantees this via
        # the symbol check alone; this test documents the shape each
        # per-language task's own nested-callable fixture (Tasks 3-8)
        # exercises end to end through the real grammar, not just here in
        # the abstract.
        entries = [
            {"key": (0, 80), "kind": "function", "symbol": "outer", "start_line": 1, "end_line": 6},
            {"key": (20, 50), "kind": "function", "symbol": "inner", "start_line": 2, "end_line": 4},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 2)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestJavaScriptExtraction(unittest.TestCase):
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "javascript_corpus"

    def _chunk(self, name):
        chunkers.treesitter.reset_cache()
        text = (self.CORPUS / name).read_text()
        return chunkers.get_chunker("javascript").chunk_file(text, name)

    def test_basic_recall_and_capture_count(self):
        result = self._chunk("basic.js")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.gaps, [])
        self.assertEqual(len(result.chunks), 7)   # capture-count golden -- verified in the scratch venv this revision
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "plain"), ("function", "arrowed"), ("function", "DefaultNamed"),
            ("constructor", "Widget.constructor"), ("accessor", "Widget.value"),
            ("accessor", "Widget.value"), ("method", "Widget.render"),
        ]))
        for c in result.chunks:
            self.assertEqual(c["lang"], "javascript")
            self.assertIn(c["kind"], chunkers.KINDS)

    def test_react_components_recall_exactly_one_chunk_per_component(self):
        # Ruling 84: verified this revision in the scratch venv, end to end
        # through the real grammar AND the dedup pipeline -- 3 components,
        # 3 chunks, no duplicate spans for the default export.
        result = self._chunk("react_components.jsx")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "Foo"), ("function", "Bar"), ("function", "default"),
        ]))
        self.assertEqual(len(result.chunks), 3)   # exactly one chunk per component

    def test_no_callable_file_yields_zero_chunks_status_ok(self):
        result = self._chunk("no_callable.js")
        self.assertEqual((result.status, result.chunks, result.gaps), ("ok", [], []))

    def test_whole_file_syntax_error(self):
        result = self._chunk("syntax_error.js")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.chunks, [])
        self.assertEqual(len(result.gaps), 1)
        self.assertEqual(result.gaps[0][2], "parse-error")

    def test_localized_error_is_partial_and_keeps_the_clean_functions(self):
        result = self._chunk("error_recovery.js")
        self.assertEqual(result.status, "partial")
        names = {c["qualified_name"] for c in result.chunks}
        self.assertEqual(names, {"good", "alsoGood"})
        self.assertTrue(any(g[2] == "parse-error" for g in result.gaps))

    def test_declared_symbols_matches_chunk_recall(self):
        chunkers.treesitter.reset_cache()
        text = (self.CORPUS / "basic.js").read_text()
        pairs = chunkers.get_chunker("javascript").declared_symbols(text)
        self.assertIn(("plain", "plain"), pairs)
        self.assertIn(("value", "Widget.value"), pairs)

    def test_nested_callables_are_kept_as_separate_chunks(self):
        # Revision 3, binding addition b -- re-verified this revision
        # against the real grammar: dedup_nested must never merge a
        # legitimately nested function into its enclosing one.
        result = self._chunk("nested_calls.js")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "outer"), ("function", "inner"),
            ("method", "Widget.method"), ("function", "Widget.helper"),
        ]))
        self.assertEqual(len(result.chunks), 4)   # nothing merged
