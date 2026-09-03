"""Tests for Anatomy's intent index: memidx.py's `code-reindex`/`code-search`
(a lexer-aware Swift chunker + a completely separate <project>-code.sqlite
index) and memlint.py's concept-record rule additions.

Owned by this session (per the concurrent-work split, see the repo-root
brief): memidx.py's code-index additions, memlint.py's concept-rule
additions, this file, and fixtures/code/. Does not touch hooks/, scripts/repo-init.sh,
tests/test_hooks.py, tests/test_write_hooks.py, or tests/test_repo_init.py.

Every python invocation for this project is
`PYTHONPATH= $MEMCONTINUUM_PYTHON` (a Windows numpy install leaks onto
PYTHONPATH by default in this shell and breaks fastembed under Linux Python
-- see the module import failing with `AttributeError: module 'os' has no
attribute 'add_dll_directory'` if that env var isn't cleared). This
machine's venv python is never hardcoded in tracked test code -- set
$MEMCONTINUUM_PYTHON in your own (untracked) shell environment before
running this file; see README.md "Requirements" / "Running the tests".
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import chunkers  # noqa: E402
import memidx  # noqa: E402
import memlint  # noqa: E402

FIXTURES = TOOLS_DIR / "fixtures" / "code"
PY_FIXTURES = TOOLS_DIR / "tests" / "fixtures" / "python_corpus"

# The real-corpus gold-probe test below runs against a PRIVATE code tree. Its
# corpus path, its probe queries and the qualified names it expects all
# describe that tree, so none of them may live in tracked content (privacy
# requirement, tests/test_repo_init.py TestNoMachineIdentifyingContent). They are
# read at run time from an untracked TSV instead; without it the test skips.
PROBE_FILE = Path(
    os.environ.get("MEMCONTINUUM_TEST_PROBES", TOOLS_DIR / "docs" / "internal" / "gold-probes.tsv")
)


def load_probe_set(path=PROBE_FILE):
    """-> (corpus_root, [(query, expected_qualified_name)], min_top1), or None
    when the untracked probe file is absent or unusable.

    Format. Header directives `#corpus:` and `#min_top1:` are BOTH required --
    defaulting either one is how a threshold quietly becomes vacuous (a missing
    min_top1 defaulting to 1) or a reindex quietly targets the working
    directory (an empty corpus resolving to Path(".")). First directive wins on
    a repeat, matching scripts/codanna-bench.sh's `sed | head -1`.

    Data rows are TAB-separated: query, qualified_name, negative_substr
    (bench only) and gold_flag. Every column is always present and "-" means
    none: an EMPTY column is unusable, because TAB is an IFS whitespace
    character and bash `read` collapses consecutive tabs, silently shifting a
    later column into an earlier variable. Only gold rows reach the assertion --
    the bench runs a wider probe list, and folding the extras in would let a
    hit on a bench-only probe mask a miss on a gold one against an absolute
    threshold.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    corpus, min_top1, probes = None, None, []
    corpus_seen = min_top1_seen = False
    for line in text.splitlines():
        if line.startswith("#corpus:"):
            # First directive wins EVEN when its value is empty -- otherwise
            # an empty first line lets a later one through here while the
            # bench's `sed | head -1` keeps the empty first, and the two
            # consumers disagree about the same file (round-3 finding 11).
            if not corpus_seen:
                corpus_seen = True
                value = line.split(":", 1)[1].strip()
                if value:
                    corpus = Path(value).expanduser()
        elif line.startswith("#min_top1:"):
            if not min_top1_seen:
                min_top1_seen = True
                try:
                    min_top1 = int(line.split(":", 1)[1].strip())
                except ValueError:
                    return None
                if min_top1 < 1:
                    # 0 would make the assertion vacuous.
                    return None
        elif line.startswith("#") or not line.strip():
            continue
        else:
            cols = line.split("\t")
            if len(cols) >= 2 and cols[0].strip() and cols[1].strip():
                is_gold = len(cols) >= 4 and cols[3].strip().lower() == "gold"
                if is_gold:
                    probes.append((cols[0].strip(), cols[1].strip()))
    if corpus is None or min_top1 is None or not probes:
        return None
    return corpus, probes, min_top1


def ns(**kw):
    base = dict(project=memidx.DEFAULT_PROJECT, db=None)
    base.update(kw)
    return SimpleNamespace(**base)


def code_reindex(code_root, db, project=memidx.DEFAULT_PROJECT, no_embed=True, full=False, lang="swift",
                  drop_root=None):
    """Task 7: `lang` now defaults to "swift" here in the test helper (not
    in memidx.py -- that hardcoded default is gone, see TestLangDefaultFix)
    purely to centralize the fix for the many pre-existing call sites in
    this file that relied on the old production default and always index
    Swift-only fixtures. Pass lang=None explicitly to exercise the real
    omitted-flag behavior (fails on a fresh project, reuses stored langs
    otherwise)."""
    args = ns(code_root=str(code_root), db=str(db), project=project, no_embed=no_embed, full=full, lang=lang,
              drop_root=drop_root)
    return memidx.cmd_code_reindex(args)


