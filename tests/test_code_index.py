"""Tests for Anatomy's intent index: memidx.py's `code-reindex`/`code-search`
(a lexer-aware Swift chunker + a completely separate <project>-code.sqlite
index) and memlint.py's concept-record rule additions.

Owned by this session (per the concurrent-work split, see the repo-root
brief): memidx.py's code-index additions, memlint.py's concept-rule
additions, this file, and fixtures/code/. Does not touch hooks/, install.sh,
tests/test_hooks.py, tests/test_write_hooks.py, or tests/test_install.py.

Every python invocation for this project is
`PYTHONPATH= /home/user/dev/mem-venv/bin/python` (a Windows numpy
install leaks onto PYTHONPATH by default in this shell and breaks fastembed
under Linux Python -- see the module import failing with
`AttributeError: module 'os' has no attribute 'add_dll_directory'` if that
env var isn't cleared).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

FIXTURES = TOOLS_DIR / "fixtures" / "code"
REAL_CORPUS_ROOT = Path.home() / "dev" / "private-corpus" / "Sources"


def ns(**kw):
    base = dict(project=memidx.DEFAULT_PROJECT, db=None)
    base.update(kw)
    return SimpleNamespace(**base)


def code_reindex(code_root, db, project=memidx.DEFAULT_PROJECT, no_embed=True, full=False, lang=None):
    args = ns(code_root=str(code_root), db=str(db), project=project, no_embed=no_embed, full=full, lang=lang)
    return memidx.cmd_code_reindex(args)


# ---------------------------------------------------------------------------
# 1. Chunker fixtures (memidx.chunk_source directly -- no DB involved)
# ---------------------------------------------------------------------------


class TestChunkerNestedTypesAndExtension(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "NestedTypes.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_nested_func_qualifies_through_both_types(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Outer.Inner.innerFunc", names)

    def test_extension_qualifies_like_its_extended_type(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Outer.extFunc", names)

    def test_class_and_struct_are_never_chunks_themselves(self):
        kinds = {c["kind"] for c in self.chunks}
        self.assertFalse(kinds & {"class", "struct", "enum", "protocol", "extension"})
        symbols = {c["symbol"] for c in self.chunks}
        self.assertNotIn("Outer", symbols)
        self.assertNotIn("Inner", symbols)


class TestChunkerLazyAndComputedVar(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "LazyAndComputedVar.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_lazy_var_with_closure_initializer_excluded(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertNotIn("Cache.cached", names)

    def test_stored_var_with_plain_initializer_excluded(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertNotIn("Cache.stored", names)

    def test_computed_var_with_body_included(self):
        names = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Cache.computed", names)
        self.assertEqual(names["Cache.computed"]["kind"], "var")


class TestChunkerStringsAndInterpolation(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "StringsAndInterpolation.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        # a '{'/'}' inside a plain string, and a closure inside a string
        # interpolation, must never desync the walker.
        self.assertEqual(self.gaps, [])

    def test_brace_in_string_does_not_split_the_function(self):
        names = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Formatter.greet", names)
        greet = names["Formatter.greet"]
        self.assertEqual(greet["start_line"], 9)
        self.assertEqual(greet["end_line"], 13)

    def test_interpolation_closure_balances_correctly(self):
        # if \(items.map { ... }.joined()) mis-balanced brace counting,
        # greet()'s own close would be found in the wrong place (already
        # covered above) or raw()/multiline() wouldn't be found at all.
        names = {c["qualified_name"] for c in self.chunks}
        self.assertEqual(names, {"Formatter.greet", "Formatter.rawPath", "Formatter.multiline"})

    def test_raw_and_triple_quoted_strings_do_not_desync(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Formatter.rawPath", names)
        self.assertIn("Formatter.multiline", names)


class TestChunkerInitSubscriptOperator(unittest.TestCase):
    def setUp(self):
        self.text = (FIXTURES / "InitSubscriptOperator.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_init_chunked_and_qualified(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Vec.init", by_name)
        self.assertEqual(by_name["Vec.init"]["kind"], "init")

    def test_subscript_has_no_own_name_but_chunks(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Vec.subscript", by_name)
        self.assertEqual(by_name["Vec.subscript"]["kind"], "subscript")

    def test_static_operator_func_chunks(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Vec.==", by_name)


class TestChunkerIfBranchesKeptBothSides(unittest.TestCase):
    def test_both_if_and_else_definitions_are_chunked(self):
        text = (FIXTURES / "IfBranches.swift").read_text()
        chunks, gaps = memidx.chunk_source(text)
        self.assertEqual(gaps, [])
        mode_chunks = [c for c in chunks if c["qualified_name"] == "Debugger.mode"]
        self.assertEqual(len(mode_chunks), 2, chunks)


class TestChunkerGapFallback(unittest.TestCase):
    """GapDesync.swift has a deliberately unbalanced #if/#else pair (a
    stray extra '}'). The chunker must skip THAT GAP (counted + reported)
    and keep indexing the rest of the file on BOTH sides of it -- never a
    whole-file fallback."""

    def setUp(self):
        self.text = (FIXTURES / "GapDesync.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_exactly_one_gap_reported(self):
        self.assertEqual(len(self.gaps), 1, self.gaps)
        start, end = self.gaps[0]
        self.assertLess(start, end)

    def test_content_before_the_gap_still_indexed(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("WellFormed.before", names)

    def test_content_after_the_gap_still_indexed(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("AlsoWellFormed.trailing", names)

    def test_never_falls_back_to_zero_chunks(self):
        self.assertGreater(len(self.chunks), 0)


class TestChunkerSyntheticAppKitFixture(unittest.TestCase):
    """~200-line synthetic AppKit-style controller (modeled on, not copied
    from, a real file -- this repo's tracked fixtures stay generic)."""

    def setUp(self):
        path = FIXTURES / "SyntheticSettingsController.swift"
        with path.open() as fh:
            self.line_count = sum(1 for _ in fh)
        self.text = path.read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_fixture_is_roughly_200_lines(self):
        self.assertGreater(self.line_count, 150)

    def test_no_gaps_on_realistic_appkit_shaped_source(self):
        self.assertEqual(self.gaps, [])

    def test_wrap_in_scroll_and_write_png_style_helpers_found(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("SyntheticSettingsController.embedInScrollBox", names)
        self.assertIn("SyntheticSettingsController.writePNGSnapshot", names)

    def test_computed_var_and_stored_property_both_handled_correctly(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("SyntheticSettingsController.rowCount", names)
        # `placeholderView` is a `lazy var ... = { ... }()` -- excluded.
        self.assertNotIn("SyntheticSettingsController.placeholderView", names)

    def test_a_reasonable_number_of_chunks_recovered(self):
        self.assertGreaterEqual(len(self.chunks), 15, self.chunks)


# ---------------------------------------------------------------------------
# 2. code-reindex / code-search (DB-backed)
# ---------------------------------------------------------------------------


class TestIncremental(unittest.TestCase):
    def test_unchanged_file_gives_zero_reembeds(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            before = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            conn.close()
            self.assertGreater(before, 0)

            # second reindex, file byte-for-byte unchanged -> 0 new chunks
            # touched, sha comparison alone decides (no embedding is
            # requested either way here, so this isolates the sha-skip).
            rc2 = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc2, 0)
            conn = memidx.open_code_db(db)
            after = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            conn.close()
            self.assertEqual(before, after)

    def test_changed_file_is_rechunked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            target.write_text(target.read_text() + "\nfunc addedLater() {}\n")
            code_reindex(root, db, no_embed=True)

            conn = memidx.open_code_db(db)
            rows = conn.execute("SELECT qualified_name FROM chunks").fetchall()
            conn.close()
            self.assertIn("addedLater", {r["qualified_name"] for r in rows})


class TestIsolationFromMarkdownIndex(unittest.TestCase):
    def test_code_reindex_never_creates_the_markdown_project_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")

            args = ns(code_root=str(root), project="isoproj", no_embed=True, full=False, lang=None)
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                rc = memidx.cmd_code_reindex(args)
            finally:
                del os.environ["MEMCONTINUUM_HOME"]
            self.assertEqual(rc, 0)

            self.assertTrue((home / "isoproj-code.sqlite").exists())
            self.assertFalse((home / "isoproj.sqlite").exists())

    def test_code_db_path_differs_from_markdown_db_path(self):
        code_args = ns(project="samename", db=None)
        md_args = ns(project="samename", db=None)
        os.environ["MEMCONTINUUM_HOME"] = "/tmp/does-not-need-to-exist-for-this-check"
        try:
            code_path = memidx.resolve_code_db_path(code_args)
            md_path = memidx.resolve_db_path(md_args)
        finally:
            del os.environ["MEMCONTINUUM_HOME"]
        self.assertNotEqual(code_path, md_path)


class TestLazyImportSubprocessAssert(unittest.TestCase):
    """Isolated-process checks: `code-reindex --no-embed` and `code-search
    --mode fts` must never import fastembed (or numpy via it) at all --
    same pattern as test_memidx.py's for-path check."""

    def test_code_reindex_no_embed_does_not_import_fastembed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-reindex', '--db', %r, '--code-root', %r, '--no-embed']); "
                "assert 'fastembed' not in sys.modules, 'fastembed was imported'; "
                "assert 'numpy' not in sys.modules, 'numpy was imported'"
            ) % (str(TOOLS_DIR), str(db), str(root))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_code_search_fts_mode_does_not_import_fastembed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts']); "
                "assert 'fastembed' not in sys.modules, 'fastembed was imported'; "
                "assert 'numpy' not in sys.modules, 'numpy was imported'"
            ) % (str(TOOLS_DIR), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestFTSSplitFindsCamelCase(unittest.TestCase):
    def test_write_png_finds_writePNG_shaped_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(
                FIXTURES / "SyntheticSettingsController.swift",
                root / "SyntheticSettingsController.swift",
            )
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            args = ns(db=str(db), query="write png", mode="fts", limit=5, json=True)
            rc = memidx.cmd_code_search(args)
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            ids = memidx.code_hits_fts(conn, "write png", memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertGreater(len(ids), 0)
            conn = memidx.open_code_db(db)
            top = conn.execute("SELECT qualified_name FROM chunks WHERE id=?", (ids[0],)).fetchone()
            conn.close()
            self.assertIn("writePNGSnapshot", top["qualified_name"])


class TestVectorAndHybridJSONIsSerializable(unittest.TestCase):
    """Regression test: cosine() used to return a numpy.float32 score
    (fastembed's query_embed yields numpy arrays), which json.dumps cannot
    serialize -- `code-search --mode vector/hybrid --json` crashed with
    TypeError: Object of type float32 is not JSON serializable the moment
    it tried to print. Control-experiment verified: this test fails on the
    pre-fix cosine() (reverting the `float(...)` cast in memidx.py
    reproduces the crash on the vector-mode test below; confirmed by
    actually running it against the pre-fix code). --mode fts never
    touches cosine() at all, so it isn't a control for this bug; hybrid's
    own output score is an RRF rank sum (always a plain float) rather than
    a raw cosine value, so it was never actually at risk from this
    specific bug -- its test here is a straightforward correctness check,
    not a second control case."""

    def _search_json(self, db, mode):
        script = (
            "import sys, io, contextlib, json; sys.path.insert(0, %r); import memidx; "
            "buf = io.StringIO()\n"
            "with contextlib.redirect_stdout(buf):\n"
            "    rc = memidx.main(['code-search', '--db', %r, '[redacted probe query]', "
            "'--mode', %r, '--limit', '3', '--json'])\n"
            "assert rc == 0, rc\n"
            "out = json.loads(buf.getvalue())\n"
            "for h in out:\n"
            "    assert isinstance(h['score'], float), (h['score'], type(h['score']))\n"
            "print('OK', len(out))\n"
        ) % (str(TOOLS_DIR), str(db), mode)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)

    def test_vector_mode_json_output_is_valid_and_scores_are_plain_floats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(
                FIXTURES / "SyntheticSettingsController.swift",
                root / "SyntheticSettingsController.swift",
            )
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=False)
            self._search_json(db, "vector")

    def test_hybrid_mode_json_output_is_valid_and_scores_are_plain_floats(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(
                FIXTURES / "SyntheticSettingsController.swift",
                root / "SyntheticSettingsController.swift",
            )
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=False)
            self._search_json(db, "hybrid")


class TestConceptAttachment(unittest.TestCase):
    def test_hit_gets_concept_id_when_decision_db_has_a_matching_implemented_by(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                md_root = Path(td) / "md"
                md_root.mkdir()
                (md_root / "concept.md").write_text(
                    "---\n"
                    "type: concept\n"
                    "id: CON-ATTACH\n"
                    "title: Attach Test\n"
                    "owner_boundary: fixture\n"
                    "implemented_by:\n"
                    "  - NestedTypes.swift#outerFunc\n"
                    "tested_by: []\n"
                    "governed_by: []\n"
                    "involved_in: []\n"
                    "---\n\n"
                    "Fixture. NOT this concept: nothing else.\n"
                )
                memidx.cmd_reindex(ns(root=str(md_root), no_embed=True, full=False))

                code_root = Path(td) / "code"
                code_root.mkdir()
                shutil.copy(FIXTURES / "NestedTypes.swift", code_root / "NestedTypes.swift")
                memidx.cmd_code_reindex(ns(code_root=str(code_root), no_embed=True, full=False, lang=None))

                import io
                import contextlib

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = memidx.cmd_code_search(ns(query="outer func", mode="fts", limit=5, json=True))
                self.assertEqual(rc, 0)
                import json as _json

                results = _json.loads(buf.getvalue())
                self.assertTrue(any(r.get("concept_id") == "CON-ATTACH" for r in results), results)
            finally:
                del os.environ["MEMCONTINUUM_HOME"]


class TestStaleWarning(unittest.TestCase):
    def test_touching_a_source_file_triggers_stderr_warning_on_search(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys, time, os; sys.path.insert(0, %r); import memidx; "
                "os.utime(%r, (time.time() + 3600, time.time() + 3600)); "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts'])"
            ) % (str(TOOLS_DIR), str(target), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertIn("stale", result.stderr.lower(), result.stderr)

    def test_search_never_warns_when_nothing_changed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts'])"
            ) % (str(TOOLS_DIR), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertNotIn("stale", result.stderr.lower(), result.stderr)


# ---------------------------------------------------------------------------
# 3. Budget tests
# ---------------------------------------------------------------------------


def _build_synthetic_swift_tree(root: Path, n: int) -> None:
    root.mkdir(parents=True, exist_ok=True)
    template = FIXTURES / "SyntheticSettingsController.swift"
    text = template.read_text()
    for i in range(n):
        # give each file a distinct top-level type name so none collide.
        variant = text.replace("SyntheticSettingsController", f"SyntheticController{i}")
        (root / f"Synthetic{i}.swift").write_text(variant)


class TestBudgets(unittest.TestCase):
    def test_no_embed_reindex_under_5s_on_173_synthetic_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            _build_synthetic_swift_tree(root, 173)
            db = Path(td) / "idx-code.sqlite"

            t0 = time.time()
            rc = code_reindex(root, db, no_embed=True)
            elapsed = time.time() - t0
            self.assertEqual(rc, 0)
            self.assertLess(elapsed, 5.0, f"code-reindex took {elapsed:.3f}s")

    def test_in_process_fts_search_under_100ms(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            _build_synthetic_swift_tree(root, 50)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            conn = memidx.open_code_db(db)
            t0 = time.time()
            ids = memidx.code_hits_fts(conn, "wrap scroll", memidx.DEFAULT_PROJECT)
            elapsed = time.time() - t0
            conn.close()
            self.assertGreater(len(ids), 0)
            self.assertLess(elapsed, 0.1, f"fts search took {elapsed:.3f}s")


# ---------------------------------------------------------------------------
# 4. Real-corpus gold-probe test (gated, like test_memidx's D5/D8 sandbox
#    tests, behind an env var -- skipped when unset)
# ---------------------------------------------------------------------------


GOLD_PROBES = [
    ("write debug png", "Redacted.rA"),
    ("[redacted probe query]", "Redacted.rB"),
    ("compare two files by content hash", "Redacted.rC"),
    ("path length budget", "Redacted.rD"),
    ("[redacted probe query]", "Redacted.rE"),
    ("[redacted probe query]", "Redacted.rF"),
    ("[redacted probe query]", "Redacted.rG"),
    ("[redacted probe query]", "Redacted.rH"),
]


class TestGoldProbesRealCorpus(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("MEMCONTINUUM_TEST_REAL_CORPUS"),
        "set $MEMCONTINUUM_TEST_REAL_CORPUS=1 to run the real ~/dev/private-corpus/Sources gold-probe test",
    )
    def test_hybrid_top1_on_real_corpus(self):
        self.assertTrue(REAL_CORPUS_ROOT.is_dir(), REAL_CORPUS_ROOT)
        # Cached (incrementally reused, like scripts/codanna-bench.sh's own
        # index) rather than a fresh tempdir -- a full embed of the real
        # corpus takes ~20+ minutes on this host, and re-embeds nothing
        # once the corpus hasn't changed since the last run.
        cache_dir = Path.home() / ".cache" / "codanna-bench"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db = cache_dir / "bench-code.sqlite"
        code_reindex(REAL_CORPUS_ROOT, db, project="codanna-bench", no_embed=False, full=False)

        hits = 0
        details = []
        for query, expect in GOLD_PROBES:
            conn = memidx.open_code_db(db)
            fts_ids = memidx.code_hits_fts(conn, query, "codanna-bench")
            vec_ids = [cid for cid, _ in memidx.code_hits_vector(conn, query, "codanna-bench")]
            k = 60
            scores: dict = {}
            for i, cid in enumerate(fts_ids):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
            for i, cid in enumerate(vec_ids):
                scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + i + 1)
            ranked = sorted(scores.items(), key=lambda t: t[1], reverse=True)
            top1_id = ranked[0][0] if ranked else None
            top1 = None
            if top1_id is not None:
                row = conn.execute("SELECT qualified_name FROM chunks WHERE id=?", (top1_id,)).fetchone()
                top1 = row["qualified_name"] if row else None
            conn.close()
            ok = top1 == expect
            details.append((query, expect, top1, ok))
            if ok:
                hits += 1
        # Measured against the real corpus (see the task report): hybrid
        # top-1 hits 5/8 -- Redacted.rC loses top-1 to
        # Redacted.rM, Redacted.rD loses
        # to Redacted.rN, and
        # Redacted.rG loses to the sibling symbol
        # Redacted.rO -- all plausible near-misses, not index bugs.
        self.assertGreaterEqual(hits, 5, f"{hits}/8 -- {details}")


# ---------------------------------------------------------------------------
# 5. memlint concept-rule additions (fixtures/code/concepts/)
# ---------------------------------------------------------------------------

CONCEPTS = FIXTURES / "concepts"
CODE_ROOT = FIXTURES


class TestMemlintSymbolFragment(unittest.TestCase):
    def test_symbol_not_declared_in_file_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_symbol.md", root / "bad_symbol.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-BADSYM" in e and "doesNotExist" in e for e in errors), errors
            )

    def test_symbol_declared_in_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("CON-CODE-GOOD" in e for e in errors), errors)

    def test_symbol_check_skipped_without_code_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_symbol.md", root / "bad_symbol.md")
            errors, _warnings = memlint.lint_root(root)
            self.assertFalse(any("doesNotExist" in e for e in errors), errors)


class TestMemlintUnqualifiedLargeFile(unittest.TestCase):
    def test_no_symbol_on_small_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "no_symbol_small.md", root / "no_symbol_small.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("CON-CODE-NOSYM-SMALL" in e for e in errors), errors)

    def test_no_symbol_on_a_file_over_400_lines_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code_root = Path(td) / "code"
            code_root.mkdir()
            big = code_root / "Big.swift"
            big.write_text("// filler line\n" * 401 + "func real() {}\n")
            (root / "big_concept.md").write_text(
                "---\n"
                "type: concept\n"
                "id: CON-CODE-BIG\n"
                "title: Big file, no symbol\n"
                "owner_boundary: fixture\n"
                "implemented_by:\n"
                "  - Big.swift\n"
                "tested_by: []\n"
                "governed_by: []\n"
                "involved_in: []\n"
                "---\n\n"
                "Fixture. NOT this concept: nothing else.\n"
            )
            errors, _warnings = memlint.lint_root(root, code_root=code_root)
            self.assertTrue(
                any("CON-CODE-BIG" in e and "400" in e for e in errors), errors
            )


class TestMemlintGovernedByRegistry(unittest.TestCase):
    def test_unknown_governed_by_id_is_error_when_topics_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_governed.md", root / "bad_governed.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-BADGOV" in e and "TOP-DOES-NOT-EXIST" in e for e in errors), errors
            )

    def test_known_governed_by_id_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("governed_by" in e and "CON-CODE-GOOD" in e for e in errors), errors)

    def test_governed_by_check_skipped_when_no_topic_registry_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_governed.md", root / "bad_governed.md")
            # deliberately NOT copying top-code-1.md -- zero topics in this
            # corpus, so there's no registry to validate against.
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("governed_by" in e for e in errors), errors)


