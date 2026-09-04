import contextlib
import hashlib
import importlib
import importlib.metadata
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
    index silently.

    Extended (whole-branch review NEW-1): a NATIVE backend module still
    fingerprints as a bare sha256 of its source, with that row's own
    impl_version the knob to bump on a real edit -- same as always. A
    SHARED-ENGINE module -- today only chunkers/treesitter.py, which
    produces all seven tree-sitter rows' chunks from one generic module --
    fingerprints as a {sha256, engine_version} PAIR instead. Pinning the
    hash alone would let someone regenerate the golden after a real edit
    without also bumping treesitter.ENGINE_VERSION; pinning the pair makes
    the new ENGINE_VERSION a value that must be typed into the golden diff,
    so the bump becomes a deliberate, reviewable act rather than an
    accidental hash regeneration. Before this, nothing here even looked at
    chunkers/treesitter.py: BACKENDS listed the two native modules only,
    and the coverage test below filtered to backend == "native" by
    construction, so the shared engine could never join the golden at all
    (self-reported by the implementer of finding 4's fix, fix report
    "Other observations")."""

    NATIVE_BACKENDS = ("chunkers/swift.py", "chunkers/python_ast.py")
    SHARED_ENGINE_BACKENDS = ("chunkers/treesitter.py",)

    @staticmethod
    def _fingerprint(rel):
        return hashlib.sha256((REPO_ROOT / rel).read_bytes()).hexdigest()

    def _golden_entry(self, rel):
        sha = self._fingerprint(rel)
        if rel in self.SHARED_ENGINE_BACKENDS:
            return {"sha256": sha, "engine_version": chunkers.treesitter.ENGINE_VERSION}
        return sha

    def test_backend_sources_match_the_recorded_fingerprints(self):
        with open(FINGERPRINT_GOLDEN_PATH) as f:
            golden = json.load(f)
        rels = self.NATIVE_BACKENDS + self.SHARED_ENGINE_BACKENDS
        got = {rel: self._golden_entry(rel) for rel in rels}
        self.assertEqual(
            got, golden,
            "chunker source changed: for chunkers/treesitter.py (the shared "
            "tree-sitter engine -- moves all seven tree-sitter rows at "
            "once) bump ENGINE_VERSION in chunkers/treesitter.py; for any "
            "other backend module bump that row's own impl_version in "
            "chunkers/__init__.py's LANGUAGE_TABLE. Either way, then "
            "regenerate the fingerprint golden (tests/goldens/"
            "chunker_source_fingerprints.json) -- if the change provably "
            "alters no chunk this backend emits (a comment, a docstring), "
            "regenerate the golden alone and say so in the commit message.",
        )

    def test_the_golden_covers_every_distinct_backend_module_in_the_table(self):
        """A new LANGUAGE_TABLE row -- native, or sharing chunkers/treesitter.py
        (or a future engine module of its own), or a wholly new backend
        module -- must not slip past the tripwire just by not being listed
        here."""
        with open(FINGERPRINT_GOLDEN_PATH) as f:
            golden = json.load(f)
        expected = {
            row["module"].replace(".", "/") + ".py"
            for row in chunkers.LANGUAGE_TABLE.values()
        }
        self.assertEqual(set(golden), expected)
        self.assertEqual(
            set(self.NATIVE_BACKENDS) | set(self.SHARED_ENGINE_BACKENDS), expected,
            "a new backend module appeared in LANGUAGE_TABLE -- add it to "
            "NATIVE_BACKENDS or SHARED_ENGINE_BACKENDS above, whichever it "
            "is, and add its entry to the fingerprint golden.",
        )


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
        # The dispatch itself lives in fragment_declaration_status;
        # fragment_declared_in_text is the verdict-only wrapper over it
        # (whole-branch review, finding 1 -- a missing optional grammar
        # wheel needs a third state the bare predicate cannot carry).
        src = inspect.getsource(memidx.fragment_declaration_status)
        body = src.split('"""')[-1]
        self.assertNotIn('== "python"', body, body)
        self.assertIn("get_chunker", body, body)
        wrapper = inspect.getsource(memidx.fragment_declared_in_text)
        self.assertIn("fragment_declaration_status", wrapper.split('"""')[-1], wrapper)


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
class TestSharedEngineImportFailureDegrades(unittest.TestCase):
    """External gate finding 6. A missing grammar WHEEL was already
    fail-open; the shared engine module itself was not. chunkers.treesitter
    is a module like any other -- a half-applied edit or a packaging fault
    can make it unimportable -- and every tree-sitter row's registry lookup
    goes through it, so the failure escaped as a bare ModuleNotFoundError
    out of get_chunker, backend_availability and chunker_version, the three
    doors code-search, why and every reindex walk through.

    `sys.modules['chunkers.treesitter'] = None` is the reviewers' own probe
    for it: python raises ModuleNotFoundError on the next import of a name
    bound to None. All three doors must answer "this backend cannot run
    here" instead."""

    @contextlib.contextmanager
    def _engine_unimportable(self):
        engine = chunkers.treesitter
        engine.reset_cache()
        # BOTH halves have to go. An engine that genuinely fails to import
        # leaves no sys.modules entry AND no attribute on the package; a
        # process that already imported it holds both, and `from . import
        # treesitter` reads the attribute without consulting sys.modules at
        # all -- so patching sys.modules alone would be a probe of nothing.
        del chunkers.treesitter
        try:
            with mock.patch.dict(sys.modules, {"chunkers.treesitter": None}):
                yield
        finally:
            chunkers.treesitter = engine
            sys.modules["chunkers.treesitter"] = engine
            engine.reset_cache()

    def test_get_chunker_reports_backend_unavailable(self):
        with self._engine_unimportable():
            with self.assertRaises(chunkers.BackendUnavailable) as ctx:
                chunkers.get_chunker("javascript")
        self.assertIn("chunkers.treesitter", str(ctx.exception))

    def test_backend_availability_reports_the_rows_missing_not_raises(self):
        with self._engine_unimportable():
            avail = chunkers.backend_availability()
        for lang in ("javascript", "typescript", "tsx", "java", "php", "rust", "lua"):
            self.assertIn(f"{lang}=missing", avail, avail)
        self.assertIn("python=ok", avail, avail)   # native rows are a different engine

    def test_chunker_version_still_answers(self):
        with self._engine_unimportable():
            cv = chunkers.chunker_version("javascript")
        self.assertRegex(cv, r"^[0-9a-f]{12}$")
        # A sentinel payload, so it cannot collide with the fingerprint a
        # working engine computes -- the files stay marked stale and are
        # re-examined once the engine imports again.
        self.assertNotEqual(cv, chunkers.chunker_version("javascript"))

    def test_code_search_survives_it(self):
        """The first of the two memidx entry points the gate names: a
        code-search over an index whose language rows all belong to the
        broken engine must report, not crash. chunker_version is reached
        per file from code_index_report with only an `except KeyError`
        around it, which is where this used to die before get_chunker was
        ever consulted."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "a.js").write_text("function findable_symbol(){ return 1; }\n")
            db_path = Path(td) / "code.sqlite"
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = code_reindex(root, db_path, project="engine-gone",
                                  no_embed=True, lang="javascript")
            self.assertEqual(rc, 0, out.getvalue())
            with self._engine_unimportable():
                buf, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                    rc = memidx.cmd_code_search(ns(
                        db=str(db_path), project="engine-gone", query="findable_symbol",
                        mode="fts", limit=5, json=False, decision_db=None,
                        no_heal=False, heal_limit=500,
                    ))
            self.assertEqual(rc, 0, buf.getvalue() + err.getvalue())

    def test_why_s_symbol_vocabulary_check_survives_it(self):
        """The second entry point: `why`'s disk-scan fallback resolves a
        symbol through fragment_declaration_status, which must answer
        "cannot tell" rather than raise."""
        with self._engine_unimportable():
            verdict, reason, remedy = memidx.fragment_declaration_status(
                "findable_symbol", "function findable_symbol(){}\n", rel_path="a.js"
            )
            self.assertIsNone(verdict)
            self.assertIn("chunkers.treesitter", reason)
            self.assertIn("backend-preflight", remedy)
            self.assertIsNone(memidx.fragment_declared_in_text(
                "findable_symbol", "function findable_symbol(){}\n", rel_path="a.js"
            ))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestDeclaredSymbolsSeparatesFailureFromAbsence(unittest.TestCase):
    """External gate finding 7. `declared_symbols` returned an empty list
    for a file it could not chunk at all, and an empty list is a positive
    claim: this file parsed and declares nothing. memlint reads it that way
    and errors on any `#symbol` fragment pointing into the file -- so a file
    over the per-file byte cap turned every valid record naming a symbol in
    it into a lint error.

    A file the backend cannot read is UNCHECKABLE: the tri-state's None,
    which memlint reports as a warning naming the reason, exactly as it
    already does for a language whose backend cannot run here at all."""

    OVER_CAP = "function valid_symbol(){ return 1; }\n" + ("// filler\n" * 200)

    def test_over_cap_file_raises_instead_of_answering_empty(self):
        chunkers.treesitter.reset_cache()
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_MAX_PARSE_BYTES": "64"}):
            backend = chunkers.get_chunker("javascript")
            with self.assertRaises(chunkers.ChunkingFailed):
                backend.declared_symbols(self.OVER_CAP)
        chunkers.treesitter.reset_cache()

    def test_over_cap_file_is_uncheckable_not_proof_of_absence(self):
        chunkers.treesitter.reset_cache()
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_MAX_PARSE_BYTES": "64"}):
            verdict, reason, remedy = memidx.fragment_declaration_status(
                "valid_symbol", self.OVER_CAP, rel_path="big.js"
            )
        chunkers.treesitter.reset_cache()
        self.assertIsNone(verdict)
        self.assertIn("TreeSitterFileTooLarge", reason)
        # The backend RUNS here -- backend-preflight would report this
        # language ok -- so the remedy is the cap, not that command.
        self.assertIn("MEMCONTINUUM_MAX_PARSE_BYTES", remedy)
        self.assertNotIn("backend-preflight", remedy)

    def test_a_file_that_did_not_parse_is_uncheckable_too(self):
        chunkers.treesitter.reset_cache()
        verdict, reason, remedy = memidx.fragment_declaration_status(
            "valid_symbol", "@@@ not javascript at all @@@\n", rel_path="broken.js"
        )
        chunkers.treesitter.reset_cache()
        self.assertIsNone(verdict)
        self.assertIn("ChunkingFailed", reason)
        self.assertIn("syntax", remedy)
        self.assertNotIn("backend-preflight", remedy)

    def test_a_readable_file_still_proves_a_symbol_absent(self):
        chunkers.treesitter.reset_cache()
        verdict, reason, remedy = memidx.fragment_declaration_status(
            "no_such_symbol", "function other(){}\n", rel_path="a.js"
        )
        self.assertFalse(verdict)
        self.assertEqual((reason, remedy), ("", ""))
        self.assertIsNotNone(verdict, "an absent symbol stays a hard error, not a warning")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTreeSitterFingerprint(unittest.TestCase):
    def test_chunker_version_never_imports_tree_sitter(self):
        with mock.patch.dict(sys.modules, {"tree_sitter": None, "tree_sitter_javascript": None}):
            v = chunkers.chunker_version("javascript")
        self.assertEqual(len(v), 12)

    def test_chunker_version_changes_when_query_file_changes(self):
        """External gate finding 11: the probe edits a COPY in a temp
        directory with QUERY_DIR pointed at it, never the tracked query
        file. Editing the real one fails outright in a read-only checkout,
        and leaves the worktree dirty if the process dies between the write
        and the restore -- a test must not be able to modify the tree it is
        testing."""
        query_file = chunkers.LANGUAGE_TABLE["javascript"]["query_file"]
        source = Path(chunkers.treesitter.QUERY_DIR) / query_file
        with tempfile.TemporaryDirectory() as td:
            copy = Path(td) / query_file
            copy.write_bytes(source.read_bytes())
            with mock.patch.object(chunkers.treesitter, "QUERY_DIR", td):
                before = chunkers.chunker_version("javascript")
                copy.write_bytes(copy.read_bytes() + b"\n; probe\n")
                after = chunkers.chunker_version("javascript")
        self.assertNotEqual(before, after)
        self.assertEqual(source.read_bytes(),
                         (Path(chunkers.treesitter.QUERY_DIR) / query_file).read_bytes())

    def test_an_unreadable_query_file_never_escapes_the_per_file_guard(self):
        """Whole-branch review, finding 14. chunker_version is reached from
        heal_code_index with only a `except KeyError` around it, so an
        OSError out of query_fingerprint would take down a whole walk over
        one row's packaging fault. The fingerprint degrades to a sentinel
        instead; the fault surfaces one step later, inside the guard, as a
        BackendUnavailable naming the file, and the source file lands in
        the retryable not-indexed bucket."""
        chunkers.treesitter.reset_cache()
        row = chunkers.LANGUAGE_TABLE["javascript"]
        original = row["query_file"]
        row["query_file"] = "no-such-query-file.scm"
        try:
            cv = chunkers.chunker_version("javascript")
            self.assertRegex(cv, r"^[0-9a-f]{12}$")
            self.assertEqual(
                chunkers.treesitter.query_fingerprint(row),
                chunkers.treesitter.QUERY_UNREADABLE,
            )
            with self.assertRaises(chunkers.BackendUnavailable) as ctx:
                chunkers.get_chunker("javascript")
            self.assertIn("no-such-query-file.scm", str(ctx.exception))

            with tempfile.TemporaryDirectory() as td:
                root = Path(td) / "code"
                root.mkdir()
                (root / "a.js").write_text("function f(){}\n")
                db_path = Path(td) / "code.sqlite"
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = code_reindex(root, db_path, project="unreadable-query",
                                      no_embed=True, lang="javascript")
                self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
                self.assertIn("1 not indexed", out.getvalue())
                conn = memidx.open_code_db(db_path)
                sha_row = conn.execute(
                    "SELECT status, reason FROM file_sha WHERE path=?", ("a.js",)
                ).fetchone()
                conn.close()
                self.assertEqual(sha_row["status"], "not-indexed")
                self.assertIn("no-such-query-file.scm", sha_row["reason"])
        finally:
            row["query_file"] = original
            chunkers.treesitter.reset_cache()

    def test_chunker_version_is_stable_across_two_calls_on_an_unchanged_row(self):
        for lang in ("javascript", "typescript", "tsx", "java", "php", "rust", "lua"):
            self.assertEqual(
                chunkers.chunker_version(lang), chunkers.chunker_version(lang),
                f"{lang}'s fingerprint must not depend on anything but the row and the query file",
            )

    def test_chunker_version_changes_when_the_shared_engine_version_changes(self):
        # Whole-branch review, finding 4: one generic module produces every
        # tree-sitter row's chunks, so its own version is the knob that
        # invalidates all of them at once.
        before = {lang: chunkers.chunker_version(lang)
                  for lang in ("javascript", "typescript", "tsx", "java", "php", "rust", "lua")}
        with mock.patch.object(chunkers.treesitter, "ENGINE_VERSION", "probe"):
            after = {lang: chunkers.chunker_version(lang) for lang in before}
        for lang in before:
            self.assertNotEqual(before[lang], after[lang], lang)
        # Native rows are a different engine and must not move with it.
        with mock.patch.object(chunkers.treesitter, "ENGINE_VERSION", "probe"):
            self.assertEqual(chunkers.chunker_version("swift"),
                             chunkers.chunker_version("swift"))

    def test_typescript_and_tsx_have_different_chunker_versions(self):
        # Whole-branch review NEW-3: typescript and tsx share a
        # grammar_module, grammar_pin, runtime_pin and query_file --
        # everything chunker_version's payload used to fold in EXCEPT
        # language_fn ("language_typescript" vs "language_tsx"), the one
        # field that decides which function of the grammar module actually
        # parses the file. Before row_shape carried it, the two rows
        # fingerprinted IDENTICALLY despite parsing the same source
        # differently. Each is checked stable across two calls too, same
        # as every row (see test_chunker_version_is_stable_across_two_calls
        # _on_an_unchanged_row above), named here explicitly since this
        # pair is what the finding is about.
        ts = chunkers.chunker_version("typescript")
        tsx = chunkers.chunker_version("tsx")
        self.assertNotEqual(ts, tsx)
        self.assertEqual(ts, chunkers.chunker_version("typescript"))
        self.assertEqual(tsx, chunkers.chunker_version("tsx"))

    def test_chunker_version_changes_with_each_row_shaping_field(self):
        # containers drives qualified_name, method_if_ancestor_in drives
        # kind, doc_comment_types drives doc, max_bytes decides whether a
        # file is chunked at all, language_fn selects which grammar
        # function actually parses the file (NEW-3) -- each one changes
        # what a chunk looks like, so each must change the fingerprint.
        row = chunkers.LANGUAGE_TABLE["javascript"]
        before = chunkers.chunker_version("javascript")
        probes = {
            "containers": {"class_declaration": "name", "probe_declaration": "name"},
            # prefix_scopes drives qualified_name for a declaration that
            # scopes what FOLLOWS it (PHP's `namespace A;`) rather than what
            # it holds; the javascript row declares none, so gaining one is
            # the edit under test.
            "prefix_scopes": {"probe_definition": "name"},
            "method_if_ancestor_in": frozenset({"probe_item"}),
            "doc_comment_types": ("comment", "probe_comment"),
            "max_bytes": 4096,
            "language_fn": "probe_language_fn",
        }
        for field, value in probes.items():
            original = row.get(field, "<<absent>>")
            row[field] = value
            try:
                self.assertNotEqual(
                    chunkers.chunker_version("javascript"), before,
                    f"editing {field} must change javascript's chunker_version",
                )
            finally:
                if original == "<<absent>>":
                    row.pop(field, None)
                else:
                    row[field] = original
        self.assertEqual(chunkers.chunker_version("javascript"), before)

    def test_chunker_version_changes_when_the_installed_grammar_version_changes(self):
        """Ruling 108, external gate finding 1 (BLOCKING) / finding 3 of the
        second gate. The pins alone cannot describe what a machine actually
        parses with: a wheel one version off the pin emits different nodes
        for the same source, and a fingerprint that ignored it would leave
        every already-indexed file of that language marked current across
        the swap. Both distributions count -- the row's grammar wheel and
        the shared tree-sitter runtime, which walks the tree the grammar
        builds -- so each is probed on its own."""
        real = importlib.metadata.version

        def patched(dist, target):
            def fake(name):
                return "9.9.9" if name == target else real(name)
            return fake

        chunkers.treesitter.reset_cache()
        before = chunkers.chunker_version("javascript")
        for target in ("tree-sitter-javascript", "tree-sitter"):
            with mock.patch.object(importlib.metadata, "version", patched(target, target)):
                chunkers.treesitter.reset_cache()
                self.assertNotEqual(
                    chunkers.chunker_version("javascript"), before,
                    f"an installed {target} at a version the row does not pin "
                    "must change the fingerprint",
                )
            chunkers.treesitter.reset_cache()
        self.assertEqual(chunkers.chunker_version("javascript"), before)

    def test_chunker_version_still_answers_when_the_grammar_is_not_installed(self):
        """Fail-open half of ruling 108: a not-indexed row still stores its
        chunker_version (M2a binding point 1), and a missing wheel is
        exactly the case that produces one -- so the absent version
        contributes a fixed token rather than an exception, and
        BackendUnavailable out of get_chunker stays the only report of the
        absence itself."""
        real = importlib.metadata.version

        def absent(name):
            if name == "tree-sitter-javascript":
                raise importlib.metadata.PackageNotFoundError(name)
            return real(name)

        chunkers.treesitter.reset_cache()
        with mock.patch.object(importlib.metadata, "version", absent):
            cv = chunkers.chunker_version("javascript")
            self.assertRegex(cv, r"^[0-9a-f]{12}$")
            self.assertEqual(
                chunkers.treesitter.installed_version("tree-sitter-javascript"),
                chunkers.treesitter.VERSION_ABSENT,
            )
        chunkers.treesitter.reset_cache()

    def test_chunker_version_changes_when_the_effective_byte_cap_changes(self):
        """External gate finding 8: MEMCONTINUUM_MAX_PARSE_BYTES decides
        which files are chunked at all, so a cap change must re-examine
        them. Raising it has to re-attempt the over-cap files an earlier run
        skipped; lowering it has to re-examine the ones it accepted."""
        chunkers.treesitter.reset_cache()
        before = chunkers.chunker_version("javascript")
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_MAX_PARSE_BYTES": "4096"}):
            self.assertNotEqual(chunkers.chunker_version("javascript"), before)
        self.assertEqual(chunkers.chunker_version("javascript"), before)

    def test_pin_mismatch_is_none_when_the_install_matches_the_pins(self):
        chunkers.treesitter.reset_cache()
        for lang in ("javascript", "typescript", "tsx", "java", "php", "rust", "lua"):
            self.assertIsNone(chunkers.pin_mismatch(lang), lang)
        self.assertIsNone(chunkers.pin_mismatch("python"))   # native rows pin nothing

    def test_pin_mismatch_names_the_distribution_and_both_versions(self):
        real = importlib.metadata.version

        def fake(name):
            return "9.9.9" if name == "tree-sitter-lua" else real(name)

        chunkers.treesitter.reset_cache()
        with mock.patch.object(importlib.metadata, "version", fake):
            mismatch = chunkers.pin_mismatch("lua")
        chunkers.treesitter.reset_cache()
        self.assertIsNotNone(mismatch)
        self.assertIn("tree-sitter-lua", mismatch)
        self.assertIn("9.9.9", mismatch)
        self.assertIn(chunkers.LANGUAGE_TABLE["lua"]["grammar_pin"], mismatch)

    def test_row_shape_ignores_the_order_a_row_is_written_in(self):
        # Sorted, so reordering a row's own containers is not a rechunk.
        row = dict(chunkers.LANGUAGE_TABLE["typescript"])
        row["prefix_scopes"] = {"alpha_definition": "name", "beta_definition": "name"}
        shuffled = dict(row)
        shuffled["containers"] = {"module": "name", "internal_module": "name",
                                  "class_declaration": "name",
                                  "abstract_class_declaration": "name"}
        shuffled["prefix_scopes"] = {"beta_definition": "name", "alpha_definition": "name"}
        self.assertEqual(
            chunkers.treesitter.row_shape(row), chunkers.treesitter.row_shape(shuffled)
        )

    def test_chunker_version_of_a_native_row_moves_with_the_interpreter(self):
        """Ruling 112. chunkers.python_ast chunks through `ast.parse`, whose
        accepted syntax and node shapes move with CPython, and the reindex
        skip is `prev_sha == sha and prev_cv == cv` -- so an interpreter
        upgrade with no payload change would leave every already-indexed .py
        file as-is under a parser that can read it differently. This is the
        gap ruling 108 closed for the tree-sitter tier by hashing the
        installed runtime, carried to the native tier that needs it.

        Swift does not: it is a hand-written lexer over `re` and string
        operations, with no interpreter-provided parser behind it, so its own
        source fingerprint and impl_version already cover it."""
        before_py = chunkers.chunker_version("python")
        before_swift = chunkers.chunker_version("swift")
        # A plain tuple: sys.version_info itself cannot be instantiated,
        # and chunker_version reads it by index.
        faked = (3, 99, 0, "final", 0)
        with mock.patch.object(sys, "version_info", faked):
            self.assertNotEqual(
                chunkers.chunker_version("python"), before_py,
                "a python row must re-fingerprint on an interpreter minor-version change",
            )
            self.assertEqual(
                chunkers.chunker_version("swift"), before_swift,
                "swift's lexer does not move with the interpreter",
            )
        self.assertEqual(chunkers.chunker_version("python"), before_py)

    def test_chunker_version_of_a_native_row_ignores_the_patch_level(self):
        """major.minor only: a patch release does not move the grammar, and
        re-chunking every project on a 3.12.7 -> 3.12.8 bump is exactly the
        cost the version stamp exists to avoid."""
        before = chunkers.chunker_version("python")
        bumped = (sys.version_info[0], sys.version_info[1],
                  sys.version_info[2] + 1, "final", 0)
        with mock.patch.object(sys, "version_info", bumped):
            self.assertEqual(chunkers.chunker_version("python"), before)

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


class TestQueryFilesInSync(unittest.TestCase):
    """typescript.scm says it is javascript.scm's body with a header of its
    own -- TS and TSX add types and JSX, neither of which changes how a
    function, method or class is shaped. That is a claim a person has to
    honor by hand on every query edit, so it is checked here instead: the
    two bodies (each file past its own header comment block) must match
    exactly."""

    @staticmethod
    def _body(name):
        text = (REPO_ROOT / "chunkers" / "queries" / name).read_text()
        _header, _blank, body = text.partition("\n\n")
        return body

    def test_javascript_and_typescript_query_bodies_are_identical(self):
        js = self._body("javascript.scm")
        self.assertTrue(js.strip(), "the body split found nothing -- check the header format")
        self.assertEqual(js, self._body("typescript.scm"))


class TestTreeSitterIntervalOverlap(unittest.TestCase):
    """External gate finding 9 / second gate finding 7, at the unit the
    finding is about: which error intervals a capture is judged to share
    bytes with. Node spans and ERROR intervals are both half-open, so a
    boundary touch is not an overlap -- except for a zero-width MISSING
    token, which can only ever touch a boundary and must still count."""

    class _Node:
        def __init__(self, start, end):
            self.start_byte = start
            self.end_byte = end

    def _overlaps(self, node_span, intervals):
        return chunkers.treesitter._overlapping_intervals(self._Node(*node_span), intervals)

    def test_an_error_starting_at_the_exclusive_end_is_not_an_overlap(self):
        self.assertEqual(self._overlaps((0, 26), [(26, 29)]), [])

    def test_an_error_ending_at_the_start_is_not_an_overlap(self):
        self.assertEqual(self._overlaps((26, 40), [(20, 26)]), [])

    def test_a_shared_byte_is_an_overlap(self):
        self.assertEqual(self._overlaps((0, 27), [(26, 29)]), [(26, 29)])
        self.assertEqual(self._overlaps((0, 40), [(10, 12)]), [(10, 12)])

    def test_a_zero_width_missing_token_counts_at_either_boundary(self):
        self.assertEqual(self._overlaps((0, 26), [(26, 26)]), [(26, 26)])
        self.assertEqual(self._overlaps((0, 26), [(0, 0)]), [(0, 0)])
        self.assertEqual(self._overlaps((0, 26), [(13, 13)]), [(13, 13)])
        self.assertEqual(self._overlaps((0, 26), [(27, 27)]), [])


class TestTreeSitterDedupPriority(unittest.TestCase):
    def test_same_span_two_kinds_keeps_the_more_specific_one(self):
        entries = [
            {"key": (10, 40), "kind": "method", "symbol": "value", "start_line": 2, "end_line": 2},
            {"key": (10, 40), "kind": "accessor", "symbol": "value", "start_line": 2, "end_line": 2},
        ]
        deduped = chunkers.treesitter.dedup_by_priority(entries)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["kind"], "accessor")

    def test_same_span_and_kind_prefers_the_explicitly_qualified_entry(self):
        # External gate finding 3: an object literal's method matches both
        # the generic method_definition pattern (which can only name it
        # `open`) and the bound-object pattern (which names it `api.open`),
        # at the same span and the same kind. The qualified reading wins by
        # rule, not by whichever match the grammar happened to complete
        # first.
        entries = [
            {"key": (10, 40), "kind": "method", "symbol": "open", "qualified_name": "open"},
            {"key": (10, 40), "kind": "method", "symbol": "open", "qualified_name": "api.open",
             "has_qualifier": True},
        ]
        deduped = chunkers.treesitter.dedup_by_priority(entries)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["qualified_name"], "api.open")
        self.assertEqual(
            chunkers.treesitter.dedup_by_priority(list(reversed(entries)))[0]["qualified_name"],
            "api.open", "the winner must not depend on match order",
        )

    def test_a_more_specific_kind_still_beats_an_explicit_qualifier(self):
        entries = [
            {"key": (10, 40), "kind": "method", "symbol": "x", "qualified_name": "api.x",
             "has_qualifier": True},
            {"key": (10, 40), "kind": "accessor", "symbol": "x", "qualified_name": "x"},
        ]
        deduped = chunkers.treesitter.dedup_by_priority(entries)
        self.assertEqual([e["kind"] for e in deduped], ["accessor"])

    def test_wrapper_span_over_the_same_qualified_name_is_dropped(self):
        # Ruling 84: an outer wrapper match (export_statement) and an
        # inner definition match (function_declaration) can both name
        # "Named" with DIFFERENT spans, the outer containing the inner --
        # keep only the innermost. Two different NODE TYPES is what makes
        # this a wrapper rather than a nesting.
        entries = [
            {"key": (0, 50), "kind": "function", "symbol": "Named", "qualified_name": "Named",
             "node_type": "export_statement", "start_line": 1, "end_line": 3},
            {"key": (7, 45), "kind": "function", "symbol": "Named", "qualified_name": "Named",
             "node_type": "function_declaration", "start_line": 1, "end_line": 3},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["key"], (7, 45))

    def test_containing_span_with_a_different_qualified_name_is_kept(self):
        # A method inside a class is CONTAINED by the class's own span, but
        # they name different qualified names -- containment alone must
        # never merge unrelated captures.
        entries = [
            {"key": (0, 100), "kind": "function", "symbol": "Outer", "qualified_name": "Outer",
             "node_type": "function_declaration", "start_line": 1, "end_line": 10},
            {"key": (10, 40), "kind": "method", "symbol": "inner", "qualified_name": "Outer.inner",
             "node_type": "method_definition", "start_line": 2, "end_line": 4},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 2)

    def test_containing_span_with_the_same_name_but_a_different_kind_is_kept(self):
        # Revision 3, binding addition b: the drop trigger includes kind,
        # not the name alone. Two entries sharing a qualified name but
        # disagreeing on kind (a shape no shipped query actually produces,
        # but the engine rule must not assume that) must both survive --
        # this is the discriminating test proving `and k["kind"] ==
        # e["kind"]` actually gates the drop, not just documents it.
        entries = [
            {"key": (0, 60), "kind": "function", "symbol": "value", "qualified_name": "value",
             "node_type": "export_statement", "start_line": 1, "end_line": 5},
            {"key": (10, 40), "kind": "method", "symbol": "value", "qualified_name": "value",
             "node_type": "method_definition", "start_line": 2, "end_line": 4},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 2)

    def test_a_containing_definition_is_never_a_wrapper(self):
        # Whole-branch review, finding 2: the discriminating test for the
        # WRAPPER_NODE_TYPES membership check. Two entries agreeing on
        # qualified_name AND kind, one containing the other, whose OUTER
        # node type is a definition rather than a declared wrapper, are a
        # callable nested inside a same-named callable. Both survive --
        # `function f(){ function f(){} }` (same node type) and `function
        # f(){ const f = () => {}; }` (different node types) are the same
        # case as far as this rule is concerned.
        for outer_type in ("function_declaration", "method_definition"):
            entries = [
                {"key": (0, 80), "kind": "function", "symbol": "f", "qualified_name": "f",
                 "node_type": outer_type, "start_line": 1, "end_line": 6},
                {"key": (20, 50), "kind": "function", "symbol": "f", "qualified_name": "f",
                 "node_type": "arrow_function", "start_line": 2, "end_line": 4},
            ]
            deduped = chunkers.treesitter.dedup_nested(entries)
            self.assertEqual(len(deduped), 2, outer_type)

    def test_only_a_declared_wrapper_node_type_can_be_dropped(self):
        self.assertIn("export_statement", chunkers.treesitter.WRAPPER_NODE_TYPES)
        self.assertNotIn("function_declaration", chunkers.treesitter.WRAPPER_NODE_TYPES)
        self.assertNotIn("arrow_function", chunkers.treesitter.WRAPPER_NODE_TYPES)

    def test_legitimately_nested_callable_is_never_merged(self):
        # `function outer(){ function inner(){} }` -- a nested function
        # inside another function, the same shape as
        # tests/fixtures/python_corpus/nested_in_method.py's
        # def-inside-a-method case -- must produce TWO chunks, never one.
        # Different names already guarantee this on their own; this test
        # documents the shape each per-language task's own nested-callable
        # fixture (Tasks 3-8) exercises end to end through the real
        # grammar, not just here in the abstract.
        entries = [
            {"key": (0, 80), "kind": "function", "symbol": "outer", "qualified_name": "outer",
             "node_type": "function_declaration", "start_line": 1, "end_line": 6},
            {"key": (20, 50), "kind": "function", "symbol": "inner", "qualified_name": "inner",
             "node_type": "function_declaration", "start_line": 2, "end_line": 4},
        ]
        deduped = chunkers.treesitter.dedup_nested(entries)
        self.assertEqual(len(deduped), 2)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestSameNamedNestingSurvivesTheRealGrammar(unittest.TestCase):
    """Whole-branch review, finding 2, against the real grammars rather
    than abstract tuples. Before the fix each of these produced ONE chunk:
    the outer level -- the one a caller imports -- was dropped and the
    survivor carried the inner span's line numbers, with status `ok`, no
    gap and no warning."""

    def _chunks(self, lang, rel, src):
        chunkers.treesitter.reset_cache()
        result = chunkers.get_chunker(lang).chunk_file(src, rel)
        self.assertEqual(result.status, "ok", result.gaps)
        return result.chunks

    def test_javascript_function_nested_in_a_same_named_function(self):
        chunks = self._chunks("javascript", "a.js", "function f(){\n  function f(){}\n}\n")
        # A function body is not a qualification container in javascript's
        # `containers` map, so both levels qualify to plain "f" -- the
        # node-type clause, not the name, is what keeps them apart. Line
        # numbers are what distinguish them to a reader.
        self.assertEqual(
            [(c["kind"], c["qualified_name"], c["start_line"]) for c in chunks],
            [("function", "f", 1), ("function", "f", 2)],
        )

    def test_rust_fn_nested_in_a_same_named_fn(self):
        chunks = self._chunks("rust", "a.rs", "fn f(){\n  fn f(){}\n}\n")
        self.assertEqual(
            [(c["kind"], c["qualified_name"], c["start_line"]) for c in chunks],
            [("function", "f", 1), ("function", "f", 2)],
        )

    def test_javascript_method_in_a_same_named_class_inside_that_method(self):
        chunks = self._chunks(
            "javascript", "a.js",
            "class A {\n  m(){\n    class A {\n      m(){}\n    }\n  }\n}\n",
        )
        # Two class_declaration containers nest, so the inner method
        # qualifies as A.A.m and the outer as A.m -- different names,
        # and both are their own chunk.
        self.assertEqual(
            [(c["kind"], c["qualified_name"], c["start_line"]) for c in chunks],
            [("method", "A.m", 2), ("method", "A.A.m", 4)],
        )

    def test_javascript_arrow_bound_to_the_enclosing_functions_own_name(self):
        # The shape a difference-based rule gets wrong: the two node types
        # genuinely DIFFER (function_declaration containing arrow_function)
        # while the names and the kind all match, so only a membership test
        # against WRAPPER_NODE_TYPES keeps the outer level. This is the
        # ordinary `function handler(){ const handler = async () => ...; }`
        # JS idiom, not a contrived one.
        chunks = self._chunks("javascript", "a.js", "function f(){\n  const f = () => {};\n}\n")
        self.assertEqual(
            [(c["kind"], c["qualified_name"], c["start_line"]) for c in chunks],
            [("function", "f", 1), ("function", "f", 2)],
        )

    def test_typescript_function_expression_bound_to_the_same_name(self):
        chunks = self._chunks("typescript", "a.ts",
                              "function f(){\n  const f = function(){};\n}\n")
        self.assertEqual(
            [(c["kind"], c["qualified_name"], c["start_line"]) for c in chunks],
            [("function", "f", 1), ("function", "f", 2)],
        )

    def test_export_default_still_yields_one_chunk(self):
        # Ruling 84's own case: the shipped queries put both patterns on
        # the SAME function_declaration span, so dedup_by_priority resolves
        # it and dedup_nested never sees a containment at all. The fix must
        # not change this.
        chunks = self._chunks("javascript", "a.js", "export default function Named(){}\n")
        self.assertEqual(
            [(c["kind"], c["qualified_name"]) for c in chunks],
            [("function", "Named")],
        )


def _tree_sitter_chunk(lang, corpus, name):
    """Shared helper for every Test*Extraction class below (Task 7 fix
    round 1, finding 9): reset the tree-sitter instance cache, read `name`
    from `corpus`, and run it through `lang`'s registered chunker. Hoisted
    from six identical per-class `_chunk` bodies (JS, TS, TSX, Java, PHP,
    Rust) -- each class still keeps a thin `_chunk(self, name)` wrapper
    (its own `self.CORPUS` differs), but none of them duplicate this
    reset/read/chunk logic any more."""
    chunkers.treesitter.reset_cache()
    text = (corpus / name).read_text()
    return chunkers.get_chunker(lang).chunk_file(text, name)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestJavaScriptExtraction(unittest.TestCase):
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "javascript_corpus"

    def _chunk(self, name):
        return _tree_sitter_chunk("javascript", self.CORPUS, name)

    def test_basic_recall_and_capture_count(self):
        result = self._chunk("basic.js")
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.gaps, [])
        # 8, not 7 (fix round 1): `boxed`, a bound function_expression (not
        # arrow), was added to exercise javascript.scm:7's untested
        # function_expression alternation branch (review finding 2).
        self.assertEqual(len(result.chunks), 8)   # capture-count golden -- verified in the scratch venv this revision
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "plain"), ("function", "arrowed"), ("function", "boxed"),
            ("function", "DefaultNamed"),
            ("constructor", "Widget.constructor"), ("accessor", "Widget.value"),
            ("accessor", "Widget.value"), ("method", "Widget.render"),
        ]))
        for c in result.chunks:
            self.assertEqual(c["lang"], "javascript")
            self.assertIn(c["kind"], chunkers.KINDS)

    def test_bound_function_expression_is_a_function_chunk(self):
        # Review finding 2 (LOW): javascript.scm:7's `[(arrow_function)
        # (function_expression)]` alternation was only ever exercised by
        # the arrow branch (`arrowed`) -- `const boxed = function (x) {...}`
        # is the function_expression branch, same bound-variable-declarator
        # pattern, symbol/qn taken from the declarator's own name exactly
        # like the arrow case.
        result = self._chunk("basic.js")
        boxed = [c for c in result.chunks if c["qualified_name"] == "boxed"]
        self.assertEqual(len(boxed), 1)
        self.assertEqual((boxed[0]["kind"], boxed[0]["symbol"]), ("function", "boxed"))

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

    def test_anonymous_default_export_arrow_yields_one_function_default_chunk(self):
        # Review finding 1 (MEDIUM): javascript.scm's plain (unwrapped by
        # memo/forwardRef) anonymous `export default (arrow_function|
        # function_expression)` pattern fired on no fixture in the original
        # delivery. B2: "MUST emit a named chunk" -- symbol/qn "default";
        # span is the arrow_function node's own span (`@chunk.function`,
        # not the `export_statement` `@chunk.default` wraps -- build_chunks
        # reads the span from whichever capture supplies the kind, and
        # "chunk.default" is excluded from that role by name), i.e. lines
        # 1-3 here, not the file's only line the `export default` keyword
        # itself sits on plus anything after the trailing `;`.
        result = self._chunk("anonymous_default_arrow.js")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "default", "default"))
        self.assertEqual((c["start_line"], c["end_line"]), (1, 3))

    def test_anonymous_default_export_function_expression_yields_one_function_default_chunk(self):
        # Review finding 1 (MEDIUM), function_expression half of the same
        # alternation: `export default function () {...}` (no name --
        # `export default function Named(){}` is a DIFFERENT grammar shape,
        # a function_declaration, already covered by basic.js).
        result = self._chunk("anonymous_default_function.js")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "default", "default"))
        self.assertEqual((c["start_line"], c["end_line"]), (1, 3))

    def test_default_export_forwardref_function_expression_span_is_the_inner_callable(self):
        # Review finding 2 (LOW): the memo/forwardRef-wrapped DEFAULT-EXPORT
        # pattern (javascript.scm:16-21) was only exercised by its
        # arrow_function branch (react_components.jsx's `export default
        # memo(() => {})`) -- this fixture is the function_expression
        # branch, `export default forwardRef(function (props, ref) {...})`.
        # Symbol/qn "default" (no @chunk.name on this pattern); span is the
        # inner function_expression's own lines (3-5), never the wrapping
        # `forwardRef(...)` call or the `export default` keyword's line (1
        # is the import, so a wrapper-span bug would show up as (1, 5) or
        # (3, 5) starting one line early -- this pins the exact span).
        result = self._chunk("default_export_forwardref.js")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "default", "default"))
        self.assertEqual((c["start_line"], c["end_line"]), (3, 5))

    def test_generators_are_function_chunks_under_their_own_names(self):
        """External gate finding 2, second gate finding 2. A generator is
        its own node type -- generator_function_declaration for `function*
        g(){}` and `async function* g(){}`, generator_function for a bound
        `function*` expression -- so none of the patterns keyed to
        function_declaration/function_expression saw one, and the file
        reported status=ok with the generator silently absent. Generator
        METHODS were kept the whole time (method_definition covers them),
        which is what made the gap so hard to see."""
        result = self._chunk("generators.js")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("function", "asyncCounter", "asyncCounter"),
            ("function", "bound", "bound"),
            ("function", "counter", "counter"),
            ("function", "exported", "exported"),
        ])

    def test_private_class_members_keep_the_hash_that_names_them(self):
        """A private member's name is a private_property_identifier, a
        different node type from the property_identifier every method
        pattern matched, so `#secret(){}` was captured by nothing.

        The `#` STAYS in the symbol and the qualified name. `#open()` and
        `open()` are two different members of one class -- an idiomatic
        pair, the private worker and its public wrapper -- and a stripped
        symbol stored both as `Vault.open`, so a reference to either
        matched both. A record spells the reference `widget.js##open`; the
        path/fragment split takes the first `#`, so the fragment keeps its
        own. Private accessors are accessors, same as public ones."""
        result = self._chunk("private_members.js")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("accessor", "#hidden", "Box.#hidden"),
            ("accessor", "#hidden", "Box.#hidden"),   # get and set, two spans
            ("method", "#open", "Vault.#open"),       # beside a public `open()`
            ("method", "#secret", "Box.#secret"),
            ("method", "open", "Box.open"),
            ("method", "open", "Vault.open"),
        ])

    def test_a_private_member_and_its_public_namesake_resolve_apart(self):
        """The collision the `#` prevents, at the predicate a record is
        checked with: `#Vault.#open` names the private member and nothing
        else, `#Vault.open` the public one."""
        text = (self.CORPUS / "private_members.js").read_text()
        rel = "private_members.js"
        pairs = chunkers.get_chunker("javascript").declared_symbols(text)
        for frag, expected in (("Vault.#open", "Vault.#open"), ("Vault.open", "Vault.open")):
            matched = [qn for _sym, qn in pairs
                       if memidx.fragment_matches_symbol(frag, _sym, qn)]
            self.assertEqual(matched, [expected], frag)
        self.assertIs(
            memidx.fragment_declared_in_text("Vault.#open", text, rel), True
        )

    def test_object_literal_methods_carry_the_binding_that_names_them(self):
        """External gate finding 3. An object literal is no qualification
        container -- nothing in the ancestor walk names it -- so every
        object's `get` was stored as the bare name `get`, and two objects
        in one file were indistinguishable. When the literal is the value
        of a binding, that binding is the name a reader uses. A literal
        with no binding to name it keeps the unqualified method: less
        precise, never dropped."""
        result = self._chunk("object_literals.js")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            # An accessor and a constructor of a bound literal carry the
            # binding too, and each meets its bare reading at its OWN kind:
            # dedup_by_priority sorts kind first, so a bound METHOD reading
            # of `get open(){}` would lose to the bare ACCESSOR one and take
            # the binding down with it.
            ("accessor", "open", "first.open"),
            ("accessor", "open", "second.open"),   # get and set, two spans
            ("accessor", "open", "second.open"),
            ("constructor", "constructor", "second.constructor"),
            ("method", "get", "api.get"),      # const api = { get(){} }
            ("method", "get", "get"),          # an argument literal: no binding to name it
            ("method", "get", "store.get"),    # store = { get(){} }
        ])

    def test_namespaced_react_wrappers_match_the_same_rule_as_bare_ones(self):
        """Second gate finding 8. `React.memo(...)` is a member_expression
        callee, not the identifier the wrapper patterns required, so the
        component was dropped with status=ok while the `import { memo }`
        spelling of the very same wrapper was kept."""
        result = self._chunk("namespaced_wrappers.jsx")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("function", "Forwarded", "Forwarded"),
            ("function", "Memoized", "Memoized"),
        ])

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

    def test_an_error_glued_to_a_clean_definition_does_not_taint_it(self):
        """External gate finding 9 / second gate finding 7. Tree-sitter byte
        ranges are half-open, so the ERROR at [26,29) that starts exactly
        where `ok` ends at [0,26) shares no byte with it. `ok` is a
        definition the parser read perfectly and it survives; the stray
        token is the gap. The same source with a space before the `@`
        always kept `ok` -- one character of whitespace is what decided
        whether a clean definition reached the index."""
        result = self._chunk("adjacent_error.js")
        self.assertEqual(result.status, "partial")
        got = sorted((c["kind"], c["qualified_name"], c["start_line"]) for c in result.chunks)
        self.assertEqual(got, [("function", "later", 2), ("function", "ok", 1)])
        self.assertEqual(result.gaps, [(1, 1, "parse-error")])

    def test_a_missing_token_at_a_definition_s_end_still_taints_it(self):
        """The other half of the same fix: a MISSING token is zero-width,
        and the commonest one -- the closing brace of an unterminated body
        -- sits exactly AT the end of the node it breaks. It keeps the
        inclusive test, so `b` is a gap rather than a chunk the parser only
        guessed at, while `a` above it is untouched."""
        chunkers.treesitter.reset_cache()
        result = chunkers.get_chunker("javascript").chunk_file(
            "function a(){}\nfunction b(){\n", "unterminated.js"
        )
        self.assertEqual(result.status, "partial")
        self.assertEqual([c["qualified_name"] for c in result.chunks], ["a"])
        self.assertEqual(result.gaps, [(2, 2, "parse-error")])

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


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTypeScriptExtraction(unittest.TestCase):
    # Task 4: chunkers/queries/typescript.scm is the SAME file the "tsx"
    # LANGUAGE_TABLE row reads (ruling 83) -- this class exercises it
    # through the "typescript" row/grammar; TestTsxExtraction below
    # exercises the identical file through the "tsx" row/grammar.
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "typescript_corpus"

    def _chunk(self, name):
        return _tree_sitter_chunk("typescript", self.CORPUS, name)

    def test_basic_recall_skips_overload_signatures(self):
        result = self._chunk("basic.ts")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "plain"), ("function", "arrowed"),
            ("function", "overloaded"),          # the ONE implementation, not the two signatures
            ("function", "Util.helper"),
            ("constructor", "Widget.constructor"), ("accessor", "Widget.value"),
        ]))
        self.assertEqual(len(result.chunks), 6)   # capture-count golden -- overload pair excluded

    def test_module_namespace_generator_private_and_object_literal(self):
        """The typescript half of the query fixes, in one fixture because
        they share one file: `module M {}` qualifies what it holds exactly
        as `namespace M {}` does (they are one construct with two
        spellings, and the grammar gives them two node types -- second gate
        finding 5); a generator is a function chunk (finding 2 of both
        gates); `#secret()` is chunked under the `#`-less name and TS's own
        `private hidden()` keeps its keyword-less one (second gate finding
        4 -- `private` is a modifier, so that name was never the problem);
        and an object literal's method carries the binding that names it
        (external gate finding 3); `abstract class A {}` qualifies what it
        holds exactly as `class A {}` does -- it is a different node type,
        abstract_class_declaration, that was missing from this row's
        containers (external-gate report residual 9)."""
        result = self._chunk("module_and_members.ts")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("function", "area", "Shapes.area"),      # module M {}
            ("function", "ids", "ids"),               # function* ids()
            # declare module "vendor-lib" {}: an ambient EXTERNAL module,
            # the same `module` node type as `module M {}` but named by a
            # quoted specifier, not a scope. It qualifies nothing.
            ("function", "shim", "shim"),
            ("function", "volume", "Solids.volume"),  # namespace M {}
            ("method", "#secret", "Box.#secret"),     # #secret()
            ("method", "get", "api.get"),             # const api = { get(){} }
            ("method", "hidden", "Box.hidden"),       # private hidden()
            ("method", "m2", "A.m2"),                 # abstract class A { m2(){} }
        ])

    def test_no_callable_file(self):
        result = self._chunk("no_callable.ts")
        self.assertEqual((result.status, result.chunks, result.gaps), ("ok", [], []))

    def test_whole_file_syntax_error(self):
        result = self._chunk("syntax_error.ts")
        self.assertEqual(result.status, "failed")

    def test_nested_callables_are_kept_as_separate_chunks(self):
        result = self._chunk("nested_calls.ts")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "outer"), ("function", "inner"),
            ("method", "Widget.method"), ("function", "Widget.helper"),
        ]))
        self.assertEqual(len(result.chunks), 4)

    def test_bound_function_expression_is_a_function_chunk(self):
        # typescript.scm's variable_declarator pattern's function_expression
        # branch -- unexercised by any brief-listed fixture (basic.ts's
        # `arrowed` only hits the arrow_function branch of the same
        # alternation), same gap Task 3's review round 1 found in
        # javascript.scm. `const boxed = function (x) {...}`.
        result = self._chunk("extra_patterns.ts")
        boxed = [c for c in result.chunks if c["qualified_name"] == "boxed"]
        self.assertEqual(len(boxed), 1)
        self.assertEqual((boxed[0]["kind"], boxed[0]["symbol"]), ("function", "boxed"))
        self.assertEqual((boxed[0]["start_line"], boxed[0]["end_line"]), (1, 3))

    def test_set_accessor_is_its_own_accessor_chunk(self):
        # typescript.scm's "set" method_definition pattern -- unexercised by
        # any brief-listed fixture (basic.ts's Widget has only `get value`).
        # Both the get and the set accessor for the same symbol survive as
        # separate chunks (different spans, same qualified_name) -- dedup
        # only merges same-span or same-symbol-containment matches, and
        # neither applies to two sibling accessor methods.
        result = self._chunk("extra_patterns.ts")
        accessors = sorted(
            (c["kind"], c["symbol"], c["qualified_name"], c["start_line"], c["end_line"])
            for c in result.chunks if c["qualified_name"] == "Accessors.value"
        )
        self.assertEqual(accessors, [
            ("accessor", "value", "Accessors.value", 6, 8),
            ("accessor", "value", "Accessors.value", 10, 12),
        ])


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestTsxExtraction(unittest.TestCase):
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "tsx_corpus"

    def _chunk(self, name):
        return _tree_sitter_chunk("tsx", self.CORPUS, name)

    def test_react_components_recall_exactly_one_chunk_per_component(self):
        # Ruling 84: re-verified this revision against the real
        # language_tsx() grammar plus the dedup pipeline, not merely
        # assumed identical to the plain-JS case.
        result = self._chunk("react_components.tsx")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "Plain"), ("function", "Named"),
            ("function", "Fwd"), ("function", "DefaultOne"),
        ]))
        self.assertEqual(len(result.chunks), 4)   # exactly one chunk per component

    def test_module_namespaced_wrapper_and_private_member(self):
        """The same query file through the tsx row and grammar: TSX is not
        assumed to follow typescript, it is checked. `module M {}`
        qualifies, `React.memo(...)` matches the wrapper rule its bare
        spelling already matched, and a private member is chunked under its
        `#`-less name."""
        result = self._chunk("module_and_wrappers.tsx")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("function", "Memoized", "Memoized"),
            ("function", "area", "Shapes.area"),
            ("function", "shim", "shim"),          # declare module "vendor-lib" {}
            ("method", "#secret", "Box.#secret"),
        ])

    def test_localized_error_is_partial(self):
        # Deviation from the brief's literal fixture (see task-4-report.md):
        # an unclosed `(` in a TSX function header does not confine the
        # ERROR node the way it does in the JS grammar -- verified against
        # the real language_tsx() (and language_typescript()) grammar, the
        # unclosed paren's ERROR recovery swallows every following token,
        # including AlsoGood's own definition (reparsed as an unbound,
        # nested function_expression no query pattern matches), all the way
        # to EOF. error_recovery.tsx instead uses a malformed expression
        # `@@@` inside Broken's (otherwise well-formed) body, which the TSX
        # grammar recovers from locally -- Good and AlsoGood both parse as
        # their own clean function_declaration nodes; only Broken's own
        # function_declaration span overlaps the ERROR interval and is
        # dropped as a gap.
        result = self._chunk("error_recovery.tsx")
        self.assertEqual(result.status, "partial")
        names = {c["qualified_name"] for c in result.chunks}
        self.assertEqual(names, {"Good", "AlsoGood"})
        self.assertEqual(result.gaps, [(5, 7, "parse-error")])

    def test_typescript_and_tsx_are_independent_rows(self):
        chunkers.treesitter.reset_cache()
        ts_inst = chunkers.treesitter.for_language("typescript")
        tsx_inst = chunkers.treesitter.for_language("tsx")
        self.assertIsNot(ts_inst, tsx_inst)
        chunkers.treesitter.reset_cache()

    def test_wrapped_function_expression_is_a_function_chunk(self):
        # typescript.scm's memo/forwardRef-wrapped variable_declarator
        # pattern's function_expression branch -- react_components.tsx's
        # `Named`/`Fwd` only exercise the arrow_function branch of the same
        # alternation. `const Boxed = memo(function (props) {...})`.
        result = self._chunk("wrapped_function_expression.tsx")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "Boxed", "Boxed"))
        self.assertEqual((c["start_line"], c["end_line"]), (3, 5))

    def test_default_export_wrapped_arrow_yields_one_function_default_chunk(self):
        # typescript.scm's `export default memo/forwardRef(...)` pattern --
        # entirely unexercised by any brief-listed fixture (DefaultOne in
        # react_components.tsx is a plain `export default function`, a
        # DIFFERENT pattern). Arrow branch: `export default memo(() => {...})`.
        result = self._chunk("default_export_wrapped_arrow.tsx")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "default", "default"))
        self.assertEqual((c["start_line"], c["end_line"]), (3, 5))

    def test_default_export_wrapped_function_expression_span_is_the_inner_callable(self):
        # Same pattern as above, function_expression branch:
        # `export default forwardRef(function (props, ref) {...})`. Span is
        # the inner function_expression's own lines, not the wrapping call
        # or the `export default` keyword's line.
        result = self._chunk("default_export_wrapped_function.tsx")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "default", "default"))
        self.assertEqual((c["start_line"], c["end_line"]), (3, 5))

    def test_anonymous_default_export_arrow_yields_one_function_default_chunk(self):
        # typescript.scm's plain (unwrapped) anonymous `export default
        # (arrow_function|function_expression)` pattern -- entirely
        # unexercised by any brief-listed fixture. Arrow branch.
        result = self._chunk("anonymous_default_arrow.tsx")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "default", "default"))
        self.assertEqual((c["start_line"], c["end_line"]), (1, 3))

    def test_anonymous_default_export_function_expression_yields_one_function_default_chunk(self):
        # Same pattern, function_expression branch: `export default
        # function () {...}` (no name -- `export default function Named(){}`
        # is a DIFFERENT grammar shape, a named function_declaration,
        # already covered by react_components.tsx's DefaultOne).
        result = self._chunk("anonymous_default_function.tsx")
        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.chunks), 1)
        c = result.chunks[0]
        self.assertEqual((c["kind"], c["symbol"], c["qualified_name"]), ("function", "default", "default"))
        self.assertEqual((c["start_line"], c["end_line"]), (1, 3))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestJavaExtraction(unittest.TestCase):
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "java_corpus"

    def _chunk(self, name):
        return _tree_sitter_chunk("java", self.CORPUS, name)

    def test_widget_recall_excludes_interface_and_abstract_signatures(self):
        result = self._chunk("Widget.java")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("constructor", "Widget.Widget"), ("method", "Widget.getValue"),
            ("constructor", "Widget.Point.Point"),
        ]))
        self.assertEqual(len(result.chunks), 3)   # capture-count golden -- greet()/act() excluded

    def test_no_callable_file(self):
        result = self._chunk("NoCallable.java")
        self.assertEqual((result.status, result.chunks, result.gaps), ("ok", [], []))

    def test_whole_file_syntax_error(self):
        result = self._chunk("SyntaxError.java")
        self.assertEqual(result.status, "failed")

    def test_localized_error_is_partial(self):
        # Deviation from the brief's literal fixture (see task-5-report.md):
        # the brief's unclosed `(` in `broken`'s header does not confine the
        # ERROR node the way the brief's asserted result requires -- verified
        # against the real grammar: the ERROR span absorbs `alsoGood`'s own
        # header as a misparsed `formal_parameter` inside `broken`'s
        # parameter list, so `alsoGood` never becomes its own
        # method_declaration node at all (no query pattern can capture what
        # isn't a method_declaration). ErrorRecovery.java's fixture instead
        # uses a malformed expression (`@@@`) inside `broken`'s (otherwise
        # well-formed) body -- the grammar recovers from this locally, so
        # `good` and `alsoGood` both parse as clean method_declaration
        # nodes; only `broken`'s own span overlaps the ERROR interval and is
        # dropped as a gap.
        result = self._chunk("ErrorRecovery.java")
        self.assertEqual(result.status, "partial")
        names = {c["qualified_name"] for c in result.chunks}
        self.assertEqual(names, {"ErrorRecovery.good", "ErrorRecovery.alsoGood"})

    def test_inner_class_method_is_kept_separate_from_the_outer_method(self):
        # Revision 3, binding addition b -- Java's "legitimately nested
        # callable" shape is a method inside an inner CLASS (Java has no
        # local/nested named function declarations); re-verified against the
        # real grammar this revision: dedup_nested never merges the two --
        # different symbols ("outerMethod" vs "innerMethod").
        result = self._chunk("NestedClass.java")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("method", "Outer.outerMethod"), ("method", "Outer.Inner.innerMethod"),
        ]))
        self.assertEqual(len(result.chunks), 2)

    def test_javadoc_block_above_a_method_lands_in_its_chunk_doc(self):
        # Task 5 review, ruling 94: _doc_for's hardcoded ("comment",) check
        # never matched Java's own comment node types (block_comment,
        # line_comment), so every Java chunk's `doc` was "" regardless of a
        # real Javadoc block sitting right above it. LANGUAGE_TABLE's java
        # row now carries doc_comment_types=("block_comment",
        # "line_comment"); Widget.java's getValue() has a /** ... */ block
        # immediately above it (verified against the real grammar: node
        # type "block_comment").
        result = self._chunk("Widget.java")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(by_qname["Widget.getValue"]["doc"], "Returns the current value.")
        # The constructor has no comment above it -- doc stays empty, not a
        # leftover from some other chunk.
        self.assertEqual(by_qname["Widget.Widget"]["doc"], "")

    def test_interface_default_method_and_enum_method_are_qualified_by_their_container(self):
        # Task 5 review finding 1: the java row's `interface_declaration`
        # and `enum_declaration` container entries had no shipped fixture
        # exercising them -- Widget.java's own `interface Greeter` and (no
        # enum at all) never produced a chunk in the first place, since
        # `greet()` there has no body and the query requires one. This
        # fixture gives both containers a method WITH a body so the query
        # actually captures it, then asserts the full (kind, symbol,
        # qualified_name) triple through both container types.
        result = self._chunk("InterfaceAndEnum.java")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("method", "greet", "Container.Greeter.greet"),
            ("method", "label", "Container.Color.label"),
            ("method", "addTwo", "Container.addTwo"),
        ]))
        self.assertEqual(len(result.chunks), 3)
        # doc_comment_types' OTHER branch (line_comment) -- Widget.java's
        # own doc test above only exercises block_comment; label() has a
        # `//` line above it so both members of the java row's tuple are
        # actually reached, not just declared.
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(
            by_qname["Container.Color.label"]["doc"], "Human-readable label for this color."
        )

    def test_single_line_javadoc_lands_in_its_chunk_doc(self):
        # Fix round 1, finding 2: Widget.java's own Javadoc test above
        # (test_javadoc_block_above_a_method_lands_in_its_chunk_doc) only
        # ever exercised the MULTI-line `/** \n * text \n */` form, where
        # _doc_for's line-by-line loop returns on the first non-empty line
        # (the `* text` line) and never reaches the closing `*/` line --
        # so it never exercised the bug a single-line Javadoc surfaces:
        # `/** Javadoc. */` used to yield doc == "Javadoc. */", the
        # trailing delimiter surviving untouched. Container.addTwo's
        # single-line Javadoc is that regression test.
        result = self._chunk("InterfaceAndEnum.java")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(by_qname["Container.addTwo"]["doc"], "Javadoc.")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPhpExtraction(unittest.TestCase):
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "php_corpus"

    def _chunk(self, name):
        return _tree_sitter_chunk("php", self.CORPUS, name)

    def test_braced_namespaces_qualify_their_declarations(self):
        """External gate finding 4. Two namespaces in one file, each with a
        class of the same name -- the shape that made the gap visible:
        without the namespace both `Box::open`s were stored as `Box.open`,
        one name for two different methods.

        The separator is the dot every other qualified name in this index
        uses, not PHP's own `\\`: one join character keeps memlint's suffix
        rule (`qualified_name.endswith("." + fragment)`) working the same
        way for every language, so `#Box.open` resolves here exactly as it
        does in Java or Swift."""
        result = self._chunk("namespaced_braced.php")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("method", "open", "Archive.Box.open"),
            ("method", "open", "Storage.Box.open"),
        ])

    def test_unbraced_namespace_qualifies_what_follows_it(self):
        """The other spelling of the same construct, and the harder one:
        `namespace Storage;` CONTAINS nothing -- it is a statement, and
        everything after it in the file is in its namespace by position
        alone -- so the ancestor walk could never find it. A class and a
        plain function both pick it up."""
        result = self._chunk("namespaced_unbraced.php")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("function", "helper", "Storage.helper"),
            ("method", "open", "Storage.Box.open"),
        ])

    def test_widget_recall_with_embedded_html(self):
        result = self._chunk("widget.php")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "top_level", "top_level"),
            ("constructor", "__construct", "Widget.__construct"),
            ("method", "render", "Widget.render"),
            ("method", "greet", "Greets.greet"),
        ]))
        self.assertEqual(len(result.chunks), 4)   # capture-count golden

    def test_no_callable_pure_html_file(self):
        result = self._chunk("no_callable.php")
        self.assertEqual((result.status, result.chunks, result.gaps), ("ok", [], []))

    def test_whole_file_syntax_error(self):
        result = self._chunk("syntax_error.php")
        self.assertEqual(result.status, "failed")

    def test_localized_error_is_partial(self):
        # Deviation from the brief's literal fixture (see task-6-report.md):
        # the brief's unclosed `(` in `broken`'s header does not confine the
        # ERROR node the way the brief's asserted result requires --
        # verified against the real grammar: the ERROR span absorbs
        # `alsoGood`'s own header as a misparsed parameter inside
        # `broken`'s formal_parameters list, merging both into ONE
        # function_definition node (no query pattern can capture what isn't
        # its own function_definition). error_recovery.php's fixture
        # instead uses a malformed expression (`return @@@;`, PHP's
        # error-suppression operator applied to a missing operand) inside
        # `broken`'s otherwise well-formed header+body -- the grammar
        # leaves a single zero-width MISSING token confined to `broken`'s
        # own span, so `good` and `alsoGood` both parse as clean,
        # untainted function_definition nodes; only `broken`'s span
        # overlaps the taint and is dropped as a gap. Same shape as the
        # Java ErrorRecovery.java deviation from Task 5.
        result = self._chunk("error_recovery.php")
        self.assertEqual(result.status, "partial")
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "good", "good"), ("function", "alsoGood", "alsoGood"),
        ]))

    def test_nested_function_is_kept_separate_from_the_outer_one(self):
        result = self._chunk("nested_calls.php")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "outer", "outer"), ("function", "inner", "inner"),
        ]))
        self.assertEqual(len(result.chunks), 2)

    def test_docblock_above_a_function_lands_in_its_chunk_doc(self):
        # Task 5 review, ruling 94: PHP's own comment node type is
        # "comment" (verified against the real grammar -- both /** */
        # docblocks and // line comments), matching _doc_for's default, so
        # the php row needs no doc_comment_types override; this asserts
        # that default actually reaches a PHP docblock end to end.
        result = self._chunk("doc_comment.php")
        self.assertEqual(result.status, "ok")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(
            by_qname["compute_total"]["doc"], "Computes the widget total."
        )

    def test_bodyless_interface_and_abstract_methods_are_never_chunked(self):
        # Fix round 1, two findings closed by one fixture:
        # - Finding 1 (moderate): php's own `containers["enum_declaration"]`
        #   entry had no fixture anywhere in the corpus -- same class of gap
        #   Task 5's review flagged for Java (closed for Java by this task's
        #   own item B, not mirrored for PHP until now). `Suit.label` below
        #   is that coverage.
        # - Finding 2 (moderate): php.scm's method_declaration patterns now
        #   require body: (compound_statement) (matching java.scm's own
        #   body: (block) constraint) -- an interface method signature and
        #   an abstract method declaration (neither has a body) must never
        #   become a chunk, unqualified or otherwise. The concrete sibling
        #   methods in the SAME file (a regular class method, an enum
        #   method) must still be chunked normally -- the body constraint
        #   only excludes the specific bodyless nodes, not their
        #   containers. Deliberately NOT adding `interface_declaration` to
        #   php's `containers` (coordinator ruling): its methods have no
        #   bodies, so nothing inside it is ever chunked in the first
        #   place -- a qualifier entry for it would qualify nothing.
        result = self._chunk("bodyless_and_enum.php")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("method", "describe", "Shape.describe"),
            ("method", "label", "Suit.label"),
        ]))
        self.assertEqual(len(result.chunks), 2)   # capture-count golden --
        # interface Greeter's greet() and abstract class Shape's area()
        # both excluded, no unqualified "greet"/"area" chunk of any kind


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestRustExtraction(unittest.TestCase):
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "rust_corpus"

    def _chunk(self, name):
        return _tree_sitter_chunk("rust", self.CORPUS, name)

    def test_generic_impls_qualify_by_the_bare_type_name(self):
        """Second gate finding 1. The impl's self type was copied whole, so
        `impl<T> Widget<T>` stored `Widget<T>.get` -- a name no reference to
        that method ever spells, and one that splits a type's methods
        across as many qualifiers as it has impl blocks. Generic impls are
        the common form. The lifetime case (`impl<'a> Handle<'a>`) is the
        same shape, and a trait impl (`impl<T> Render for Widget<T>`)
        qualifies by the type exactly as `impl Render for Widget` already
        did."""
        result = self._chunk("generic_impl.rs")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("method", "get", "Widget.get"),
            ("method", "name", "Handle.name"),
            ("method", "render", "Widget.render"),
        ])

    def test_widget_recall_impl_trait_mod_qualification(self):
        result = self._chunk("widget.rs")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "top_level"),
            ("method", "Widget.new"), ("method", "Widget.value"),
            ("method", "Greeter.greet"),        # trait default method
            ("method", "Widget.greet"),         # impl Greeter for Widget -- qualified by the TYPE
            ("function", "util.helper"),        # mod, not a type -- stays `function`
            ("function", "add_two"),            # single-line block comment above it (fix round 1)
        ]))
        self.assertEqual(len(result.chunks), 7)   # capture-count golden --
        # 6 in the brief's own text, +1 for `add_two` (fix round 1, finding 1/2)
        # Coverage rule: bodyless declarations must not produce chunks.
        # widget.rs's own `trait Named { fn name(&self) -> String; }` has a
        # signature with no default body -- verified against the real
        # grammar (tree_sitter_rust 0.24.2) that this parses as the
        # DISTINCT node type `function_signature_item`, never
        # `function_item`, so rust.scm's single pattern (which only matches
        # `function_item` and additionally requires `body: (block)`) never
        # captures it. No chunk named "name" should exist anywhere in the
        # 7 chunks above.
        self.assertNotIn("name", {c["symbol"] for c in result.chunks})

    def test_single_line_block_comment_lands_in_its_chunk_doc(self):
        # Fix round 1, findings 1+2: rust's own doc_comment_types row
        # declares ("line_comment", "block_comment") but only line_comment
        # had a committed test (see test_triple_slash_doc_comment_lands_in_
        # its_chunk_doc below) -- block_comment was verified only by a
        # one-off manual probe during Task 7's original development, per
        # the review. That probe surfaced a real, pre-existing
        # chunkers/treesitter.py bug (shared by every language whose
        # doc_comment_types includes a block-comment type, not specific to
        # rust): `_doc_for` stripped only the LEFT-hand comment markers, so
        # a SINGLE-LINE block comment `/* Adds two numbers. */` used to
        # yield doc == "Adds two numbers. */" -- the closing delimiter
        # survived. Both are fixed together: widget.rs's own `add_two` has
        # `/* Adds two numbers. */` immediately above it.
        result = self._chunk("widget.rs")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(by_qname["add_two"]["doc"], "Adds two numbers.")

    def test_no_callable_file(self):
        result = self._chunk("no_callable.rs")
        self.assertEqual((result.status, result.chunks, result.gaps), ("ok", [], []))

    def test_whole_file_syntax_error(self):
        result = self._chunk("syntax_error.rs")
        self.assertEqual(result.status, "failed")

    def test_localized_error_is_partial(self):
        result = self._chunk("error_recovery.rs")
        self.assertEqual(result.status, "partial")
        names = {c["qualified_name"] for c in result.chunks}
        self.assertEqual(names, {"good", "also_good"})

    def test_nested_fn_is_kept_separate_from_the_outer_one(self):
        # Revision 3, binding addition b: a `fn` nested inside another `fn`
        # is its own `function` chunk, never merged with the enclosing one
        # -- and when the OUTER fn sits inside an `impl` block, the ancestor
        # walk that promotes `outer` to `method` finds the same `impl_item`
        # ancestor for the nested `inner` fn too (the walk does not stop at
        # the first function boundary), so BOTH `Widget.method` and
        # `Widget.helper` come back kind `method`, as two separate chunks
        # since their symbols differ -- never a dedup_nested hazard because
        # it never collides on symbol. Verified against the real grammar.
        result = self._chunk("nested_calls.rs")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "outer"), ("function", "inner"),
            ("method", "Widget.method"), ("method", "Widget.helper"),
        ]))
        self.assertEqual(len(result.chunks), 4)

    def test_triple_slash_doc_comment_lands_in_its_chunk_doc(self):
        # Context note (not in the brief's literal steps): rust's own
        # comment node types are "line_comment"/"block_comment", verified
        # against the real grammar -- and a `///` doc comment is NOT a
        # distinct top-level node type in this grammar version; it parses
        # as an ordinary `line_comment` node whose children
        # (`outer_doc_comment_marker`, `doc_comment`) carry the marking
        # internally, so the FULL node text (including the `///` prefix)
        # is what _doc_for reads and strips. widget.rs's own `top_level`
        # has `/// Adds one to the input.` immediately above it.
        result = self._chunk("widget.rs")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(by_qname["top_level"]["doc"], "Adds one to the input.")
        # A container method with no comment above it -- doc stays empty.
        self.assertEqual(by_qname["Widget.new"]["doc"], "")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestLuaExtraction(unittest.TestCase):
    CORPUS = REPO_ROOT / "tests" / "fixtures" / "lua_corpus"

    def _chunk(self, name):
        return _tree_sitter_chunk("lua", self.CORPUS, name)

    def test_multi_segment_table_names_keep_the_whole_path(self):
        """External gate finding 5, second gate finding 6. A dotted name of
        more than one segment (`function App.Services.load()`, the shape of
        every Lua module of any size) nests one dot_index_expression inside
        another, so the identifier-only table capture matched nothing and
        the function vanished with status=ok. All three declaration forms
        -- dot, colon, and the assignment form -- carry the full path."""
        result = self._chunk("nested_tables.lua")
        self.assertEqual((result.status, result.gaps), ("ok", []))
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, [
            ("method", "load", "App.Services.load"),      # function App.Services.load()
            ("method", "reload", "App.Services.reload"),  # function App.Services:reload()
            ("method", "save", "App.Services.save"),      # App.Services.save = function()
            ("method", "single", "M.single"),             # one segment, unchanged
        ])

    def test_widget_recall_dotted_and_colon_forms(self):
        # Pattern-index coverage: widget.lua alone exercises all four
        # lua.scm patterns -- pattern 0 (plain/local function_declaration:
        # `plain`, `loc`), pattern 1 (dot_index_expression method:
        # `M.g`), pattern 2 (method_index_expression: `obj.m`), pattern 3
        # (assignment_statement RHS function_definition: `M.f`). Verified
        # against the real grammar (tree_sitter_lua 0.5.0) this revision.
        result = self._chunk("widget.lua")
        self.assertEqual(result.status, "ok")
        # kind, symbol AND qualified_name -- not just the pair -- so
        # patterns 1/2/3's own job (stripping the qualifier back out of
        # the symbol: `M.g` -> symbol `g`, `obj.m` -> symbol `m`, `M.f` ->
        # symbol `f`) is actually asserted, not just the qualified result
        # patterns 0/1/2/3 all happen to agree on.
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "plain", "plain"), ("function", "loc", "loc"),
            ("method", "f", "M.f"), ("method", "g", "M.g"), ("method", "m", "obj.m"),
        ]))
        self.assertEqual(len(result.chunks), 5)   # capture-count golden
        for c in result.chunks:
            self.assertEqual(c["lang"], "lua")
            self.assertIn(c["kind"], chunkers.KINDS)

    def test_no_callable_file(self):
        result = self._chunk("no_callable.lua")
        self.assertEqual((result.status, result.chunks, result.gaps), ("ok", [], []))

    def test_whole_file_syntax_error(self):
        result = self._chunk("syntax_error.lua")
        self.assertEqual(result.status, "failed")

    def test_localized_error_is_partial(self):
        result = self._chunk("error_recovery.lua")
        self.assertEqual(result.status, "partial")
        names = {c["qualified_name"] for c in result.chunks}
        self.assertEqual(names, {"good", "also_good"})

    def test_declared_symbols_accepts_bare_and_dotted_fragment(self):
        chunkers.treesitter.reset_cache()
        text = (self.CORPUS / "widget.lua").read_text()
        pairs = chunkers.get_chunker("lua").declared_symbols(text)
        self.assertIn(("m", "obj.m"), pairs)

    def test_nested_local_function_is_kept_separate_from_the_outer_one(self):
        # Revision 3, binding addition b: a `local function` nested inside
        # another `function` is its own `function` chunk, never merged
        # with the enclosing one -- verified against the real grammar,
        # both function_declaration nodes match pattern 0, different
        # symbols (outer vs inner) so dedup_nested never collides them.
        result = self._chunk("nested_calls.lua")
        self.assertEqual(result.status, "ok")
        got = sorted((c["kind"], c["symbol"], c["qualified_name"]) for c in result.chunks)
        self.assertEqual(got, sorted([
            ("function", "outer", "outer"), ("function", "inner", "inner"),
        ]))
        self.assertEqual(len(result.chunks), 2)

    def test_assignment_form_doc_comment_anchors_on_the_statement_not_the_rhs(self):
        # Task 8 review, finding 2: `M.f = function() ... end`'s @chunk.method
        # capture binds the anonymous function_definition nested inside
        # expression_list, which has no preceding sibling of its own -- a
        # `--` doc comment directly above the ASSIGNMENT never reached the
        # chunk's `doc` field until lua.scm's @chunk.doc_anchor fix (Task 9).
        result = self._chunk("widget.lua")
        self.assertEqual(result.status, "ok")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(by_qname["M.f"]["doc"], "Overwrites the widget's f field.")

    def test_dash_dash_doc_comment_lands_in_its_chunk_doc(self):
        # Context note (not in the brief's literal fixture list): Lua's own
        # comment node type is "comment" (verified against the real
        # grammar -- a `--` line comment and a `--[[ ]]` block comment are
        # both node type "comment"), matching _doc_for's default
        # ("comment",), so the lua row needs no doc_comment_types override
        # -- this asserts that default actually reaches a Lua `--` doc
        # comment end to end (same shape as php's doc_comment.php, which
        # needed no override for the identical reason).
        result = self._chunk("doc_comment.lua")
        self.assertEqual(result.status, "ok")
        by_qname = {c["qualified_name"]: c for c in result.chunks}
        self.assertEqual(by_qname["compute_total"]["doc"], "Computes the widget total.")