class TestCodeSchemaV2(unittest.TestCase):
    V1_DDL = """
      CREATE TABLE chunks (id INTEGER PRIMARY KEY, path TEXT, project TEXT, lang TEXT, kind TEXT,
        symbol TEXT, qualified_name TEXT, signature TEXT, doc TEXT, start_line INTEGER, end_line INTEGER);
      CREATE VIRTUAL TABLE fts USING fts5(qualified_name, split_tokens, signature, doc, body);
      CREATE TABLE embeddings (chunk_id INTEGER PRIMARY KEY, project TEXT, dim INTEGER, vector BLOB);
      CREATE TABLE file_sha (path TEXT, project TEXT, sha256 TEXT NOT NULL, mtime REAL, size INTEGER,
        gap_count INTEGER DEFAULT 0, chunker_version TEXT, PRIMARY KEY (path, project));
      CREATE TABLE code_meta (project TEXT PRIMARY KEY, code_root TEXT, langs TEXT, last_indexed_at REAL, head_sha TEXT);
      INSERT INTO chunks VALUES (1,'a.swift','p','swift','function','f','f','func f()','',1,2);
      INSERT INTO file_sha VALUES ('a.swift','p','x',0,0,0,'cv');
      INSERT INTO code_meta VALUES ('p','/old/root','swift,python',0,'abc');
    """

    def _cols(self, conn, table):
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}

    def _v1(self, td):
        db = Path(td) / "c.sqlite"
        conn = sqlite3.connect(str(db)); conn.executescript(self.V1_DDL); conn.commit(); conn.close()
        return db

    def test_fresh_db_has_v2_shape(self):
        with tempfile.TemporaryDirectory() as td:
            conn = memidx.open_code_db(Path(td) / "c.sqlite")
            self.assertEqual(memidx.code_schema_version(conn), 2)
            self.assertIn("code_root", self._cols(conn, "chunks"))
            self.assertTrue({"code_root", "status", "reason", "chunker_version"} <= self._cols(conn, "file_sha"))
            self.assertNotIn("langs", self._cols(conn, "code_meta"))
            self.assertEqual(self._cols(conn, "code_project"), {"project", "langs", "embedding_mode"})
            self.assertIn("attempt_key", self._cols(conn, "file_sha"))
            pk = sorted(r[1] for r in conn.execute("PRAGMA table_info(code_meta)") if r[5])
            self.assertEqual(pk, ["code_root", "project"])
            self.assertIn("tokenchars", conn.execute("SELECT sql FROM sqlite_master WHERE name='fts'").fetchone()[0])

    def test_older_db_keeps_roots_and_langs_but_drops_derived_rows(self):
        with tempfile.TemporaryDirectory() as td:
            conn = memidx.open_code_db(self._v1(td))
            self.assertEqual(memidx.code_schema_version(conn), 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM chunks").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM file_sha").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT project, code_root FROM code_meta").fetchall()[0][:], ("p", "/old/root"))
            self.assertEqual(conn.execute("SELECT langs, embedding_mode FROM code_project").fetchone()[:], ("swift,python", "none"))

    def test_failed_rebuild_leaves_older_db_intact(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._v1(td)
            real = memidx.CODE_SCHEMA_SQL
            memidx.CODE_SCHEMA_SQL = real + "\nCREATE TABLE code_schema (dup INTEGER);"   # forces an error mid-rebuild
            try:
                with self.assertRaises(sqlite3.Error):
                    memidx.open_code_db(db)
            finally:
                memidx.CODE_SCHEMA_SQL = real
            conn = sqlite3.connect(str(db))
            self.assertEqual(conn.execute("SELECT count(*) FROM chunks").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT langs FROM code_meta").fetchone()[0], "swift,python")

    def test_reopen_keeps_v2_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "c.sqlite"
            conn = memidx.open_code_db(db)
            conn.execute("INSERT INTO code_meta VALUES ('p','/r',0,NULL)"); conn.commit(); conn.close()
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT count(*) FROM code_meta").fetchone()[0], 1)


class TestMultiRootReindex(unittest.TestCase):
    def _mk(self, td):
        a = Path(td) / "a"; b = Path(td) / "b"; a.mkdir(); b.mkdir()
        (a / "main.py").write_text("def alpha():\n    return 1\n")
        (b / "main.py").write_text("def beta():\n    return 2\n")
        return a, b, Path(td) / "idx-code.sqlite"

    def test_two_roots_with_same_relative_path_coexist(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            self.assertEqual(code_reindex(a, db, lang="python"), 0)
            self.assertEqual(code_reindex(b, db, lang=None), 0)      # langs reused from code_project
            conn = memidx.open_code_db(db)
            rows = conn.execute("SELECT code_root, qualified_name FROM chunks ORDER BY code_root").fetchall()
            self.assertEqual([tuple(r) for r in rows], [(str(a.resolve()), "alpha"), (str(b.resolve()), "beta")])
            self.assertEqual(conn.execute("SELECT count(*) FROM code_meta").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT langs FROM code_project").fetchone()[0], "python")

    def test_reindexing_one_root_never_removes_the_other(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python"); code_reindex(b, db, lang="python")
            (a / "main.py").unlink()
            code_reindex(a, db, lang="python")
            conn = memidx.open_code_db(db)
            self.assertEqual([r[0] for r in conn.execute("SELECT qualified_name FROM chunks")], ["beta"])

    def test_missing_root_is_refused_and_nothing_is_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python")
            shutil.rmtree(a)
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = code_reindex(a, db, lang="python")
            self.assertEqual(rc, 2)
            self.assertIn("not a readable directory", buf.getvalue())
            self.assertIn("--drop-root", buf.getvalue())
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT count(*) FROM chunks").fetchone()[0], 1)

    def test_invalid_root_is_refused_before_any_db_is_touched(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "never-created.sqlite"
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(code_reindex(Path(td) / "missing", db, lang="python"), 2)
            self.assertFalse(db.exists(), "a refused root must not create the db")
            regular = Path(td) / "file.txt"; regular.write_text("x")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(code_reindex(regular, db, lang="python"), 2)
            self.assertFalse(db.exists())
            # an older-schema db must not be rebuilt by a refused run
            v1 = Path(td) / "v1.sqlite"
            conn = sqlite3.connect(str(v1)); conn.executescript(TestCodeSchemaV2.V1_DDL); conn.commit(); conn.close()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(code_reindex(Path(td) / "missing", v1, lang="python"), 2)
            conn = sqlite3.connect(str(v1))
            self.assertEqual(conn.execute("SELECT count(*) FROM chunks").fetchone()[0], 1)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='code_schema'").fetchone())
            if os.geteuid() != 0:
                unreadable = Path(td) / "noperm"; unreadable.mkdir(); unreadable.chmod(0)
                try:
                    with contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(code_reindex(unreadable, db, lang="python"), 2)
                finally:
                    unreadable.chmod(0o755)
                self.assertFalse(db.exists())

    def test_drop_root_removes_only_that_root(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python"); code_reindex(b, db, lang="python")
            rc = memidx.cmd_code_reindex(ns(code_root=None, drop_root=str(a), db=str(db),
                                           project=memidx.DEFAULT_PROJECT, no_embed=True, full=False, lang=None))
            self.assertEqual(rc, 0)
            conn = memidx.open_code_db(db)
            self.assertEqual([r[0] for r in conn.execute("SELECT qualified_name FROM chunks")], ["beta"])
            self.assertEqual(conn.execute("SELECT count(*) FROM code_meta").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM file_sha").fetchone()[0], 1)

    def test_lang_superset_is_additive_but_removal_needs_full(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python")
            self.assertEqual(code_reindex(b, db, lang="python,swift"), 0)        # superset: --add-lang's shape
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT langs FROM code_project").fetchone()[0], "python,swift"); conn.close()
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                self.assertEqual(code_reindex(b, db, lang="swift"), 1)              # drops python
            self.assertIn("drops python", buf.getvalue())
            self.assertEqual(code_reindex(b, db, lang="swift", full=True), 0)
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT langs FROM code_project").fetchone()[0], "swift")

    def _coverage(self, db):
        conn = memidx.open_code_db(db)
        mode = conn.execute("SELECT embedding_mode FROM code_project").fetchone()[0]
        chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
        embedded = conn.execute("SELECT count(*) FROM chunks c JOIN embeddings e ON e.chunk_id=c.id").fetchone()[0]
        conn.close()
        return mode, chunks, embedded

    def test_embedding_mode_tracks_real_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python", no_embed=True)
            self.assertEqual(self._coverage(db)[0], "none")
            code_reindex(a, db, lang="python", no_embed=False)
            mode, chunks, embedded = self._coverage(db)
            self.assertEqual((mode, chunks, embedded), ("full", 1, 1))
            code_reindex(a, db, lang="python", no_embed=True)               # nothing changed -> stays full
            self.assertEqual(self._coverage(db), ("full", 1, 1))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code_reindex(b, db, lang="python", no_embed=True)           # adds chunks without vectors
            mode, chunks, embedded = self._coverage(db)
            self.assertEqual(mode, "none"); self.assertEqual(chunks, 2); self.assertEqual(embedded, 1)
            self.assertIn("embedding mode set to none", buf.getvalue())
            code_reindex(b, db, lang="python", no_embed=False)              # restores coverage
            self.assertEqual(self._coverage(db), ("full", 2, 2))

    def test_full_mode_means_every_chunk_has_a_vector(self):
        """The invariant behind the mode: whenever code_project says full,
        chunks and embeddings agree 1:1 (change a file with --no-embed on a
        full project -> mode none; with embeddings -> still full)."""
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python", no_embed=False)
            (a / "main.py").write_text("def alpha2():\n    return 1\n")
            with contextlib.redirect_stdout(io.StringIO()):
                code_reindex(a, db, lang="python", no_embed=True)
            mode, chunks, embedded = self._coverage(db)
            self.assertEqual(mode, "none"); self.assertEqual(embedded, 0)

    def test_search_output_is_root_qualified_with_two_roots(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python"); code_reindex(b, db, lang="python")
            script = ("import sys; sys.path.insert(0, %r); import memidx; "
                      "memidx.main(['code-search', '--db', %r, 'beta', '--mode', 'fts', '--no-heal'])"
                      ) % (str(TOOLS_DIR), str(db))
            r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
            self.assertIn(f"{b.resolve()}/main.py:1", r.stdout)

    def test_json_hit_carries_code_root(self):
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python"); code_reindex(b, db, lang="python")
            script = ("import sys; sys.path.insert(0, %r); import memidx; "
                      "memidx.main(['code-search', '--db', %r, 'beta', '--mode', 'fts', '--no-heal', '--json'])"
                      ) % (str(TOOLS_DIR), str(db))
            r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
            env = json.loads(r.stdout)
            self.assertTrue(env["results"], env)
            self.assertEqual(env["results"][0]["code_root"], str(b.resolve()))


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
        self.assertEqual(names["Cache.computed"]["kind"], "accessor")


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
        self.assertEqual(by_name["Vec.init"]["kind"], "constructor")

    def test_subscript_has_no_own_name_but_chunks(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Vec.subscript", by_name)
        self.assertEqual(by_name["Vec.subscript"]["kind"], "accessor")

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


class TestChunkerActorObserversExtensionBacktick(unittest.TestCase):
    """Finding 6: actor as a container (qualifies like class/struct/enum/
    protocol/extension, never a chunk itself), willSet/didSet observers on
    a stored property are NOT computed-var chunks, `extension Outer.Inner`
    keeps its dotted qualifier, and backtick-quoted names are captured."""

    def setUp(self):
        self.text = (FIXTURES / "ChunkerCases.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_actor_qualifies_its_members_and_is_never_a_chunk_itself(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Counter.increment", names)
        kinds = {c["kind"] for c in self.chunks}
        self.assertNotIn("actor", kinds)
        symbols = {c["symbol"] for c in self.chunks}
        self.assertNotIn("Counter", symbols)

    def test_willset_didset_observers_are_not_a_computed_var_chunk(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertNotIn("Observed.value", names)

    def test_extension_of_a_nested_type_keeps_the_dotted_qualifier(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Outer.Inner.nested", names)

    def test_backtick_quoted_func_and_var_names_are_captured(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("Escaped.default", by_name)
        self.assertEqual(by_name["Escaped.default"]["kind"], "method")
        self.assertIn("Escaped.type", by_name)
        self.assertEqual(by_name["Escaped.type"]["kind"], "accessor")


class TestChunkerGapResyncModifiers(unittest.TestCase):
    """Finding 6: gap-resync must also recognise `package`/`consuming`/
    `borrowing` func modifiers and bare `var`/`subscript` declarations as
    valid resync anchors -- previously the heuristic only knew func/init/
    class/struct/enum/protocol/extension (with a narrower modifier set),
    so a member using this vocabulary right after a desync used to be
    swallowed into the same gap instead of being recovered."""

    def setUp(self):
        self.text = (FIXTURES / "GapResyncModifiers.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_exactly_five_gaps_reported(self):
        self.assertEqual(len(self.gaps), 5, self.gaps)

    def test_package_modifier_recovered_after_gap(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("afterPackageGap", names)

    def test_consuming_modifier_recovered_after_gap(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("afterConsumingGap", names)

    def test_borrowing_modifier_recovered_after_gap(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("afterBorrowingGap", names)

    def test_bare_var_recovered_after_gap(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("afterVarGap", by_name)
        self.assertEqual(by_name["afterVarGap"]["kind"], "accessor")

    def test_bare_subscript_recovered_after_gap(self):
        by_name = {c["qualified_name"]: c for c in self.chunks}
        self.assertIn("subscript", by_name)
        self.assertEqual(by_name["subscript"]["kind"], "accessor")


class TestChunkerClassMemberAndBacktickContainer(unittest.TestCase):
    """Finding 3: `class subscript` (and other `class <modifier> func/var/
    subscript` member forms) is a class-level MEMBER, not a phantom type
    container -- only `class func`/`class var` used to be excluded. A
    backtick-quoted extension target must still be kept as a real
    container, including when it is itself a dotted, backtick-quoted
    nested type."""

    def setUp(self):
        self.text = (FIXTURES / "ClassMemberAndBacktickContainer.swift").read_text()
        self.chunks, self.gaps = memidx.chunk_source(self.text)

    def test_no_gaps(self):
        self.assertEqual(self.gaps, [])

    def test_class_subscript_qualifies_under_box_not_under_a_phantom_subscript_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Box.subscript", names)
        self.assertNotIn("Box.subscript.subscript", names)

    def test_class_subscript_is_not_a_type_container(self):
        symbols = {c["symbol"] for c in self.chunks}
        # a phantom "subscript" container would make "make" qualify as
        # Box.subscript.make instead of Box.make.
        self.assertIn("Box.make", {c["qualified_name"] for c in self.chunks})

    def test_class_final_func_member_form_is_not_a_phantom_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Box.make", names)
        self.assertNotIn("Box.final.make", names)
        symbols = {c["symbol"] for c in self.chunks}
        self.assertNotIn("final", symbols)

    def test_backtick_extension_target_keeps_its_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Type.plain", names)

    def test_backtick_dotted_nested_extension_target_keeps_its_container(self):
        names = {c["qualified_name"] for c in self.chunks}
        self.assertIn("Type.Inner.nested", names)


# ---------------------------------------------------------------------------
# 2. code-reindex / code-search (DB-backed)
# ---------------------------------------------------------------------------


class TestIncremental(unittest.TestCase):
    def test_unchanged_file_gives_zero_reembeds(self):
        """Finding 8: despite its name, this test used to run with
        embeddings OFF both times (no_embed=True) and only checked that
        the chunk COUNT was unchanged -- it never actually asserted a
        re-embed count, so it couldn't tell "0 reembeds because nothing
        changed" apart from "0 reembeds because embedding never ran at
        all". Turn embeddings ON (no_embed=False) for this small (3-func)
        fixture and assert the real re-embed count from code-reindex's own
        summary line: > 0 on the first run, exactly 0 on the second
        (sha-skip, file byte-for-byte unchanged)."""
        import contextlib
        import io
        import re

        reembed_re = re.compile(r"(\d+) chunk\(s\) \(re-\)embedded")

        def reembed_count(root, db):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=False)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            m = reembed_re.search(out)
            self.assertIsNotNone(m, out)
            return int(m.group(1))

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            first = reembed_count(root, db)
            self.assertGreater(first, 0, "first-ever index of a new file must embed its chunks")

            second = reembed_count(root, db)
            self.assertEqual(second, 0, "file byte-for-byte unchanged -- sha-skip must re-embed nothing")

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


class TestChunkerVersionSkipDecision(unittest.TestCase):
    """Task 3: chunker_version joins the sha-only skip decision, so a
    backend behavior change (impl_version bump) forces a re-chunk on the
    next code-reindex even though no source byte moved -- the stale-chunk
    one-way-door the brief closes."""

    @staticmethod
    def _summary_counts(output: str):
        m = re.search(
            r"(\d+) files scanned, (\d+) added, (\d+) changed, (\d+) unchanged",
            output,
        )
        assert m is not None, output
        return {
            "scanned": int(m.group(1)),
            "added": int(m.group(2)),
            "changed": int(m.group(3)),
            "unchanged": int(m.group(4)),
        }

    def _reindex_summary(self, root, db):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = code_reindex(root, db, no_embed=True)
        self.assertEqual(rc, 0)
        return self._summary_counts(buf.getvalue())

    def test_chunker_version_bump_forces_rechunk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            first = self._reindex_summary(root, db)
            self.assertEqual(first["added"], 1)

            second = self._reindex_summary(root, db)
            self.assertEqual(second["changed"], 0)
            self.assertEqual(second["unchanged"], 1)

            original_version = chunkers.LANGUAGE_TABLE["swift"]["impl_version"]
            chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = original_version + "-bumped"
            try:
                third = self._reindex_summary(root, db)
            finally:
                chunkers.LANGUAGE_TABLE["swift"]["impl_version"] = original_version

            self.assertGreater(third["changed"], 0, "impl_version bump must force a re-chunk")
            self.assertEqual(third["unchanged"], 0, "no file may be reported unchanged after a version bump")

    def test_pre_stamp_rows_rechunk_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            code_reindex(root, db, no_embed=True)

            conn = memidx.open_code_db(db)
            conn.execute("UPDATE file_sha SET chunker_version = NULL")
            conn.commit()
            conn.close()

            summary = self._reindex_summary(root, db)
            self.assertGreater(summary["changed"], 0, "a NULL (pre-stamp) chunker_version must force one re-chunk")
            self.assertEqual(summary["unchanged"], 0)

            conn = memidx.open_code_db(db)
            row = conn.execute(
                "SELECT chunker_version FROM file_sha WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()
            conn.close()
            self.assertEqual(row["chunker_version"], chunkers.chunker_version("swift"))

            # Re-running now must be a clean skip: the row is stamped.
            summary2 = self._reindex_summary(root, db)
            self.assertEqual(summary2["changed"], 0)
            self.assertEqual(summary2["unchanged"], 1)


class TestRegistryDispatchMixedCorpus(unittest.TestCase):
    """Task 5: code-reindex dispatches per-file through
    chunkers.get_chunker(lang).chunk_file instead of hardcoding the Swift
    walker -- a --lang swift,python run over a corpus containing both must
    index both, each chunk row carrying its own backend's `lang`."""

    def test_mixed_swift_and_python_corpus_indexes_both_langs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="swift,python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            rows = conn.execute("SELECT lang, qualified_name FROM chunks").fetchall()
            conn.close()

            by_lang: dict = {}
            for r in rows:
                by_lang.setdefault(r["lang"], set()).add(r["qualified_name"])
            self.assertEqual(set(by_lang.keys()), {"swift", "python"})
            self.assertGreater(len(by_lang["swift"]), 0)
            self.assertIn("plain_function", by_lang["python"])


class TestPerLanguageSkipDirs(unittest.TestCase):
    """Task 7: CODE_SKIP_DIR_NAMES is now the GLOBAL set (.git, vendor,
    node_modules) only -- language-specific noise dirs (python's venv/
    .venv/__pycache__/build/dist/.tox/.eggs) live on the LANGUAGE_TABLE row
    instead, so a Swift-only project never has its own `build/` or `dist/`
    output pruned by a rule meant for Python virtualenvs."""

    def test_python_venv_dir_is_pruned_and_not_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            venv_pkg = root / "venv" / "lib"
            venv_pkg.mkdir(parents=True)
            (venv_pkg / "vendored.py").write_text("def vendored_function():\n    return 1\n")
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            self.assertIn("basic_functions.py", paths)
            self.assertFalse(
                any("venv" in Path(p).parts for p in paths),
                f"venv/ must be pruned for a python-wired walk, got paths: {paths}",
            )

    def test_dunder_pycache_dir_is_pruned_and_not_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            cache_dir = root / "__pycache__"
            cache_dir.mkdir()
            (cache_dir / "stray.py").write_text("def stray():\n    return 1\n")
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            self.assertFalse(any("__pycache__" in Path(p).parts for p in paths))

    def test_stray_lang_with_no_language_table_row_is_rejected_not_ignored(self):
        """SUPERSEDED CONTRACT (C1/C2, final fix wave). This test used to
        assert that a --lang value outside LANGUAGE_TABLE was tolerated
        silently (rc 0, zero files matched). The Codex gate ruled that
        wrong: a typo'd language is a typo, and "matched nothing, said
        nothing" is exactly the silent blind spot this milestone exists to
        close. code-reindex now validates every resolved language name and
        fails loudly instead -- see TestCodeReindexLangValidation for the
        full contract; this case is kept here because the directory-pruning
        code is what used to have to tolerate it."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc = code_reindex(root, db, no_embed=True, lang="swift,ts")
            self.assertNotEqual(rc, 0)
            self.assertIn("ts", err_buf.getvalue())


class TestSkipDirOwnLanguageRule(unittest.TestCase):
    """C1 (final fix wave, supersedes Task 7's Step 1(b) union rule, which
    the Codex gate flagged as silent data loss): a language's skip_dirs
    prune ONLY that language's OWN files. The walk still prunes the global
    noise dirs plus the INTERSECTION of every wired language's skip sets
    (pure optimization -- a dir every wired lang would drop anyway can be
    skipped wholesale); every other directory is walked, and an individual
    file is dropped iff one of its ancestor directory names (relative to
    the code root) is in ITS OWN language's skip set.

    Concretely, with swift and python both wired: swift's skip_dirs name
    "Tests", python's do not, so `Tests/basic_functions.py` IS indexed
    while `Tests/NestedTypes.swift` is NOT. python's own "venv" still
    drops `venv/*.py` either way."""

    @staticmethod
    def _make_corpus(root):
        shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
        tests_dir = root / "Tests"
        tests_dir.mkdir()
        shutil.copy(PY_FIXTURES / "basic_functions.py", tests_dir / "basic_functions.py")
        shutil.copy(FIXTURES / "NestedTypes.swift", tests_dir / "NestedTypes.swift")

    def test_tests_dir_python_file_indexed_when_swift_wired_alongside(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            self._make_corpus(root)
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="swift,python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            tests_py = str(Path("Tests") / "basic_functions.py")
            self.assertIn(
                tests_py, paths,
                "C1: 'Tests' is in swift's skip set, not python's -- it may only "
                f"drop swift files there, never python ones. Got: {paths}",
            )

    def test_tests_dir_swift_file_still_dropped_when_swift_wired_alongside(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            self._make_corpus(root)
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="swift,python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            tests_swift = str(Path("Tests") / "NestedTypes.swift")
            self.assertNotIn(
                tests_swift, paths,
                "C1: 'Tests' IS in swift's own skip set -- a .swift file under it "
                f"stays dropped. Got: {paths}",
            )
            self.assertIn("NestedTypes.swift", paths)

    def test_code_root_named_Tests_does_not_drop_everything(self):
        """The ancestor check is relative to the code root: a project whose
        root directory is itself called `Tests` must still index its files
        (the root's own name is not an ancestor name inside the walk)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "Tests"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="swift")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            self.assertIn("NestedTypes.swift", paths)

    def test_own_language_skipped_file_is_not_tallied_as_unsupported(self):
        """A file dropped by its OWN language's skip set is noise the walk
        deliberately prunes -- it must not show up on the end-of-run
        "unsupported/unwired extensions" census line, which is about
        extensions this install cannot chunk at all."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            self._make_corpus(root)
            db = Path(td) / "idx-code.sqlite"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True, lang="swift,python")
            self.assertEqual(rc, 0)
            self.assertNotIn("unsupported/unwired", buf.getvalue())

    def test_tests_dir_python_file_indexed_when_python_wired_alone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            self._make_corpus(root)
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            tests_py = str(Path("Tests") / "basic_functions.py")
            self.assertIn(
                tests_py, paths,
                "python's own skip_dirs never include Tests/ -- it is indexed "
                "whether or not swift happens to be wired alongside (C1)",
            )


class TestExtensionlessShebangIndexing(unittest.TestCase):
    """B5 (final fix wave): the census PROMISES an extensionless
    `#!/usr/bin/env python3` script under the python row, so the reindex
    walk must actually index it when python is wired -- otherwise the
    census proposes a language on the strength of files the indexer then
    silently ignores. Extensionless files with no recognized shebang are
    tallied on the skipped census line under NO_EXTENSION_BUCKET rather
    than vanishing (the same gap, on the other surface)."""

    def test_shebang_only_extensionless_file_is_indexed_under_python(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            script = root / "run-migrations"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "def migrate_everything():\n"
                "    return 1\n"
            )
            script.chmod(0o755)
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            symbols = {r["symbol"] for r in conn.execute("SELECT symbol FROM chunks")}
            sha_row = conn.execute(
                "SELECT chunker_version FROM file_sha WHERE path=?", ("run-migrations",)
            ).fetchone()
            conn.close()
            self.assertEqual(paths, {"run-migrations"})
            self.assertIn("migrate_everything", symbols)
            self.assertEqual(
                sha_row["chunker_version"], chunkers.chunker_version("python"),
                "a shebang-resolved file must be stamped with its real chunker "
                "version, not the 'unversioned' fail-open label",
            )

    def test_shebang_file_not_indexed_when_its_language_is_not_wired(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            (root / "deploy").write_text("#!/usr/bin/env python3\ndef go():\n    return 1\n")
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="swift")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            self.assertEqual(paths, {"NestedTypes.swift"})

    def test_extensionless_without_shebang_appears_on_the_skipped_census_line(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            (root / "NOTES").write_text("no shebang, just prose\n")
            db = Path(td) / "idx-code.sqlite"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True, lang="swift")
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn(memidx.NO_EXTENSION_BUCKET + "=1", out, out)


class TestLangDefaultFix(unittest.TestCase):
    """Task 7: the old hardcoded `"swift"` --lang default is gone.
    Omitted --lang: reuse code_meta.langs for the project if a prior
    reindex stored one, else fail loudly (first reindex for a project
    MUST name its languages explicitly) rather than silently default to
    Swift-only, which used to index nothing at all for a python-only
    project set up without --lang and never say why."""

    def test_first_reindex_without_lang_on_fresh_project_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = code_reindex(root, db, no_embed=True, lang=None, project="freshproj")
            self.assertNotEqual(rc, 0)
            self.assertIn("--lang required on first code-reindex", err.getvalue())

            conn = memidx.open_code_db(db)
            row = conn.execute(
                "SELECT * FROM code_meta WHERE project=?", ("freshproj",)
            ).fetchone()
            conn.close()
            self.assertIsNone(row, "a refused first reindex must not stamp code_meta")

    def test_second_reindex_without_lang_reuses_stored_langs_not_swift_default(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="python", project="reuseproj")
            self.assertEqual(rc, 0)

            # A swift file lands in the corpus AFTER the python-only first
            # reindex -- if the omitted --lang on the next run silently fell
            # back to the old hardcoded "swift" default (or wired both),
            # this would wire it in. It must not: the STORED set ("python")
            # is what gets reused.
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True, lang=None, project="reuseproj")
            self.assertEqual(rc, 0, buf.getvalue())

            conn = memidx.open_code_db(db)
            langs_row = conn.execute(
                "SELECT langs FROM code_project WHERE project=?", ("reuseproj",)
            ).fetchone()
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            self.assertEqual(langs_row["langs"], "python")
            self.assertIn("basic_functions.py", paths)
            self.assertNotIn("NestedTypes.swift", paths)
            self.assertIn(
                "code-reindex: 1 files with unsupported/unwired extensions not indexed: .swift=1",
                buf.getvalue(),
            )


class TestCodeCensus(unittest.TestCase):
    """Task 8: memidx.py `code-census` -- discovery-only extension+shebang
    census, three-way classification (spec §3, DESIGN-anatomy-chunkers.md:
    extension-supported / extension-unsupported / shebang-sniffed
    extensionless). No DB, no --project, no consent recorded; exit 0
    always -- census never fails a scan it can walk."""

    @staticmethod
    def _make_corpus(root):
        shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
        shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
        (root / "Program.cs").write_text("class Program {}\n")
        script = root / "run-migrations"
        script.write_text("#!/usr/bin/env python3\nprint('hi')\n")
        script.chmod(0o755)
        return script

    def test_json_shape_classifies_extension_and_shebang_together(self):
        """Brief Step 1 corpus: .py, .swift, .cs, plus an extensionless
        `#!/usr/bin/env python3` script -- python's count includes BOTH the
        real .py file and the shebang script (corpus-count(+1 shebang)),
        .cs is unsupported."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            self._make_corpus(root)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["code-census", "--root", str(root), "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())

            self.assertEqual(data["python"], {"files": 2, "status": "supported"})
            self.assertEqual(data["swift"], {"files": 1, "status": "supported"})
            self.assertEqual(data[".cs"], {"files": 1, "status": "unsupported"})

    def test_compound_extension_keys_by_its_full_suffix(self):
        """I2 (final fix wave): code_census must key `.blade.php`/`.d.ts`/
        `.min.js` files by the COMPOUND suffix, not by the parent single
        suffix -- `foo.blade.php` counted as `.php` misrepresents what the
        tree actually holds, and lang_for_path already refuses to treat the
        two as the same thing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "foo.blade.php").write_text("<div></div>\n")
            (root / "bar.php").write_text("<?php ?>\n")
            (root / "types.d.ts").write_text("declare const x: number;\n")

            counts = memidx.code_census(root)
            self.assertEqual(counts[".blade.php"], {"files": 1, "status": "unsupported"})
            self.assertEqual(counts[".php"], {"files": 1, "status": "unsupported"})
            self.assertEqual(counts[".d.ts"], {"files": 1, "status": "unsupported"})

    def test_json_lists_zero_count_rows_for_every_known_language(self):
        """C5 (final fix wave): the driven install flow reads
        "supported but not found" straight out of the --json census, so
        every LANGUAGE_TABLE language absent from the tree must still get a
        zero-count supported row rather than simply being missing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["code-census", "--root", str(root), "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual(data["swift"], {"files": 0, "status": "supported"})
            self.assertEqual(data["python"], {"files": 1, "status": "supported"})
            for lang in chunkers.LANGUAGE_TABLE:
                self.assertIn(lang, data)

    def test_human_table_lists_a_not_found_language_under_supported_with_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["code-census", "--root", str(root)])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("swift: 0", out, out)
            self.assertIn("python: 1", out, out)

    def test_extensionless_shebang_script_counted_under_its_language(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "deploy").write_text("#!/usr/bin/env python3\nprint('x')\n")

            counts = memidx.code_census(root)
            self.assertEqual(counts["python"], {"files": 1, "status": "supported"})

    def test_extensionless_without_recognized_shebang_falls_to_no_extension_bucket(self):
        """Controller-scope addition #1 (Task 5 reviewer, "second silent
        gap"): an extensionless file with NO recognized shebang -- whether
        no shebang at all, or a real shebang for a language not in the
        table -- is counted under NO_EXTENSION_BUCKET as unsupported, never
        silently dropped from the census entirely."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "README").write_text("just some notes, no shebang here\n")
            (root / "run-it").write_text("#!/bin/bash\necho hi\n")

            counts = memidx.code_census(root)
            self.assertEqual(
                counts[memidx.NO_EXTENSION_BUCKET],
                {"files": 2, "status": "unsupported"},
            )
            # C5: every known language still gets a row, at zero.
            self.assertEqual(counts["python"], {"files": 0, "status": "supported"})
            self.assertEqual(counts["swift"], {"files": 0, "status": "supported"})

    def test_universal_skip_dirs_pruned_and_language_skip_dirs_scoped_to_own_language(self):
        """Controller-scope addition #2, moved to the D7-as-reconciled
        contract (Ruling 17): census walks with ONLY the universal noise
        set pruned (CODE_SKIP_DIR_NAMES == chunkers.UNIVERSAL_SKIP_DIRS) --
        not a union of every LANGUAGE_TABLE row's skip_dirs. `.venv/`
        (universal noise) is never counted for any language. `Tests/`
        (swift's own skip_dir, not python's) drops the swift file it holds
        but NOT a python file in the very same directory -- the union rule
        this superseded would have dropped both, silently losing real
        python source to swift's noise rule."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            venv_dir = root / ".venv" / "lib"
            venv_dir.mkdir(parents=True)
            shutil.copy(PY_FIXTURES / "basic_functions.py", venv_dir / "vendored.py")
            tests_dir = root / "Tests"
            tests_dir.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", tests_dir / "NestedTypes.swift")
            shutil.copy(PY_FIXTURES / "basic_functions.py", tests_dir / "also_python.py")
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")

            counts = memidx.code_census(root)
            # basic_functions.py at root + also_python.py under Tests/ --
            # python has no "Tests" skip_dir, so both count.
            self.assertEqual(counts["python"], {"files": 2, "status": "supported"})
            # NestedTypes.swift under Tests/ -- swift's own skip_dir drops
            # it; vendored.py under .venv/ never reaches a language check
            # at all (universal noise, pruned before the walk descends).
            self.assertEqual(counts["swift"], {"files": 0, "status": "supported"})

    def test_exit_code_is_always_zero_even_on_a_nonexistent_root(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does-not-exist"
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["code-census", "--root", str(missing), "--json"])
            self.assertEqual(rc, 0)
            # C5: a walk that found nothing still names every known
            # language at zero -- "supported but not found", not silence.
            self.assertEqual(
                json.loads(buf.getvalue()),
                {lang: {"files": 0, "status": "supported"}
                 for lang in chunkers.LANGUAGE_TABLE},
            )

    def test_human_readable_table_groups_supported_before_unsupported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            self._make_corpus(root)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["code-census", "--root", str(root)])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertLess(out.index("supported:"), out.index("unsupported:"))
            self.assertIn("python: 2", out)
            self.assertIn(".cs: 1", out)


class TestCensusSkipRule(unittest.TestCase):
    """D7 as reconciled (Ruling 17, "your call"): the census prunes
    chunkers.UNIVERSAL_SKIP_DIRS only -- the same universal noise set
    iter_code_source_files prunes. A SUPPORTED file is then dropped iff an
    ancestor directory on its root-relative path sits in ITS OWN
    language's skip_dirs (chunkers.path_is_skipped_for_lang, the same
    per-file test the indexer's walk already uses) -- a union rule would
    have hidden a real `Tests/x.cs` file behind swift's "Tests" skip_dir
    even though `.cs` has no language of its own to skip anything for. An
    UNSUPPORTED extension is always counted, in any directory that is not
    universal noise -- it has no language, so it has no skip_dirs to be
    dropped by."""

    def test_per_file_language_skip_and_unsupported_counted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Tests").mkdir()
            (root / "node_modules").mkdir()
            # python has no "Tests" skip_dir -> counted
            (root / "Tests" / "t.py").write_text("def t():\n    pass\n")
            # swift's own skip_dir names "Tests" -> not counted
            (root / "Tests" / "T.swift").write_text("func t() {}\n")
            # unsupported extension, no language to skip it -> always counted
            (root / "Tests" / "x.cs").write_text("class X {}\n")
            # node_modules is universal noise -> pruned regardless of language
            (root / "node_modules" / "m.py").write_text("")

            counts = memidx.code_census(root)
            self.assertEqual(counts["python"], {"files": 1, "status": "supported"})
            self.assertEqual(counts["swift"], {"files": 0, "status": "supported"})
            self.assertEqual(counts[".cs"], {"files": 1, "status": "unsupported"})


class TestUnsupportedExtensionCensus(unittest.TestCase):
    """Task 5 spec S4: a file whose extension is not in the wired lang set
    must not be silently dropped -- code-reindex prints exactly one summary
    line, sorted by count desc, and never crashes on it (INC-0103/0104: no
    growing blind spot may be silent)."""

    def test_rust_file_produces_census_line_and_does_not_crash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            (root / "lib.rs").write_text("fn main() {}\n")
            db = Path(td) / "idx-code.sqlite"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn(
                "code-reindex: 1 files with unsupported/unwired extensions not indexed: .rs=1",
                out,
                out,
            )

    def test_compound_extension_is_tallied_under_its_full_suffix(self):
        """I2 (final fix wave): a `.blade.php` file must not be tallied as a
        plain `.php` one -- lang_for_path already treats the compound as its
        own thing (COMPOUND_EXCLUDES), so the census line must agree."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            (root / "foo.blade.php").write_text("<div></div>\n")
            (root / "bar.php").write_text("<?php ?>\n")
            db = Path(td) / "idx-code.sqlite"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn(".blade.php=1", out, out)
            self.assertIn(".php=1", out, out)

    def test_no_census_line_when_nothing_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)
            self.assertNotIn("unsupported/unwired", buf.getvalue())


class TestChunkerFailOpenPerFile(unittest.TestCase):
    """Task 5 spec S4: ANY exception escaping a chunker backend, or a
    ChunkResult with status "failed", must not crash the reindex -- warn
    (naming the file) and keep going. Task 3 (Anatomy M2a) refines WHAT
    gets written for the skipped file: a non-deterministic exception (a
    RuntimeError, here) lands `not-indexed` (sha NULL, retried on the next
    run or when the backend/chunker changes); a deterministic ChunkResult
    status="failed" lands `failed` (sha stored, retried only on a source
    edit or --full) -- either way the row stays so repair provenance is
    visible, it is just never "ok"."""

    def test_raising_chunker_is_skipped_not_crashed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            original = chunkers.swift.chunk_file

            def boom(text, rel):
                raise RuntimeError("simulated backend crash")

            chunkers.swift.chunk_file = boom
            try:
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    rc = code_reindex(root, db, no_embed=True)
            finally:
                chunkers.swift.chunk_file = original

            self.assertEqual(rc, 0)
            self.assertIn("NestedTypes.swift", err_buf.getvalue())

            conn = memidx.open_code_db(db)
            row = conn.execute(
                "SELECT * FROM file_sha WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()
            chunk_count = conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()["c"]
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "not-indexed")
            self.assertIsNone(row["sha256"])
            self.assertEqual(chunk_count, 0)

    def test_failed_status_result_is_skipped_not_crashed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "broken.py", root / "broken.py")
            db = Path(td) / "idx-code.sqlite"

            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0)
            self.assertIn("broken.py", err_buf.getvalue())

            conn = memidx.open_code_db(db)
            row = conn.execute(
                "SELECT * FROM file_sha WHERE path=?", ("broken.py",)
            ).fetchone()
            conn.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["sha256"])


class TestFailOpenDeletesStaleIndexState(unittest.TestCase):
    """B1 (final fix wave, Grok HIGH 1): "fail open" must not mean "keep
    serving what the last good run stored". When a file that WAS indexed
    later fails to chunk, its old chunks and its file_sha row both have to
    go -- otherwise code-search keeps answering from rows whose source
    text no longer produces them, and the index reports itself "current"
    while carrying content nothing on disk backs."""

    @staticmethod
    def _raise_on_swift():
        def boom(text, rel):
            raise RuntimeError("simulated backend crash")
        return boom

    def _index_then_break(self, td, *, bump_version=False):
        root = Path(td) / "code"
        root.mkdir()
        target = root / "NestedTypes.swift"
        shutil.copy(FIXTURES / "NestedTypes.swift", target)
        db = Path(td) / "idx-code.sqlite"

        rc = code_reindex(root, db, no_embed=True)
        self.assertEqual(rc, 0)
        conn = memidx.open_code_db(db)
        first_count = conn.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE path=?", ("NestedTypes.swift",)
        ).fetchone()["c"]
        conn.close()
        self.assertGreater(first_count, 0, "setup: the first pass must index something")

        if bump_version:
            row = dict(chunkers.LANGUAGE_TABLE["swift"])
            row["impl_version"] = str(int(row["impl_version"]) + 100)
            self._prev_row = chunkers.LANGUAGE_TABLE["swift"]
            chunkers.LANGUAGE_TABLE["swift"] = row
        else:
            target.write_text(target.read_text() + "\nfunc addedLater() {}\n")

        original = chunkers.swift.chunk_file
        chunkers.swift.chunk_file = self._raise_on_swift()
        try:
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = code_reindex(root, db, no_embed=True)
        finally:
            chunkers.swift.chunk_file = original
        return root, db, rc, err_buf.getvalue()

    def test_content_change_then_failure_purges_chunks_and_file_sha(self):
        """Task 3: a RuntimeError escaping the backend is not a
        deterministic (source-code) failure -- it lands `not-indexed`, not
        `failed`. The chunks/fts purge is unchanged; what changed is that
        the file_sha row now STAYS (status="not-indexed", sha NULL) instead
        of being deleted, so provenance survives the failure."""
        with tempfile.TemporaryDirectory() as td:
            _root, db, rc, err = self._index_then_break(td)
            self.assertEqual(rc, 0)
            self.assertIn("NestedTypes.swift", err)

            conn = memidx.open_code_db(db)
            chunk_count = conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()["c"]
            sha_row = conn.execute(
                "SELECT * FROM file_sha WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()
            fts_count = conn.execute("SELECT COUNT(*) AS c FROM fts").fetchone()["c"]
            conn.close()
            self.assertEqual(chunk_count, 0, "stale chunks from the last good run must be deleted")
            self.assertIsNotNone(sha_row, "the file_sha row stays -- with a not-indexed status")
            self.assertEqual(sha_row["status"], "not-indexed")
            self.assertIsNone(sha_row["sha256"])
            self.assertEqual(fts_count, 0, "the fts shadow rows must go with the chunks")

    def test_version_bump_then_failure_leaves_the_index_not_current(self):
        """Task 3: same not-indexed contract as above; the row's presence
        no longer by itself makes `code_index_report` honest, so the state
        check consults `status` too (a not-indexed/failed row can never
        read "current", whatever its stored mtime/size/chunker_version)."""
        try:
            with tempfile.TemporaryDirectory() as td:
                _root, db, rc, err = self._index_then_break(td, bump_version=True)
                self.assertEqual(rc, 0)
                self.assertIn("NestedTypes.swift", err)

                conn = memidx.open_code_db(db)
                chunk_count = conn.execute(
                    "SELECT COUNT(*) AS c FROM chunks WHERE path=?", ("NestedTypes.swift",)
                ).fetchone()["c"]
                sha_row = conn.execute(
                    "SELECT * FROM file_sha WHERE path=?", ("NestedTypes.swift",)
                ).fetchone()
                state = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"]
                conn.close()
                self.assertEqual(chunk_count, 0)
                self.assertIsNotNone(sha_row, "the file_sha row stays -- with a not-indexed status")
                self.assertEqual(sha_row["status"], "not-indexed")
                self.assertNotEqual(
                    state, "current",
                    "a file the chunker could not process is missing from the index "
                    "-- the index must not report itself current",
                )
        finally:
            if getattr(self, "_prev_row", None) is not None:
                chunkers.LANGUAGE_TABLE["swift"] = self._prev_row
                self._prev_row = None

    def test_repair_retriggers_on_the_next_reindex(self):
        with tempfile.TemporaryDirectory() as td:
            root, db, rc, _err = self._index_then_break(td)
            self.assertEqual(rc, 0)

            # chunk_file is restored by _index_then_break's finally -- this
            # second pass is the repaired chunker actually running again.
            rc2 = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc2, 0)

            conn = memidx.open_code_db(db)
            chunk_count = conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()["c"]
            sha_row = conn.execute(
                "SELECT * FROM file_sha WHERE path=?", ("NestedTypes.swift",)
            ).fetchone()
            conn.close()
            self.assertGreater(chunk_count, 0, "repair must re-index the file")
            self.assertIsNotNone(sha_row)

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0,
                     "mode-000 is only unreadable for a non-root posix user")
    def test_unreadable_file_is_skipped_and_the_walk_continues(self):
        """Grok HIGH 2: the per-file try must cover the read/stat/decode
        too, not only the chunker call -- one mode-000 file must not abort
        the whole walk."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            locked = root / "AAALocked.swift"
            locked.write_text("func locked() {}\n")
            locked.chmod(0o000)
            db = Path(td) / "idx-code.sqlite"
            try:
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    rc = code_reindex(root, db, no_embed=True)
            finally:
                locked.chmod(0o644)

            self.assertEqual(rc, 0)
            self.assertIn("AAALocked.swift", err_buf.getvalue())
            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            self.assertIn(
                "NestedTypes.swift", paths,
                "the rest of the tree must still index after one unreadable file",
            )
            self.assertNotIn("AAALocked.swift", paths)


class TestFileStatusRows(unittest.TestCase):
    """Task 3 (Anatomy M2a): deterministic chunker/contract failures land as
    `failed` (sha + chunker_version stored, retried only on source change or
    --full); everything else (a missing backend, a permission error, any
    other exception) lands as `not-indexed` (sha NULL, chunker_version
    stored, attempt_key = the backend availability fingerprint) and is
    retried whenever the run is explicit, --full, or the fingerprint/
    chunker_version no longer match the stamped row -- binding point 1."""

    def _row(self, db, rel):
        conn = memidx.open_code_db(db)
        return conn.execute(
            "SELECT status, reason, sha256, chunker_version FROM file_sha WHERE path=?", (rel,)
        ).fetchone()

    def test_syntax_error_is_failed_with_sha(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "bad.py").write_text("def (:\n")
            (root / "ok.py").write_text("def fine():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")
            row = self._row(db, "bad.py")
            self.assertEqual(row["status"], "failed")
            self.assertIsNotNone(row["sha256"])
            self.assertIsNotNone(row["chunker_version"])
            self.assertEqual(self._row(db, "ok.py")["status"], "ok")

    def test_failed_is_skipped_incrementally_and_retried_on_full_or_source_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "bad.py").write_text("def (:\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code_reindex(root, db, lang="python")
            self.assertIn("1 unchanged", out.getvalue())
            self.assertIn("0 failed", out.getvalue())
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code_reindex(root, db, lang="python", full=True)
            self.assertIn("1 failed", out.getvalue())
            (root / "bad.py").write_text("def fixed():\n    pass\n")
            code_reindex(root, db, lang="python")
            self.assertEqual(self._row(db, "bad.py")["status"], "ok")

    def test_backend_unavailable_is_not_indexed_without_sha_and_retried(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            real = chunkers.get_chunker

            def broken(lang):
                raise chunkers.BackendUnavailable("tree-sitter wheel missing")

            chunkers.get_chunker = broken
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code_reindex(root, db, lang="python")
            finally:
                chunkers.get_chunker = real
            row = self._row(db, "x.py")
            self.assertEqual(row["status"], "not-indexed")
            self.assertIn("wheel missing", row["reason"])
            self.assertIsNone(row["sha256"])
            self.assertIn("1 not indexed", out.getvalue())
            code_reindex(root, db, lang="python")  # explicit run always retries
            self.assertEqual(self._row(db, "x.py")["status"], "ok")

    def test_unreadable_file_is_not_indexed_not_failed(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file modes")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            p = root / "x.py"
            p.write_text("def f():\n    pass\n")
            p.chmod(0)
            db = Path(td) / "idx-code.sqlite"
            try:
                code_reindex(root, db, lang="python")
            finally:
                p.chmod(0o644)
            self.assertEqual(self._row(db, "x.py")["status"], "not-indexed")

    @unittest.skipIf(os.name != "posix" or os.geteuid() == 0,
                     "mode-000 is only unreadable for a non-root posix user")
    def test_retry_gate_runs_before_read_bytes_for_unreadable_file(self):
        """Carried-in fix (Task 3 review, Ruling 57): the retry-gate lookup
        (lang/cv/prev, then the not-indexed leave-it-alone decision) must
        run BEFORE f.read_bytes() -- otherwise an unreadable (or vanished)
        file never reaches the skip and is re-attempted, and re-counted as
        not indexed, on every run. A second, heal-style run
        (retry_not_indexed=False) over an unchanged-availability not-
        indexed row must report 0 not indexed / 1 unchanged, not another
        attempt."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            p = root / "locked.py"
            p.write_text("def f():\n    pass\n")
            p.chmod(0)
            db = Path(td) / "idx-code.sqlite"
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code_reindex(root, db, lang="python")
                self.assertIn("1 not indexed", out.getvalue())
                self.assertEqual(self._row(db, "locked.py")["status"], "not-indexed")

                out2 = io.StringIO()
                with contextlib.redirect_stdout(out2):
                    memidx.cmd_code_reindex(ns(
                        code_root=str(root), drop_root=None, db=str(db), project=memidx.DEFAULT_PROJECT,
                        no_embed=True, full=False, lang="python", retry_not_indexed=False,
                    ))
                self.assertIn("0 not indexed", out2.getvalue())
                self.assertIn("1 unchanged", out2.getvalue())
                self.assertEqual(self._row(db, "locked.py")["status"], "not-indexed")
            finally:
                p.chmod(0o644)

    def test_partial_status_is_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "GapDesync.swift", root / "GapDesync.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db)
            self.assertEqual(self._row(db, "GapDesync.swift")["status"], "partial")

    def _broken_backend(self):
        """Context manager: get_chunker raises BackendUnavailable and the
        availability fingerprint reports python=missing (both patched, so
        the attempt_key stamped on the row matches what a later un-patched
        run compares against)."""
        return mock.patch.multiple(
            chunkers,
            get_chunker=lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("missing")),
            backend_availability=lambda: "python=missing;swift=ok",
        )

    def test_not_indexed_row_carries_attempt_key_and_chunker_version(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with self._broken_backend():
                code_reindex(root, db, lang="python")
            conn = memidx.open_code_db(db)
            row = conn.execute("SELECT attempt_key, chunker_version, sha256 FROM file_sha").fetchone()
            self.assertEqual(row["attempt_key"], "python=missing;swift=ok")
            self.assertEqual(row["chunker_version"], chunkers.chunker_version("python"))
            self.assertIsNone(row["sha256"])

    def test_not_indexed_retry_rules(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with self._broken_backend():
                code_reindex(root, db, lang="python")
                # same fingerprint, heal-style run (no explicit retry): still not-indexed, no attempt made
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    memidx.cmd_code_reindex(ns(
                        code_root=str(root), drop_root=None, db=str(db), project=memidx.DEFAULT_PROJECT,
                        no_embed=True, full=False, lang="python", retry_not_indexed=False,
                    ))
                self.assertIn("0 not indexed", out.getvalue())
            # backend back (fingerprint differs) -> a heal-style run retries and succeeds
            with contextlib.redirect_stdout(io.StringIO()):
                memidx.cmd_code_reindex(ns(
                    code_root=str(root), drop_root=None, db=str(db), project=memidx.DEFAULT_PROJECT,
                    no_embed=True, full=False, lang="python", retry_not_indexed=False,
                ))
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT status FROM file_sha").fetchone()[0], "ok")

    def test_chunker_version_bump_retries_not_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with self._broken_backend():
                code_reindex(root, db, lang="python")
            old = chunkers.LANGUAGE_TABLE["python"]["impl_version"]
            chunkers.LANGUAGE_TABLE["python"]["impl_version"] = old + "-bump"
            try:
                with mock.patch.object(chunkers, "backend_availability", lambda: "python=missing;swift=ok"):
                    # same availability, different chunker version, heal-style run -> retried (and succeeds: real get_chunker)
                    with contextlib.redirect_stdout(io.StringIO()):
                        memidx.cmd_code_reindex(ns(
                            code_root=str(root), drop_root=None, db=str(db), project=memidx.DEFAULT_PROJECT,
                            no_embed=True, full=False, lang="python", retry_not_indexed=False,
                        ))
                conn = memidx.open_code_db(db)
                self.assertEqual(conn.execute("SELECT status FROM file_sha").fetchone()[0], "ok")
            finally:
                chunkers.LANGUAGE_TABLE["python"]["impl_version"] = old

    def test_repairing_root_a_does_not_strand_root_b(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"
            b = Path(td) / "b"
            a.mkdir()
            b.mkdir()
            (a / "x.py").write_text("def fa():\n    pass\n")
            (b / "y.py").write_text("def fb():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with self._broken_backend():
                code_reindex(a, db, lang="python")
                code_reindex(b, db, lang="python")
            code_reindex(a, db, lang="python")  # explicit repair of A only
            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)  # Task 4 -- B's stale attempt_key still differs
            self.assertTrue(rep["availability_changed"])
            self.assertEqual(rep["not_indexed"], 1)


class TestRegistryContractEnforcement(unittest.TestCase):
    """C3 (final fix wave, Codex): the reindex loop is the boundary between
    a chunker backend and the database. It validates what a backend hands
    back -- required keys, a `kind` from the frozen KINDS vocabulary,
    integer line numbers, a real ChunkResult -- and a violation is that
    file's failure (B1's path: warn, purge, continue), never a row in the
    chunks table. Task 3 (Anatomy M2a): a validate_chunk_result violation
    is a bare ValueError, a deterministic failure -- the file_sha row
    stays with status="failed" and its sha256/chunker_version stored, so
    it is skipped incrementally and retried only on a source edit or
    --full, never every run."""

    def _reindex_with_backend(self, td, fake_chunk_file):
        root = Path(td) / "code"
        root.mkdir()
        shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
        db = Path(td) / "idx-code.sqlite"
        original = chunkers.swift.chunk_file
        chunkers.swift.chunk_file = fake_chunk_file
        try:
            out_buf, err_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = code_reindex(root, db, no_embed=True)
        finally:
            chunkers.swift.chunk_file = original
        return db, rc, err_buf.getvalue()

    def _assert_nothing_stored(self, db, rc, err):
        """Name kept from before Task 3 -- no CHUNK ever reaches the table
        for a rejected result. The file_sha row itself now stays (status
        "failed", sha256/chunker_version stored) instead of being absent."""
        self.assertEqual(rc, 0)
        self.assertIn("NestedTypes.swift", err)
        conn = memidx.open_code_db(db)
        chunk_count = conn.execute(
            "SELECT COUNT(*) AS c FROM chunks WHERE path=?", ("NestedTypes.swift",)
        ).fetchone()["c"]
        sha_row = conn.execute(
            "SELECT * FROM file_sha WHERE path=?", ("NestedTypes.swift",)
        ).fetchone()
        conn.close()
        self.assertEqual(chunk_count, 0)
        self.assertIsNotNone(sha_row)
        self.assertEqual(sha_row["status"], "failed")
        self.assertIsNotNone(sha_row["sha256"])

    def test_kind_outside_the_frozen_vocabulary_is_rejected(self):
        def bad_kind(text, rel):
            return chunkers.ChunkResult(
                [{"kind": "method_definition", "symbol": "x", "qualified_name": "x",
                  "signature": "func x()", "doc": "", "start_line": 1, "end_line": 2,
                  "lang": "swift"}],
                [], "ok",
            )
        with tempfile.TemporaryDirectory() as td:
            db, rc, err = self._reindex_with_backend(td, bad_kind)
            self._assert_nothing_stored(db, rc, err)
            self.assertIn("kind", err)

    def test_missing_required_key_is_rejected(self):
        def missing_key(text, rel):
            return chunkers.ChunkResult(
                [{"kind": "function", "symbol": "x", "signature": "func x()",
                  "doc": "", "start_line": 1, "end_line": 2, "lang": "swift"}],
                [], "ok",
            )
        with tempfile.TemporaryDirectory() as td:
            db, rc, err = self._reindex_with_backend(td, missing_key)
            self._assert_nothing_stored(db, rc, err)

    def test_non_integer_line_numbers_are_rejected(self):
        def bad_lines(text, rel):
            return chunkers.ChunkResult(
                [{"kind": "function", "symbol": "x", "qualified_name": "x",
                  "signature": "func x()", "doc": "", "start_line": "1",
                  "end_line": 2, "lang": "swift"}],
                [], "ok",
            )
        with tempfile.TemporaryDirectory() as td:
            db, rc, err = self._reindex_with_backend(td, bad_lines)
            self._assert_nothing_stored(db, rc, err)

    def test_a_backend_returning_none_is_rejected(self):
        def returns_none(text, rel):
            return None
        with tempfile.TemporaryDirectory() as td:
            db, rc, err = self._reindex_with_backend(td, returns_none)
            self._assert_nothing_stored(db, rc, err)

    def test_malformed_gaps_are_rejected(self):
        def bad_gaps(text, rel):
            return chunkers.ChunkResult([], ["not-a-tuple"], "partial")
        with tempfile.TemporaryDirectory() as td:
            db, rc, err = self._reindex_with_backend(td, bad_gaps)
            self._assert_nothing_stored(db, rc, err)

    def test_a_rejected_file_leaves_no_orphan_embedding_rows(self):
        """The chunk INSERT loop runs after the old rows are deleted, so a
        mid-file rejection must not leave a half-written file behind --
        nor queue embeddings for chunk ids that no longer exist."""
        def half_bad(text, rel):
            good = {"kind": "function", "symbol": "ok", "qualified_name": "ok",
                    "signature": "func ok()", "doc": "", "start_line": 1,
                    "end_line": 2, "lang": "swift"}
            bad = dict(good, kind="method_definition", symbol="bad")
            return chunkers.ChunkResult([good, bad], [], "ok")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            original = chunkers.swift.chunk_file
            chunkers.swift.chunk_file = half_bad
            try:
                out_buf, err_buf = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                    rc = code_reindex(root, db, no_embed=False)
            finally:
                chunkers.swift.chunk_file = original
            self.assertEqual(rc, 0)
            conn = memidx.open_code_db(db)
            chunk_count = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
            emb_count = conn.execute("SELECT COUNT(*) AS c FROM embeddings").fetchone()["c"]
            fts_count = conn.execute("SELECT COUNT(*) AS c FROM fts").fetchone()["c"]
            conn.close()
            self.assertEqual(chunk_count, 0)
            self.assertEqual(emb_count, 0)
            self.assertEqual(fts_count, 0)


class TestStaleCheckConsultsChunkerVersion(unittest.TestCase):
    """B2 (final fix wave): the old _code_index_is_stale compared only
    mtime/size, so bumping a chunker's impl_version left every stored row
    reading "current" until something on disk happened to change.
    code_index_report must compare each stored stamp against the chunker
    version that lang would produce today."""

    def test_impl_version_bump_makes_the_index_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            rc = code_reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            try:
                state_before = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"]
                self.assertEqual(state_before, "current")

                prev = chunkers.LANGUAGE_TABLE["swift"]
                bumped = dict(prev)
                bumped["impl_version"] = str(int(prev["impl_version"]) + 100)
                chunkers.LANGUAGE_TABLE["swift"] = bumped
                try:
                    state_after = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"]
                finally:
                    chunkers.LANGUAGE_TABLE["swift"] = prev
            finally:
                conn.close()
            self.assertEqual(
                state_after, "stale",
                "a chunker-version bump must read as stale before any reindex runs",
            )


class TestCodeReindexLangValidation(unittest.TestCase):
    """C2 (final fix wave, Codex): an unknown --lang is a typo, not a
    silent no-op. code-reindex fails, names the languages it knows, and
    persists nothing to code_meta."""

    def test_unknown_lang_fails_and_writes_no_code_meta(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"

            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc = code_reindex(root, db, no_embed=True, lang="ts")
            self.assertNotEqual(rc, 0)
            err = err_buf.getvalue()
            self.assertIn("ts", err)
            self.assertIn("swift", err, "the message must list the known languages")
            self.assertIn("python", err)

            conn = memidx.open_code_db(db)
            rows = conn.execute("SELECT * FROM code_meta").fetchall()
            conn.close()
            self.assertEqual(rows, [], "no code_meta row may be written for an unknown lang")

    def test_one_unknown_lang_among_known_ones_still_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc = code_reindex(root, db, no_embed=True, lang="swift,ts")
            self.assertNotEqual(rc, 0)

            conn = memidx.open_code_db(db)
            chunk_count = conn.execute("SELECT COUNT(*) AS c FROM chunks").fetchone()["c"]
            conn.close()
            self.assertEqual(chunk_count, 0)


class TestPreRewireStampSkewForcesRechunk(unittest.TestCase):
    """Task 3 carry-forward (ruling 2): a .py file whose file_sha row was
    stamped BEFORE this dispatch rewire (chunker_version = swift's, or the
    literal "unversioned" a stray/unwired extension used to get) must
    re-chunk on the very next reindex -- its stored chunker_version can
    never match python's real one, so the dispatch rewire itself cannot
    silently go on serving chunks some other backend produced."""

    def _assert_forces_one_rechunk(self, cv_before):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "basic_functions.py"
            shutil.copy(PY_FIXTURES / "basic_functions.py", target)
            db = Path(td) / "idx-code.sqlite"

            conn = memidx.open_code_db(db)
            data = target.read_bytes()
            sha = hashlib.sha256(data).hexdigest()
            stat = target.stat()
            conn.execute(
                "INSERT INTO file_sha (path, project, code_root, sha256, mtime, size, gap_count, chunker_version) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ("basic_functions.py", memidx.DEFAULT_PROJECT, str(root.resolve()), sha, stat.st_mtime,
                 stat.st_size, 0, cv_before),
            )
            conn.commit()
            conn.close()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0, buf.getvalue())
            m = re.search(r"(\d+) added, (\d+) changed, (\d+) unchanged", buf.getvalue())
            self.assertIsNotNone(m, buf.getvalue())
            self.assertEqual(int(m.group(3)), 0, "a stamp-skewed row must NOT be reported unchanged")
            self.assertEqual(int(m.group(2)), 1, "a stamp-skewed row must force exactly one re-chunk")

            conn = memidx.open_code_db(db)
            row = conn.execute(
                "SELECT chunker_version FROM file_sha WHERE path=?", ("basic_functions.py",)
            ).fetchone()
            chunk_count = conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE path=?", ("basic_functions.py",)
            ).fetchone()["c"]
            conn.close()
            self.assertEqual(row["chunker_version"], chunkers.chunker_version("python"))
            self.assertGreater(chunk_count, 0)

    def test_swift_stamped_row_rechunks_as_python(self):
        self._assert_forces_one_rechunk(chunkers.chunker_version("swift"))

    def test_unversioned_stamped_row_rechunks_as_python(self):
        self._assert_forces_one_rechunk("unversioned")


class TestIsolationFromMarkdownIndex(unittest.TestCase):
    def test_code_reindex_never_creates_the_markdown_project_sqlite(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")

            args = ns(code_root=str(root), project="isoproj", no_embed=True, full=False, lang="swift")
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


class TestExactSymbolRanksFirst(unittest.TestCase):
    def test_body_mentions_do_not_outrank_the_exact_name(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "a.py").write_text(
                "def load_settings():\n    return 1\n\n"
                "def caller():\n    '''load_settings load_settings load_settings'''\n"
                "    load_settings(); load_settings(); return load_settings()\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
            conn = memidx.open_code_db(db)
            ids = memidx.code_hits_fts(conn, "load_settings", memidx.DEFAULT_PROJECT)
            self.assertEqual(conn.execute("SELECT qualified_name FROM chunks WHERE id=?", (ids[0],)).fetchone()[0], "load_settings")

    def test_partial_word_still_matches(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir(); (root / "a.py").write_text("def load_settings():\n    return 1\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
            conn = memidx.open_code_db(db)
            self.assertTrue(memidx.code_hits_fts(conn, "settings", memidx.DEFAULT_PROJECT))


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
        # Finding 8: the old version only asserted "OK" appeared in
        # stdout and that every score (already round-tripped through
        # json.loads, which never yields numpy types either way -- the
        # real risk is json.dumps() raising before that point) was a
        # plain float -- neither check could fail even if `out` were
        # empty, silently losing all coverage of the actual float32 path.
        # Assert a nonzero hit count explicitly instead.
        script = (
            "import sys, io, contextlib, json; sys.path.insert(0, %r); import memidx; "
            "buf = io.StringIO()\n"
            "with contextlib.redirect_stdout(buf):\n"
            "    rc = memidx.main(['code-search', '--db', %r, 'embed a view in a scroll box', "
            "'--mode', %r, '--limit', '3', '--json'])\n"
            "assert rc == 0, rc\n"
            "env = json.loads(buf.getvalue())\n"
            "assert env['state'] == 'current', env['state']\n"
            "out = env['results']\n"
            "assert len(out) > 0, 'zero hits -- the float32 path was never exercised'\n"
            "for h in out:\n"
            "    assert isinstance(h['score'], float), (h['score'], type(h['score']))\n"
            "print('OK', len(out))\n"
        ) % (str(TOOLS_DIR), str(db), mode)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("OK", result.stdout)
        hit_count = int(result.stdout.strip().rsplit(" ", 1)[-1])
        self.assertGreater(hit_count, 0, result.stdout)
        return hit_count

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

            # Finding 8: hybrid's own per-hit score is an RRF rank sum
            # (always a plain float already, per the class docstring), so
            # a passing hybrid JSON test alone doesn't prove the merge
            # actually drew on real vector-side (float32-cast) results
            # rather than FTS alone. Confirm code_hits_vector -- the exact
            # function that used to leak a numpy.float32 score -- genuinely
            # returns nonempty, plain-float results for this same query.
            conn = memidx.open_code_db(db)
            try:
                vec_hits = memidx.code_hits_vector(
                    conn, "embed a view in a scroll box", memidx.DEFAULT_PROJECT
                )
            finally:
                conn.close()
            self.assertGreater(len(vec_hits), 0, "hybrid's vector side never ran")
            for _cid, score in vec_hits:
                self.assertIsInstance(score, float)


def _build_decision_db_with_concepts(parent, concepts, db=None, project=memidx.DEFAULT_PROJECT):
    """The one concept-record fixture every concept-attachment test builds
    on (factored out of what used to be an inline frontmatter block
    duplicated per test): writes one concept.md per entry in `concepts`
    (each a dict with keys concept_id, implemented_by (list of ref
    strings), and optional title/owner_boundary; tested_by/governed_by/
    involved_in are always empty) into ONE scratch markdown root under
    `parent`, indexes them with a SINGLE cmd_reindex call, and returns
    that root.

    ONE call, not one call per concept: cmd_reindex deletes any existing
    record path this run's own walk did not `seen` (removed_paths =
    existing - seen) -- writing concept N into a fresh md_root and
    reindexing again would walk only that new root, see none of the
    earlier concepts' paths, and delete them from the shared db on the
    spot. A caller that wants two concepts in one db (e.g.
    test_two_concepts_on_one_file_different_symbols_each_get_their_own_chunk)
    must pass both specs here together.

    `parent` is always the caller's own tempfile.TemporaryDirectory path
    (never a fresh mkdtemp of this function's own) so the md_root this
    creates is cleaned up with everything else the test already owns.

    `db` explicit routes cmd_reindex straight to that sqlite file
    (TestConceptAttachIsRootChecked, which passes its own decision db path
    and reads it back via code-search's --decision-db); left None, it falls
    through to cmd_reindex's own MEMCONTINUUM_HOME-derived default -- every
    pre-existing caller here, which pins MEMCONTINUUM_HOME (mc_home /
    inline os.environ) to the SAME default code-search itself reads."""
    md_root = Path(parent) / "md"
    md_root.mkdir(parents=True, exist_ok=True)
    for i, c in enumerate(concepts):
        concept_id = c["concept_id"]
        lines = "\n".join(f"  - {p}" for p in c["implemented_by"])
        (md_root / f"concept_{i}.md").write_text(
            "---\n"
            "type: concept\n"
            f"id: {concept_id}\n"
            f"title: {c.get('title') or concept_id}\n"
            f"owner_boundary: {c.get('owner_boundary', 'fixture')}\n"
            "implemented_by:\n"
            f"{lines}\n"
            "tested_by: []\n"
            "governed_by: []\n"
            "involved_in: []\n"
            "---\n\n"
            "Fixture. NOT this concept: nothing else.\n"
        )
    args = ns(root=str(md_root), no_embed=True, full=False, project=project)
    if db is not None:
        args.db = str(db)
    rc = memidx.cmd_reindex(args)
    assert rc == 0
    return md_root


class TestConceptAttachment(unittest.TestCase):
    def test_hit_gets_concept_id_when_decision_db_has_a_matching_implemented_by(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                _build_decision_db_with_concepts(td, [{
                    "concept_id": "CON-ATTACH",
                    "implemented_by": ["NestedTypes.swift#outerFunc"],
                    "title": "Attach Test",
                }])

                code_root = Path(td) / "code"
                code_root.mkdir()
                shutil.copy(FIXTURES / "NestedTypes.swift", code_root / "NestedTypes.swift")
                memidx.cmd_code_reindex(ns(code_root=str(code_root), no_embed=True, full=False, lang="swift"))

                import io
                import contextlib

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = memidx.cmd_code_search(ns(query="outer func", mode="fts", limit=5, json=True))
                self.assertEqual(rc, 0)
                import json as _json

                results = _json.loads(buf.getvalue())["results"]
                self.assertTrue(any(r.get("concept_id") == "CON-ATTACH" for r in results), results)
            finally:
                del os.environ["MEMCONTINUUM_HOME"]

    def test_explicit_code_db_still_attaches_concepts_from_decision_db_and_stays_clean(self):
        """Finding 3: `code-search --db <codedb>` used to run the markdown
        SCHEMA_SQL onto the CODE db (schema clash, concepts read from the
        wrong db, attach silently empty). `--db` for code-search must
        select the CODE db only; concept attachment must always read the
        DECISION db (default project path here -- no --decision-db
        override needed for this case)."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                _build_decision_db_with_concepts(td, [{
                    "concept_id": "CON-EXPLICITDB",
                    "implemented_by": ["NestedTypes.swift#outerFunc"],
                    "title": "Explicit DB Test",
                }])

                code_root = Path(td) / "code"
                code_root.mkdir()
                shutil.copy(FIXTURES / "NestedTypes.swift", code_root / "NestedTypes.swift")
                code_db = Path(td) / "explicit-code.sqlite"
                memidx.cmd_code_reindex(
                    ns(code_root=str(code_root), db=str(code_db), no_embed=True, full=False, lang="swift")
                )

                import io
                import contextlib
                import json as _json

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = memidx.cmd_code_search(
                        ns(query="outer func", mode="fts", limit=5, json=True, db=str(code_db))
                    )
                self.assertEqual(rc, 0)
                results = _json.loads(buf.getvalue())["results"]
                self.assertTrue(
                    any(r.get("concept_id") == "CON-EXPLICITDB" for r in results), results
                )

                import sqlite3

                code_conn = sqlite3.connect(str(code_db))
                tables = {
                    r[0]
                    for r in code_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                code_conn.close()
                markdown_only_tables = {"records", "links", "edges", "assumptions", "concepts", "concept_paths"}
                self.assertFalse(
                    tables & markdown_only_tables,
                    f"code db must never carry markdown tables: {tables & markdown_only_tables}",
                )
            finally:
                del os.environ["MEMCONTINUUM_HOME"]

    def test_two_concepts_on_one_file_different_symbols_each_get_their_own_chunk(self):
        """Finding 4: concept_id attachment must prefer a symbol-level
        match -- an implemented_by entry carrying #symbol must only stamp
        the chunk(s) whose qualified name actually matches that symbol,
        not every chunk in the file."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                _build_decision_db_with_concepts(td, [
                    {"concept_id": "CON-OUTER", "implemented_by": ["NestedTypes.swift#outerFunc"],
                     "title": "Outer func concept"},
                    {"concept_id": "CON-EXT", "implemented_by": ["NestedTypes.swift#extFunc"],
                     "title": "Ext func concept"},
                ])

                code_root = Path(td) / "code"
                code_root.mkdir()
                shutil.copy(FIXTURES / "NestedTypes.swift", code_root / "NestedTypes.swift")
                memidx.cmd_code_reindex(ns(code_root=str(code_root), no_embed=True, full=False, lang="swift"))

                import io
                import contextlib
                import json as _json

                def top_hit(query):
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        rc = memidx.cmd_code_search(ns(query=query, mode="fts", limit=5, json=True))
                    self.assertEqual(rc, 0)
                    return _json.loads(buf.getvalue())["results"][0]

                outer_hit = top_hit("outer func")
                self.assertEqual(outer_hit["qualified_name"], "Outer.outerFunc")
                self.assertEqual(outer_hit.get("concept_id"), "CON-OUTER", outer_hit)

                ext_hit = top_hit("ext func")
                self.assertEqual(ext_hit["qualified_name"], "Outer.extFunc")
                self.assertEqual(ext_hit.get("concept_id"), "CON-EXT", ext_hit)
            finally:
                del os.environ["MEMCONTINUUM_HOME"]


class TestConceptAttachIsRootChecked(unittest.TestCase):
    def test_concept_attaches_only_where_the_file_exists_under_the_hit_root(self):
        """The real adversary: the SAME relative path indexed under two roots,
        one concept whose implemented_by is that path, then B's file deleted
        while B's stale hit stays in the index (heal off). A attaches; B does
        not -- without the guard both would, because the matcher keys on the
        relative path alone."""
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"; b = Path(td) / "b"; a.mkdir(); b.mkdir()
            (a / "main.py").write_text("def shared_name():\n    return 1\n")
            (b / "main.py").write_text("def shared_name():\n    return 2\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(a, db, lang="python"); code_reindex(b, db, lang="python")
            md_db = Path(td) / "decisions.sqlite"
            _build_decision_db_with_concepts(
                td, [{"concept_id": "CON-1", "implemented_by": ["main.py#shared_name"]}], db=md_db
            )
            (b / "main.py").unlink()
            script = ("import sys; sys.path.insert(0, %r); import memidx; "
                      "memidx.main(['code-search', '--db', %r, 'shared_name', '--mode', 'fts', '--json', '--no-heal', '--decision-db', %r])"
                      ) % (str(TOOLS_DIR), str(db), str(md_db))
            env = json.loads(subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30).stdout)
            by_root = {h["code_root"]: h for h in env["results"]}
            self.assertIn("concept_id", by_root[str(a.resolve())])
            self.assertNotIn("concept_id", by_root[str(b.resolve())])


class TestCodeIndexReport(unittest.TestCase):
    def _search(self, db, q="fine", *extra):
        script = ("import sys; sys.path.insert(0, %r); import memidx; "
                  "memidx.main(['code-search', '--db', %r, %r, '--mode', 'fts', '--no-heal'] + %r)"
                  ) % (str(TOOLS_DIR), str(db), q, list(extra))
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)

    def test_failed_file_keeps_index_current_with_a_counted_line(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "bad.py").write_text("def (:\n"); (root / "ok.py").write_text("def fine():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
            r = self._search(db)
            self.assertNotIn("stale", r.stderr.lower(), r.stderr)
            self.assertIn("1 file(s) failed to index", r.stderr)

    def test_touch_is_not_a_change_but_an_edit_is(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir(); p = root / "a.py"; p.write_text("def fine():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
            os.utime(p, (time.time() + 3600, time.time() + 3600))
            conn = memidx.open_code_db(db)
            self.assertEqual(memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"], "current"); conn.close()
            p.write_text("def other():\n    pass\n"); os.utime(p, (time.time() + 7200, time.time() + 7200))
            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            self.assertEqual((rep["state"], rep["changed"]), ("stale", 1))

    def test_not_indexed_makes_state_degraded_not_current(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir(); (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            real = chunkers.get_chunker
            chunkers.get_chunker = lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("missing"))
            try:
                code_reindex(root, db, lang="python")
                conn = memidx.open_code_db(db)
                rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            finally:
                chunkers.get_chunker = real
            self.assertEqual(rep["state"], "degraded"); self.assertEqual(rep["not_indexed"], 1)
            r = self._search(db, "f")
            self.assertIn("incomplete (1 file(s) not indexed)", r.stderr)

    def test_chunker_version_drift_stales_failed_rows_too(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir(); (root / "bad.py").write_text("def (:\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
            old = chunkers.LANGUAGE_TABLE["python"]["impl_version"]
            chunkers.LANGUAGE_TABLE["python"]["impl_version"] = old + "-bump"
            try:
                conn = memidx.open_code_db(db)
                self.assertEqual(memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"], "stale")
            finally:
                chunkers.LANGUAGE_TABLE["python"]["impl_version"] = old

    def test_report_is_per_root_and_missing_root_is_named(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"; b = Path(td) / "b"; a.mkdir(); b.mkdir()
            (a / "x.py").write_text("def fx():\n    pass\n"); (b / "y.py").write_text("def fy():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(a, db, lang="python"); code_reindex(b, db, lang="python")
            (b / "y.py").write_text("def fy2():\n    pass\n"); os.utime(b / "y.py", (time.time() + 3600,) * 2)
            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            self.assertEqual([(r["code_root"], r["changed"]) for r in rep["roots"]], [(str(a.resolve()), 0), (str(b.resolve()), 1)])
            conn.close(); code_reindex(b, db, lang="python"); shutil.rmtree(b)
            conn = memidx.open_code_db(db)
            self.assertEqual(memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"], "degraded"); conn.close()
            r = self._search(db, "fx")
            self.assertIn(f"recorded code root {b.resolve()} does not exist", r.stderr); self.assertIn("--drop-root", r.stderr)

    def test_touching_a_not_indexed_file_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir(); p = root / "x.py"; p.write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with mock.patch.multiple(chunkers, get_chunker=lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("m")),
                                     backend_availability=lambda: "python=missing;swift=ok"):
                code_reindex(root, db, lang="python")
                os.utime(p, (time.time() + 3600,) * 2)
                conn = memidx.open_code_db(db)
                rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            self.assertEqual((rep["state"], rep["changed"], rep["not_indexed"]), ("degraded", 0, 1))

    def test_json_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "bad.py").write_text("def (:\n"); (root / "ok.py").write_text("def fine():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
            env = json.loads(self._search(db, "fine", "--json").stdout)
            self.assertEqual(env["state"], "current"); self.assertEqual(env["failed"], 1); self.assertEqual(env["not_indexed"], 0)
            self.assertEqual(env["code_roots"][0]["code_root"], str(root.resolve())); self.assertEqual(env["embedding_mode"], "none")


class TestHealOnSearch(unittest.TestCase):
    """Anatomy M2a Task 5: `code-search` preflights the index (Task 4's
    code_index_report) and, when it is eligible (stale or degraded, within
    --heal-limit), reindexes each recorded root ONCE in-process before
    answering -- one attempt, never a retry loop -- so a routine edit never
    leaves a search silently answering from stale content. --no-heal and
    --heal-limit (already-parsed no-ops since Task 4) get their meaning
    here."""

    def _run(self, db, q, *extra):
        script = ("import sys; sys.path.insert(0, %r); import memidx; "
                  "memidx.main(['code-search', '--db', %r, %r, '--mode', 'fts'] + %r)") % (str(TOOLS_DIR), str(db), q, list(extra))
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)

    def _stale_tree(self, td):
        root = Path(td) / "code"; root.mkdir(); p = root / "x.py"
        p.write_text("def old_name():\n    pass\n")
        db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
        p.write_text("def new_name():\n    pass\n"); os.utime(p, (time.time() + 3600,) * 2)
        return root, db

    def test_stale_index_heals_once_and_finds_the_new_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._stale_tree(td)
            r = self._run(db, "new_name")
            self.assertIn("new_name", r.stdout); self.assertIn("index healed (1 file(s) re-indexed)", r.stderr)
            self.assertNotIn("stale", r.stderr.lower())
            r2 = self._run(db, "new_name")
            self.assertNotIn("healed", r2.stderr)

    def test_stale_index_heals_with_json_and_envelope_reflects_post_heal_report(self):
        """Carried in from Task 5's review: --json's envelope (state,
        results, changed) must come from the POST-heal report, not the one
        computed before heal_code_index ran."""
        with tempfile.TemporaryDirectory() as td:
            root, db = self._stale_tree(td)
            r = self._run(db, "new_name", "--json")
            env = json.loads(r.stdout)
            self.assertEqual(env["state"], "current")
            self.assertEqual(env["changed"], 0)
            self.assertTrue(
                any(h["qualified_name"] == "new_name" for h in env["results"]), env["results"]
            )

    def test_no_heal_keeps_the_stale_warning(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._stale_tree(td)
            r = self._run(db, "new_name", "--no-heal")
            self.assertNotIn("new_name", r.stdout); self.assertIn("stale", r.stderr.lower())

    def test_heal_limit_refuses_large_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._stale_tree(td); (root / "y.py").write_text("def y():\n    pass\n")
            r = self._run(db, "new_name", "--heal-limit", "1")
            self.assertIn("above --heal-limit 1", r.stderr); self.assertIn("stale", r.stderr.lower())

    def test_missing_root_is_never_healed_or_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"; b = Path(td) / "b"; a.mkdir(); b.mkdir()
            (a / "x.py").write_text("def fx():\n    pass\n"); (b / "y.py").write_text("def fy():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(a, db, lang="python"); code_reindex(b, db, lang="python")
            shutil.rmtree(b)
            (a / "x.py").write_text("def fx2():\n    pass\n"); os.utime(a / "x.py", (time.time() + 3600,) * 2)
            r = self._run(db, "fx2")
            self.assertIn("fx2", r.stdout)                       # a healed
            self.assertIn("does not exist", r.stderr)            # b named, not touched
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT count(*) FROM chunks WHERE code_root=?", (str(b.resolve()),)).fetchone()[0], 1)
            self.assertEqual(memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"], "degraded")   # never current while b is missing

    def test_heal_follows_embedding_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._stale_tree(td)                       # mode none
            self._run(db, "new_name")
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT count(*) FROM embeddings").fetchone()[0], 0)

    def test_failed_file_does_not_trigger_heal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "bad.py").write_text("def (:\n"); (root / "ok.py").write_text("def fine():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"; code_reindex(root, db, lang="python")
            r = self._run(db, "fine")
            self.assertNotIn("healed", r.stderr); self.assertNotIn("stale", r.stderr.lower())

    def test_not_indexed_is_retried_only_when_availability_changes(self):
        """Task 5 conflict resolved (see task-5-report.md): the brief's
        fixture made BOTH files not-indexed (get_chunker mocked to throw for
        every lang), so there was nothing genuinely indexed left for step
        (a)'s edit to change -- the sha-based `changed` count binding point
        4 relies on never fires for a not-indexed (sha-NULL) row, so the
        edit could never be observed and "g2" could never be found. Fixed
        by indexing other.py for REAL first (no mock active), THEN adding
        x.py under the mocked-unavailable backend -- only x.py is
        not-indexed; other.py's later edit is a genuine sha-confirmed
        change the preflight (and the heal) can see."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "other.py").write_text("def g():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")                # other.py indexed ok (real backend)
            (root / "x.py").write_text("def f():\n    pass\n")
            with mock.patch.multiple(chunkers, get_chunker=lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("m")),
                                     backend_availability=lambda: "python=missing;swift=ok"):
                code_reindex(root, db, lang="python")            # x.py not-indexed, attempt_key python=missing; other.py untouched (sha match, chunker never called)
            # (a) same fingerprint + an unrelated edit: heal re-indexes the edit only, x.py stays not-indexed
            (root / "other.py").write_text("def g2():\n    pass\n"); os.utime(root / "other.py", (time.time() + 3600,) * 2)
            script = ("import sys; sys.path.insert(0, %r); import chunkers, memidx; "
                      "chunkers.backend_availability = lambda: 'python=missing;swift=ok'; "
                      "memidx.main(['code-search', '--db', %r, 'g2', '--mode', 'fts'])") % (str(TOOLS_DIR), str(db))
            r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
            self.assertIn("g2", r.stdout)
            conn = memidx.open_code_db(db)
            self.assertEqual(conn.execute("SELECT status FROM file_sha WHERE path='x.py'").fetchone()[0], "not-indexed"); conn.close()
            # (b) fingerprint differs (backend really available now) -> heal retries x.py
            r = self._run(db, "f")
            self.assertIn("f", r.stdout); self.assertIn("healed", r.stderr)
            r2 = self._run(db, "f")
            self.assertNotIn("healed", r2.stderr)

    def test_healed_count_reports_files_actually_reindexed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir(); (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with mock.patch.multiple(chunkers, get_chunker=lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("m")),
                                     backend_availability=lambda: "python=missing;swift=ok"):
                code_reindex(root, db, lang="python")
            r = self._run(db, "f")                      # availability-only heal: preflight changed == 0
            self.assertIn("index healed (1 file(s) re-indexed)", r.stderr)

    def test_heal_failure_is_fail_open(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._stale_tree(td)
            real = memidx.cmd_code_reindex
            memidx.cmd_code_reindex = lambda a: (_ for _ in ()).throw(RuntimeError("boom"))
            # in-process call needed for the monkeypatch: call cmd_code_search directly
            try:
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
                    rc = memidx.cmd_code_search(ns(db=str(db), project=memidx.DEFAULT_PROJECT, query="old_name", mode="fts",
                                                   limit=10, json=False, decision_db=None, no_heal=False, heal_limit=500))
            finally:
                memidx.cmd_code_reindex = real
            self.assertEqual(rc, 0); self.assertIn("heal failed", buf.getvalue()); self.assertIn("stale", buf.getvalue().lower())


class TestStaleWarning(unittest.TestCase):
    def test_editing_a_source_file_triggers_stderr_warning_on_search(self):
        """Anatomy M2a Task 4: code_index_report is sha-confirmed -- a bare
        `touch` alone is no longer enough to warn stale (see
        TestCodeIndexReport.test_touch_is_not_a_change_but_an_edit_is); an
        actual content edit is.

        Anatomy M2a Task 5: a stale index heals itself by default now, so
        this must pass --no-heal to still exercise the bare warning path;
        TestHealOnSearch covers the with-heal behavior."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys, time, os; sys.path.insert(0, %r); import memidx; "
                "p = %r; open(p, 'a').write('\\nfunc addedLater() {}\\n'); "
                "os.utime(p, (time.time() + 3600, time.time() + 3600)); "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts', '--no-heal'])"
            ) % (str(TOOLS_DIR), str(target), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertIn("stale", result.stderr.lower(), result.stderr)

    def test_touch_then_reindex_with_unchanged_sha_clears_stale_warning(self):
        """Finding 2: a SHA-skipped unchanged file must still refresh its
        stored file_sha mtime/size row -- otherwise the on-disk mtime keeps
        drifting away from the stale-comparison snapshot forever after a
        mere touch, even though a reindex genuinely ran and found nothing
        to do. touch -> reindex (sha unchanged, 0 chunks touched) -> search
        must NOT warn stale."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            os.utime(target, (time.time() + 3600, time.time() + 3600))
            code_reindex(root, db, no_embed=True)

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['code-search', '--db', %r, 'outer func', '--mode', 'fts'])"
            ) % (str(TOOLS_DIR), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertNotIn("stale", result.stderr.lower(), result.stderr)

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


class TestIndexProvenance(unittest.TestCase):
    """Finding 1 (HIGH): code-search must distinguish three code-index
    states and SAY so -- uninitialized (never reindexed for this project:
    refuse, not a healthy-empty result), stale (existing warning, now also
    surfaced in --json), current (indexed_at/code_root/head_sha surfaced
    in --json)."""

    def _run(self, argv):
        script = (
            "import sys; sys.path.insert(0, %r); import memidx; "
            "sys.exit(memidx.main(%r))"
        ) % (str(TOOLS_DIR), argv)
        return subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )

    def test_uninitialized_index_refuses_not_a_bare_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "never-reindexed-code.sqlite"
            result = self._run(["code-search", "--db", str(db), "anything", "--mode", "fts"])
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("code-reindex", (result.stdout + result.stderr).lower(), result.stderr)
            self.assertNotEqual(result.stdout.strip(), "[]")

    def test_uninitialized_index_json_carries_structured_state_not_bare_list(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "never-reindexed-code.sqlite"
            result = self._run(["code-search", "--db", str(db), "anything", "--mode", "fts", "--json"])
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["state"], "uninitialized")
            self.assertEqual(data["results"], [])

    def test_current_index_json_carries_indexed_at_and_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            result = self._run(["code-search", "--db", str(db), "outer func", "--mode", "fts", "--json"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["state"], "current")
            self.assertEqual(data["code_root"], str(root))
            self.assertIsInstance(data["indexed_at"], (int, float))
            self.assertGreater(len(data["results"]), 0)

    def test_stale_index_json_carries_state_stale(self):
        """Anatomy M2a Task 4: code_index_report is sha-confirmed -- a real
        content edit (not a bare `touch`) is what the state must react to.

        Anatomy M2a Task 5: --no-heal, so this exercises the raw preflight
        state rather than the now-default heal-to-current behavior
        (TestHealOnSearch covers that)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            target = root / "NestedTypes.swift"
            shutil.copy(FIXTURES / "NestedTypes.swift", target)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)
            target.write_text(target.read_text() + "\nfunc addedLater() {}\n")
            os.utime(target, (time.time() + 3600, time.time() + 3600))

            result = self._run(["code-search", "--db", str(db), "outer func", "--mode", "fts", "--json", "--no-heal"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["state"], "stale")

    def test_code_meta_stores_head_sha_when_code_root_is_a_git_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.email=t@t.t", "-c", "user.name=t",
                 "add", "-A"], check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "-c", "user.email=t@t.t", "-c", "user.name=t",
                 "commit", "-q", "-m", "init"], check=True,
            )
            expected_sha = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()

            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            conn = memidx.open_code_db(db)
            row = conn.execute(
                "SELECT head_sha FROM code_meta WHERE project=?", (memidx.DEFAULT_PROJECT,)
            ).fetchone()
            conn.close()
            self.assertEqual(row["head_sha"], expected_sha)


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




class TestLoadProbeSet(unittest.TestCase):
    """Runs in the DEFAULT suite. The real-corpus test that consumes this
    parser is double-gated and normally skips, so without these the parser
    would never execute in CI at all (reviewer finding 6, 2026-08-31)."""

    def write(self, body):
        d = tempfile.mkdtemp()
        f = Path(d) / "probes.tsv"
        f.write_text(body, encoding="utf-8")
        return f

    GOOD = (
        "#corpus: /tmp\n"
        "#min_top1: 2\n"
        "a query\tPkg.alpha\t-\tgold\n"
        "b query\tPkg.beta\tbypass\tgold\n"
        "bench only\tPkg.gamma\t-\t-\n"
    )

    def test_parses_directives_and_gold_rows_only(self):
        corpus, probes, min_top1 = load_probe_set(self.write(self.GOOD))
        self.assertEqual(corpus, Path("/tmp"))
        self.assertEqual(min_top1, 2)
        self.assertEqual(probes, [("a query", "Pkg.alpha"), ("b query", "Pkg.beta")])

    def test_missing_file_is_none_not_an_error(self):
        self.assertIsNone(load_probe_set(Path("/nonexistent/probes.tsv")))

    def test_non_utf8_file_is_none_not_an_error(self):
        d = tempfile.mkdtemp()
        f = Path(d) / "probes.tsv"
        f.write_bytes(b"#corpus: /tmp\n\xff\xfe binary\n")
        self.assertIsNone(load_probe_set(f))

    def test_empty_corpus_directive_is_refused(self):
        """An empty value would resolve to Path(".") and point the real-corpus
        reindex at the working directory."""
        self.assertIsNone(load_probe_set(self.write(
            "#corpus:\n#min_top1: 1\nq\tP.a\t-\tgold\n")))

    def test_missing_min_top1_is_refused_rather_than_defaulted(self):
        """Defaulting it to 1 would make the assertion nearly vacuous."""
        self.assertIsNone(load_probe_set(self.write("#corpus: /tmp\nq\tP.a\t-\tgold\n")))

    def test_non_integer_min_top1_is_refused(self):
        self.assertIsNone(load_probe_set(self.write(
            "#corpus: /tmp\n#min_top1: five\nq\tP.a\t-\tgold\n")))

    def test_no_gold_rows_is_refused(self):
        self.assertIsNone(load_probe_set(self.write(
            "#corpus: /tmp\n#min_top1: 1\nq\tP.a\t-\t-\n")))

    def test_empty_first_corpus_directive_is_not_overridden_by_a_later_one(self):
        """First-wins must hold even for an empty value, or this parser and
        the bench's `sed | head -1` disagree about the same file."""
        self.assertIsNone(load_probe_set(self.write(
            "#corpus:\n#corpus: /tmp\n#min_top1: 1\nq\tP.a\t-\tgold\n")))

    def test_zero_min_top1_is_refused_as_vacuous(self):
        self.assertIsNone(load_probe_set(self.write(
            "#corpus: /tmp\n#min_top1: 0\nq\tP.a\t-\tgold\n")))

    def test_first_directive_wins_matching_the_bench_script(self):
        """scripts/codanna-bench.sh reads `sed ... | head -1`; last-wins here
        would make the two consumers disagree about the same file."""
        corpus, _, min_top1 = load_probe_set(self.write(
            "#corpus: /tmp\n#corpus: /var\n#min_top1: 3\n#min_top1: 9\n"
            "q\tP.a\t-\tgold\n"))
        self.assertEqual(corpus, Path("/tmp"))
        self.assertEqual(min_top1, 3)

    def test_comment_with_a_space_is_a_comment_not_a_directive(self):
        corpus, _, _ = load_probe_set(self.write(
            "# corpus: /decoy\n#corpus: /tmp\n#min_top1: 1\nq\tP.a\t-\tgold\n"))
        self.assertEqual(corpus, Path("/tmp"))


class TestGoldProbesRealCorpus(unittest.TestCase):
    """Hybrid top-1 accuracy against a real code corpus, not the fixtures.

    Double-gated: $MEMCONTINUUM_TEST_REAL_CORPUS must be set AND the untracked
    probe file must exist (see load_probe_set). Everything project-specific --
    corpus root, queries, expected qualified names, the pass threshold -- comes
    from that file, so this test names nothing private. The threshold is below
    100% on purpose: the misses are plausible near-misses (a sibling symbol or
    a semantically adjacent one taking top-1), not index bugs; the analysis of
    which probes miss and why is in the internal notes beside the probe file.
    """

    @unittest.skipUnless(
        os.environ.get("MEMCONTINUUM_TEST_REAL_CORPUS"),
        "set $MEMCONTINUUM_TEST_REAL_CORPUS=1 to run the real-corpus gold-probe test",
    )
    def test_hybrid_top1_on_real_corpus(self):
        loaded = load_probe_set()
        if loaded is None:
            self.skipTest(f"no probe set at {PROBE_FILE} (see $MEMCONTINUUM_TEST_PROBES)")
        corpus_root, probes, min_top1 = loaded
        self.assertTrue(corpus_root.is_dir(), corpus_root)
        # Cached (incrementally reused, like scripts/codanna-bench.sh's own
        # index) rather than a fresh tempdir -- a full embed of a real corpus
        # takes ~20+ minutes on this host, and re-embeds nothing once the
        # corpus hasn't changed since the last run.
        cache_dir = Path.home() / ".cache" / "codanna-bench"
        cache_dir.mkdir(parents=True, exist_ok=True)
        db = cache_dir / "bench-code.sqlite"
        code_reindex(corpus_root, db, project="codanna-bench", no_embed=False, full=False)

        hits = 0
        details = []
        for query, expect in probes:
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
        self.assertGreaterEqual(
            hits, min_top1, f"{hits}/{len(probes)} top-1 (threshold {min_top1}) -- {details}"
        )


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
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertTrue(
                any("CON-CODE-BADSYM" in e and "doesNotExist" in e for e in errors), errors
            )

    def test_symbol_declared_in_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertFalse(any("CON-CODE-GOOD" in e for e in errors), errors)

    def test_symbol_check_skipped_without_code_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_symbol.md", root / "bad_symbol.md")
            errors, _warnings = memlint.lint_root(root)
            self.assertFalse(any("doesNotExist" in e for e in errors), errors)


class TestMemlintSymbolVocabulary(unittest.TestCase):
    """Finding 5: memlint's #symbol check must recognize everything the
    chunker emits (init, subscript, computed var names, static/class func,
    operators, backtick names) and stop matching inside comments/strings."""

    def test_init_computed_var_static_func_and_backtick_name_all_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "vocab_good.md", root / "vocab_good.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertFalse(any("CON-VOCAB-GOOD" in e for e in errors), errors)

    def test_symbol_only_in_a_comment_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "vocab_bad_comment.md", root / "vocab_bad_comment.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertTrue(
                any("CON-VOCAB-BADCOMMENT" in e and "commentedOutSymbol" in e for e in errors),
                errors,
            )

    def test_symbol_only_in_a_string_literal_is_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "vocab_bad_string.md", root / "vocab_bad_string.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertTrue(
                any("CON-VOCAB-BADSTRING" in e and "stringOnlySymbol" in e for e in errors),
                errors,
            )


class TestMemlintFragmentAttachAgreement(unittest.TestCase):
    """Finding 4: memlint's #symbol vocabulary check must accept a QUALIFIED
    fragment (e.g. "Outer.outerFunc") exactly as code-search's runtime
    concept attachment (concept_matches_for_chunk) does -- one shared
    matcher (memidx.fragment_matches_symbol / fragment_declared_in_text),
    not two independently-encoded rules that can disagree."""

    def test_qualified_fragment_matching_runtime_attachment_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "qualified_symbol_good.md", root / "qualified_symbol_good.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertFalse(any("CON-CODE-QUALGOOD" in e for e in errors), errors)

    def test_lint_and_runtime_attach_agree_on_the_same_qualified_fragment(self):
        # The same predicate, exercised both ways on the same fixture data:
        # lint_concept accepts "Outer.outerFunc" against NestedTypes.swift
        # (above) AND concept_matches_for_chunk would match a chunk whose
        # symbol="outerFunc"/qualified_name="Outer.outerFunc" against a
        # concept_paths row fragment of "Outer.outerFunc" -- verified
        # directly via the shared predicate so a future regression that
        # breaks only one side is caught here.
        self.assertTrue(memidx.fragment_matches_symbol("Outer.outerFunc", "outerFunc", "Outer.outerFunc"))
        text = (FIXTURES / "NestedTypes.swift").read_text()
        self.assertTrue(memidx.fragment_declared_in_text("Outer.outerFunc", text, rel_path="NestedTypes.swift"))


class TestMemlintPathContainment(unittest.TestCase):
    """Finding 4: implemented_by/tested_by path resolution against code_root
    must be containment-checked -- an absolute ref_path used to silently
    discard code_root entirely (Path's `/` operator drops the left side for
    an absolute right-hand side), and a relative "../" path could walk
    outside code_root with no check at all."""

    def _lint_one_ref(self, ref_path: str, td: Path, code_root: Path):
        (td / "escape_concept.md").write_text(
            "---\n"
            "type: concept\n"
            "id: CON-CODE-ESCAPE\n"
            "title: Fixture -- path containment\n"
            "owner_boundary: fixture\n"
            "implemented_by:\n"
            f"  - {ref_path}\n"
            "tested_by: []\n"
            "governed_by: []\n"
            "involved_in: []\n"
            "---\n\n"
            "Fixture. NOT this concept: nothing else.\n"
        )
        return memlint.lint_root(td, code_roots=[code_root])

    def test_absolute_ref_path_escaping_code_root_is_an_error(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            outside = td / "secret.swift"
            outside.write_text("func real() {}\n")
            errors, _warnings = self._lint_one_ref(str(outside), td, code_root)
            self.assertTrue(
                any("CON-CODE-ESCAPE" in e and "absolute" in e for e in errors), errors
            )

    def test_relative_dotdot_escaping_code_root_is_an_error(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            outside = td / "secret.swift"
            outside.write_text("func real() {}\n")
            errors, _warnings = self._lint_one_ref("../secret.swift", td, code_root)
            self.assertTrue(
                any("CON-CODE-ESCAPE" in e and "escapes code_root" in e for e in errors), errors
            )

    def test_relative_path_inside_code_root_is_not_a_containment_error(self):
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            code_root = td / "code"
            code_root.mkdir()
            (code_root / "Inside.swift").write_text("func real() {}\n")
            errors, _warnings = self._lint_one_ref("Inside.swift", td, code_root)
            self.assertFalse(
                any("CON-CODE-ESCAPE" in e and ("absolute" in e or "escapes" in e) for e in errors),
                errors,
            )


class TestMemlintUnqualifiedLargeFile(unittest.TestCase):
    def test_no_symbol_on_small_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "no_symbol_small.md", root / "no_symbol_small.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
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
            errors, _warnings = memlint.lint_root(root, code_roots=[code_root])
            self.assertTrue(
                any("CON-CODE-BIG" in e and "400" in e for e in errors), errors
            )


class TestMemlintGovernedByRegistry(unittest.TestCase):
    def test_unknown_governed_by_id_is_error_when_topics_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_governed.md", root / "bad_governed.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertTrue(
                any("CON-CODE-BADGOV" in e and "TOP-DOES-NOT-EXIST" in e for e in errors), errors
            )

    def test_known_governed_by_id_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertFalse(any("governed_by" in e and "CON-CODE-GOOD" in e for e in errors), errors)

    def test_governed_by_check_skipped_when_no_topic_registry_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "bad_governed.md", root / "bad_governed.md")
            # deliberately NOT copying top-code-1.md -- zero topics in this
            # corpus, so there's no registry to validate against.
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertFalse(any("governed_by" in e for e in errors), errors)


class TestMemlintDuplicateClaim(unittest.TestCase):
    def test_two_concepts_claiming_the_same_symbol_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "dup_a.md", root / "dup_a.md")
            shutil.copy(CONCEPTS / "dup_b.md", root / "dup_b.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertTrue(
                any("CON-CODE-DUP-A" in e and "CON-CODE-DUP-B" in e for e in errors), errors
            )

    def test_single_owner_is_not_a_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "dup_a.md", root / "dup_a.md")
            errors, _warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertFalse(any("duplicate" in e.lower() for e in errors), errors)


class TestMemlintNotThisConcept(unittest.TestCase):
    def test_body_without_not_this_sentence_warns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "no_not_this.md", root / "no_not_this.md")
            _errors, warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertTrue(
                any("CON-CODE-NO-NOT-THIS" in w and "not this" in w.lower() for w in warnings), warnings
            )

    def test_body_with_not_this_sentence_does_not_warn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copy(CONCEPTS / "good.md", root / "good.md")
            shutil.copy(CONCEPTS / "topics" / "top-code-1.md", root / "top-code-1.md")
            _errors, warnings = memlint.lint_root(root, code_roots=[CODE_ROOT])
            self.assertFalse(
                any("CON-CODE-GOOD" in w and "not this" in w.lower() for w in warnings), warnings
            )


class TestMemlintExistingRulesStillGreen(unittest.TestCase):
    def test_v11_concept_fixtures_still_lint_clean_of_the_original_checks(self):
        v11_concepts = TOOLS_DIR / "fixtures" / "v11" / "concepts"
        v11_code = TOOLS_DIR / "fixtures" / "v11" / "code"
        errors, _warnings = memlint.lint_root(v11_concepts, code_roots=[v11_code])
        # CON-900's missing path is still expected; nothing NEW should
        # appear for CON-007 (media-identity) or CON-901 (no-tests).
        self.assertFalse(any("CON-007" in e for e in errors), errors)


# ---------------------------------------------------------------------------
# Task 11: self-index acceptance gate (design doc S6) -- the engine indexes
# itself. Every reviewer round across this milestone named "point it at its
# own repo" as the closer; this is that check, made real. THIS repo
# (TOOLS_DIR = Path(__file__).resolve().parent.parent) is the tree under
# test, run through the real registry dispatch (chunkers.get_chunker), the
# real python AST chunker, the real skip-dir census and the real FTS index
# end to end -- no synthetic fixture stands in for it.
# ---------------------------------------------------------------------------


class TestSelfIndexAcceptanceGate(unittest.TestCase):
    """`code-reindex --lang python --no-embed` over TOOLS_DIR itself, indexed
    once for the whole class (setUpClass) -- this repo's ~19 .py files
    reindex in well under a second with --no-embed, and every assertion
    below reads the same resulting index, so there is no reason to pay for
    it four times over.

    MEMCONTINUUM_HOME is pointed at a throwaway tmpdir and --db is left
    unset (matches ns()'s default), so resolve_code_db_path lands the index
    at $MEMCONTINUUM_HOME/<PROJECT>-code.sqlite -- the SAME path
    _resolve_symbol_via_code_index hardcodes (it never consults --db; see
    that function's docstring), which is what lets the fast-path test below
    exercise the real fast path instead of a --db location it can't see.

    A note on the two FTS assertions below: this class is itself indexed as
    part of "the engine indexes itself" (it lives under TOOLS_DIR, in
    tests/), and each probe method necessarily embeds its own query string
    as a literal argument. Method names, docstrings and comments here
    therefore deliberately do NOT restate those query words -- the FIRST
    version of this gate named its test methods after the queries, and
    each self-indexed method promptly out-scored the real target (its own
    short chunk repeating the query in its name AND its call site beat the
    target file's single genuine mention) with a self-referential false
    positive, not a chunker defect. One literal mention per probe (the
    `query = "..."` line the test needs to actually call code_hits_fts)
    is unavoidable and left as-is.
    """

    PROJECT = "anatomy-self-index"

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory()
        home = Path(cls._tmpdir.name) / "home"
        home.mkdir()
        cls._prev_home = os.environ.get("MEMCONTINUUM_HOME")
        os.environ["MEMCONTINUUM_HOME"] = str(home)

        args = ns(
            code_root=str(TOOLS_DIR),
            project=cls.PROJECT,
            db=None,
            no_embed=True,
            full=False,
            lang="python",
        )
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = memidx.cmd_code_reindex(args)
        # Not asserted on: reindexing this repo's real tree always prints a
        # "files with unsupported/unwired extensions not indexed" census
        # line (.md/.sh/.swift/... alongside the wired .py files) and a
        # WARNING for tests/fixtures/python_corpus/broken.py (Task 4's
        # deliberately-invalid fixture, a syntax-error probe, not a defect
        # here) -- both expected, neither a failure. Captured only so a
        # green run stays quiet; kept on the class for a failing test's
        # message to include if something above rc unexpectedly breaks.
        cls.reindex_stdout = out_buf.getvalue()
        cls.reindex_stderr = err_buf.getvalue()
        if rc != 0:
            raise AssertionError(
                f"code-reindex failed (rc={rc})\nstdout:\n{cls.reindex_stdout}\nstderr:\n{cls.reindex_stderr}"
            )

        cls.db_path = home / f"{cls.PROJECT}-code.sqlite"

    @classmethod
    def tearDownClass(cls):
        if cls._prev_home is None:
            os.environ.pop("MEMCONTINUUM_HOME", None)
        else:
            os.environ["MEMCONTINUUM_HOME"] = cls._prev_home
        cls._tmpdir.cleanup()

    def _conn(self):
        return memidx.open_code_db(self.db_path)

    def test_memidx_py_yields_over_50_chunks(self):
        conn = self._conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE project=? AND path=?",
                (self.PROJECT, "memidx.py"),
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertGreater(count, 50, f"memidx.py chunk count was {count}")

    def test_fts_search_for_a_known_memidx_symbol_hits_that_file(self):
        query = "parse_frontmatter"
        conn = self._conn()
        try:
            ids = memidx.code_hits_fts(conn, query, self.PROJECT, limit=10)
            self.assertTrue(ids, f"no FTS hits for {query!r}")
            top = conn.execute("SELECT path FROM chunks WHERE id=?", (ids[0],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(top["path"], "memidx.py")

    def test_fts_search_for_a_multiword_phrase_hits_its_home_file(self):
        # fts_escape ORs the words of a multi-word query; the target
        # chunk matches every one of them via its own qualified name
        # (split into its two identifier halves) plus its own doc line --
        # genuinely the top bm25 hit, not a tuned assertion. See the class
        # docstring for why this method avoids restating the query itself
        # anywhere but the one line below that actually needs it.
        query = "merge settings hook entries"
        conn = self._conn()
        try:
            ids = memidx.code_hits_fts(conn, query, self.PROJECT, limit=10)
            self.assertTrue(ids, f"no FTS hits for {query!r}")
            top = conn.execute("SELECT path FROM chunks WHERE id=?", (ids[0],)).fetchone()
        finally:
            conn.close()
        self.assertEqual(top["path"], "scripts/mc_settings_merge.py")

    def test_no_venv_or_pycache_paths_indexed(self):
        # This tree has no .venv/venv directory at all right now (only the
        # tracked __pycache__ dirs the test run itself recreates under
        # scripts/, chunkers/, tests/ and the repo root) -- so only the
        # __pycache__ half of this check is live today. The .venv/venv
        # checks stay in as a guard against a regression the moment a venv
        # ever gets created inside this repo (the skip_dirs entry already
        # covers it; this just asserts the walker actually honors it).
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT DISTINCT path FROM file_sha WHERE project=?", (self.PROJECT,)
            ).fetchall()
        finally:
            conn.close()
        offenders = [
            r["path"]
            for r in rows
            if r["path"].startswith(".venv/")
            or r["path"].startswith("venv/")
            or r["path"].startswith("__pycache__/")
            or "/.venv/" in r["path"]
            or "/venv/" in r["path"]
            or "/__pycache__/" in r["path"]
        ]
        self.assertEqual(offenders, [])

    def test_resolve_symbol_to_path_uses_the_code_index_fast_path(self):
        """Ruling 5 (controller): the spec's controller-resolves-a-symbol
        requirement is satisfied in M1 via the CODE-INDEX FAST PATH, not
        the bare --code-root lexer fallback (still swift-only -- an M2
        item). resolve_symbol_to_path with project= set tries
        _resolve_symbol_via_code_index FIRST; that function reads ONLY
        $MEMCONTINUUM_HOME/<project>-code.sqlite (never --db -- see its own
        docstring), which is exactly the db setUpClass built above, so a
        hit here is the fast path actually firing, not the fallback scan
        silently doing the same work."""
        symbol = "parse_frontmatter"
        resolved = memidx.resolve_symbol_to_path(TOOLS_DIR, symbol, project=self.PROJECT)
        self.assertEqual(resolved, "memidx.py")

    def test_why_on_a_bare_symbol_names_the_file_that_defines_it(self):
        """C6 (Anatomy M1 fix wave, Codex): the milestone's acceptance
        sentence is about the real COMMAND, not the helper underneath it.
        `why <bare symbol>` must go through cmd_why -- bare-symbol
        detection, --code-root resolution, the code-index fast path, the
        decision-store lookup -- and print a line naming memidx.py.

        There are no concepts in this throwaway store, so the printed line
        is `no concept claims 'memidx.py'` -- which is exactly the point:
        the command resolved the symbol to its defining file and said so.
        A regression in any step above prints a different path, or exits
        non-zero, instead."""
        args = ns(
            symbol_or_path="parse_frontmatter",
            code_root=str(TOOLS_DIR),
            project=self.PROJECT,
            db=str(Path(self._tmpdir.name) / "decisions.sqlite"),
            json=False,
        )
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = memidx.cmd_why(args)
        self.assertEqual(rc, 0, buf.getvalue() + err.getvalue())
        self.assertIn("memidx.py", buf.getvalue(), buf.getvalue())


if __name__ == "__main__":
    unittest.main()