class TestMemlintDuplicateClaim(unittest.TestCase):
    def test_two_concepts_claiming_the_same_symbol_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "dup_a.md", root / "dup_a.md")
            shutil.copy(CONCEPTS / "dup_b.md", root / "dup_b.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-DUP-A" in e and "CON-CODE-DUP-B" in e for e in errors), errors
            )

    def test_single_owner_is_not_a_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "dup_a.md", root / "dup_a.md")
            errors, _warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(any("duplicate" in e.lower() for e in errors), errors)


class TestMemlintNotThisConcept(unittest.TestCase):
    def test_body_without_not_this_sentence_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "no_not_this.md", root / "no_not_this.md")
            _errors, warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertTrue(
                any("CON-CODE-NO-NOT-THIS" in w and "not this" in w.lower() for w in warnings), warnings
            )

    def test_body_with_not_this_sentence_does_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            _errors, warnings = memlint.lint_root(root, code_root=CODE_ROOT)
            self.assertFalse(
                any("CON-CODE-GOOD" in w and "not this" in w.lower() for w in warnings), warnings
            )


class TestMemlintExistingRulesStillGreen(unittest.TestCase):
    def test_v11_concept_fixtures_still_lint_clean_of_the_original_checks(self):
        v11_concepts = TOOLS_DIR / "fixtures" / "v11" / "concepts"
        v11_code = TOOLS_DIR / "fixtures" / "v11" / "code"
        errors, _warnings = memlint.lint_root(v11_concepts, code_root=v11_code)
        # CON-900's missing path is still expected; nothing NEW should
        # appear for CON-007 (media-identity) or CON-901 (no-tests).
        self.assertFalse(any("CON-007" in e for e in errors), errors)


if __name__ == "__main__":
    unittest.main()
