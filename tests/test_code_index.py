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
import errno
import hashlib
import importlib.metadata
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
from stat import S_ISLNK, S_ISREG
from types import SimpleNamespace
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import chunkers  # noqa: E402
# Explicit: `chunkers.treesitter` is a submodule the package imports lazily, so
# the attribute exists only once something has imported it. Tests here call
# chunkers.treesitter.reset_cache(), which passed only because another test
# module in the same discover run happened to import it first.
import chunkers.treesitter  # noqa: E402,F401
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


class TestCodeRrfConstant(unittest.TestCase):
    def test_code_rrf_k_is_30(self):
        # Provisional value chosen on the graded intent set; a silent revert to
        # the store search's 60 must fail here.
        self.assertEqual(memidx.CODE_RRF_K, 30)


class TestCodeSchemaV3(unittest.TestCase):
    """Design R3/R4 (audit MC-P1-02/MC-P1-06): renamed from the class's
    original TestCodeSchemaV2 name when CODE_SCHEMA_VERSION bumped 2->3
    (file_sha gains mtime_ns/ctime_ns/ino/dev, embeddings gains embed_fp,
    code_project gains embedding_fingerprint -- the two fingerprint
    columns reserved for the embedding-fingerprint work still to land)."""

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

    def test_fresh_db_has_v3_shape(self):
        with tempfile.TemporaryDirectory() as td:
            conn = memidx.open_code_db(Path(td) / "c.sqlite")
            self.assertEqual(memidx.code_schema_version(conn), 3)
            self.assertIn("code_root", self._cols(conn, "chunks"))
            self.assertTrue({"code_root", "status", "reason", "chunker_version"} <= self._cols(conn, "file_sha"))
            self.assertTrue(
                {"mtime_ns", "ctime_ns", "ino", "dev"} <= self._cols(conn, "file_sha"),
                "design R3: the four new stat-signal columns",
            )
            self.assertIn("embed_fp", self._cols(conn, "embeddings"), "design R4: reserved, NULL for now")
            self.assertNotIn("langs", self._cols(conn, "code_meta"))
            self.assertEqual(
                self._cols(conn, "code_project"),
                {"project", "langs", "embedding_mode", "embedding_fingerprint"},
            )
            self.assertIn("attempt_key", self._cols(conn, "file_sha"))
            pk = sorted(r[1] for r in conn.execute("PRAGMA table_info(code_meta)") if r[5])
            self.assertEqual(pk, ["code_root", "project"])
            self.assertIn("tokenchars", conn.execute("SELECT sql FROM sqlite_master WHERE name='fts'").fetchone()[0])

    def test_older_db_keeps_roots_and_langs_but_drops_derived_rows(self):
        with tempfile.TemporaryDirectory() as td:
            conn = memidx.open_code_db(self._v1(td))
            self.assertEqual(memidx.code_schema_version(conn), 3)
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

    def test_rebuild_of_a_v2_db_preserves_langs_from_code_project(self):
        """Fix-wave item 8 follow-up: langs lives on `code_project` from
        schema v2 on -- code_meta has NO langs column here at all. A
        rebuild that (as CODE_TABLES now requires) drops code_project too
        must still carry ITS langs forward, not just code_meta's (which
        would read NULL and silently wipe the one non-derived fact this
        whole preservation path exists for)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir(); (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "c.sqlite"
            code_reindex(root, db, lang="python")   # a real v2 db, code_project.langs='python'
            conn = sqlite3.connect(str(db))
            conn.execute("UPDATE code_schema SET version=1")  # force the rebuild path
            conn.commit(); conn.close()
            conn = memidx.open_code_db(db)
            self.assertEqual(memidx.code_schema_version(conn), memidx.CODE_SCHEMA_VERSION)
            self.assertEqual(
                conn.execute("SELECT langs FROM code_project WHERE project=?", (memidx.DEFAULT_PROJECT,)).fetchone()[0],
                "python",
            )


# The v2 schema, captured verbatim from CODE_SCHEMA_SQL before this task
# bumps CODE_SCHEMA_VERSION to 3 -- the "older schema" input for the v2->v3
# rebuild red test (design R3/R4, audit MC-P1-02): a REAL v2 db, not just
# TestCodeSchemaV3.V1_DDL's hand-written v1 shape, must also survive.
CODE_SCHEMA_V2_DDL = """
CREATE TABLE IF NOT EXISTS code_project (
  project TEXT PRIMARY KEY,
  langs TEXT,
  embedding_mode TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL,
  project TEXT NOT NULL,
  code_root TEXT NOT NULL,
  lang TEXT NOT NULL,
  kind TEXT NOT NULL,
  symbol TEXT NOT NULL,
  qualified_name TEXT NOT NULL,
  signature TEXT,
  doc TEXT,
  start_line INTEGER NOT NULL,
  end_line INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_code_chunks_path ON chunks(project, path);
CREATE INDEX IF NOT EXISTS idx_code_chunks_qname ON chunks(project, qualified_name);
CREATE INDEX IF NOT EXISTS idx_code_chunks_root_path ON chunks(project, code_root, path);

CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
  qualified_name, split_tokens, signature, doc, body,
  tokenize = "unicode61 tokenchars '_'"
);

CREATE TABLE IF NOT EXISTS embeddings (
  chunk_id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS file_sha (
  path TEXT NOT NULL,
  project TEXT NOT NULL,
  code_root TEXT NOT NULL,
  sha256 TEXT,
  mtime REAL,
  size INTEGER,
  gap_count INTEGER NOT NULL DEFAULT 0,
  chunker_version TEXT,
  status TEXT NOT NULL DEFAULT 'ok',
  reason TEXT,
  attempt_key TEXT,
  PRIMARY KEY (project, code_root, path)
);

CREATE TABLE IF NOT EXISTS code_meta (
  project TEXT NOT NULL,
  code_root TEXT NOT NULL,
  last_indexed_at REAL,
  head_sha TEXT,
  PRIMARY KEY (project, code_root)
);

CREATE TABLE IF NOT EXISTS code_schema (
  version INTEGER NOT NULL
);
"""


def _copy_new_signals_into_file_sha(db_path, project, code_root, rel) -> None:
    """Test helper (design R3, audit MC-P1-02, red tests 4b/6): simulates a
    stat-identical `file_sha` row by copying the CURRENT file's five stat
    signals onto its own row -- ctime cannot be restored from userspace,
    so this is how a test proves the --verify-content/git-trigger
    fallback rather than the primary ctime/ino/dev signal. PRAGMA
    table_info-tolerant: against UNMODIFIED (pre-migration) code, file_sha
    has no mtime_ns/ctime_ns/ino/dev columns at all, so this only writes
    mtime/size there -- the red run then fails on the freshness VERDICT
    (`current` vs `metadata-current`), not on a missing-column error.

    Fix round 3: `code_root` is matched RESOLVED here -- cmd_code_reindex
    stores `Path(args.code_root).resolve()`, so on a platform where the
    caller's own root string sits behind a symlinked ancestor (macOS: a
    tempfile.TemporaryDirectory under /var/folders/..., itself a symlink
    to /private/var/folders/...) matching on the raw, unresolved string
    finds zero rows and silently no-ops -- the row is never actually
    changed, the test's whole premise (a stat-identical row) never lands,
    and the assertion below fails for a reason that has nothing to do
    with the freshness logic under test."""
    resolved_root = str(Path(code_root).resolve())
    f = Path(code_root) / rel
    st = f.stat()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(file_sha)")}
        sets = ["mtime=?", "size=?"]
        params = [st.st_mtime, st.st_size]
        for col, val in (
            ("mtime_ns", st.st_mtime_ns), ("ctime_ns", st.st_ctime_ns),
            ("ino", st.st_ino), ("dev", st.st_dev),
        ):
            if col in cols:
                sets.append(f"{col}=?")
                params.append(val)
        params.extend([project, resolved_root, rel])
        cur = conn.execute(
            f"UPDATE file_sha SET {', '.join(sets)} WHERE project=? AND code_root=? AND path=?",
            params,
        )
        assert cur.rowcount == 1, (
            f"fixture bug: expected exactly one file_sha row for "
            f"({project!r}, {resolved_root!r}, {rel!r}), updated {cur.rowcount}"
        )
        conn.commit()
    finally:
        conn.close()


def _rewrite_same_size(path: Path, old: str, new: str, *, restore_mtime_ns: bool = True) -> None:
    """Verify-report reproducer shape (audit MC-P1-02): replace `old` with
    `new` (same length) and, unless told otherwise, restore the exact
    `mtime_ns` afterwards."""
    st = path.stat()
    orig_ns = st.st_mtime_ns
    text = path.read_text()
    new_text = text.replace(old, new)
    assert new_text != text, "fixture bug: nothing to rewrite"
    assert len(new_text) == len(text), "fixture bug: rewrite must be same length"
    path.write_text(new_text)
    if restore_mtime_ns:
        os.utime(path, ns=(orig_ns, orig_ns))


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git_commit_all(root, "init")


def _git_commit_all(root: Path, msg: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t.t", "-c", "user.name=t", "add", "-A"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t.t", "-c", "user.name=t",
         "commit", "-q", "-m", msg],
        check=True,
    )


class TestContentProvenFreshness(unittest.TestCase):
    """audit MC-P1-02 / design R3 (TOP-0123 L3), code side: stat signals
    (size/mtime_ns/ctime_ns/ino/dev), the git trigger, --verify-content,
    and the current/metadata-current split. Reproducer shape from the
    verify report: replace `alpha` with `bravo` (same length) in a source
    file, then restore the exact `mtime_ns` (ctime cannot be restored from
    userspace at all -- the "stat-identical row" variants below simulate
    it instead via _copy_new_signals_into_file_sha, proving the
    --verify-content/git-trigger fallback rather than the ctime/ino/dev
    signal)."""

    def _one_file_root(self, td, name="a.py", body="def alpha():\n    return 1\n"):
        root = Path(td) / "code"
        root.mkdir()
        (root / name).write_text(body)
        return root, root / name

    def test_same_size_rewrite_with_restored_mtime_ns_is_stale_via_ctime(self):
        """Variant a: only mtime_ns is restored -- ctime moves on write
        regardless (userspace cannot fake it), so the stat-signal set
        alone must catch this without any git trigger or --verify-content."""
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")

            _rewrite_same_size(p, "alpha", "bravo")

            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(rep["state"], "stale", rep)

    def test_stat_identical_row_reads_metadata_current_and_serves_the_old_symbol(self):
        """Variant b: every one of the five stat signals is made to match
        (simulated). The default (no --verify-content) report must NOT
        claim `current`; it reads `metadata-current` and the stale symbol
        is still what a search answers by default. --verify-content
        hashes anyway, proves `stale`, heals, and the new symbol is found."""
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")

            _rewrite_same_size(p, "alpha", "bravo")
            _copy_new_signals_into_file_sha(db, memidx.DEFAULT_PROJECT, root, "a.py")

            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(rep["state"], "metadata-current", rep)

            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(io.StringIO()):
                memidx.cmd_code_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query="alpha", mode="fts",
                    limit=10, json=True, decision_db=None, no_heal=True,
                ))
            data = json.loads(out_buf.getvalue())
            self.assertTrue(any(h["qualified_name"] == "alpha" for h in data["results"]), data)

            out_buf2 = io.StringIO()
            with contextlib.redirect_stdout(out_buf2), contextlib.redirect_stderr(io.StringIO()):
                memidx.cmd_code_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query="bravo", mode="fts",
                    limit=10, json=True, decision_db=None, no_heal=False, verify_content=True,
                ))
            data2 = json.loads(out_buf2.getvalue())
            self.assertEqual(data2["state"], "current", data2)
            self.assertTrue(any(h["qualified_name"] == "bravo" for h in data2["results"]), data2)

    def test_touch_is_metadata_current_no_rechunk_no_reembed(self):
        """5 (brief note): the no-rechunk/no-reembed mechanism for a bare
        touch predates this task, so only the state is checked loosely
        here (not "stale") -- the exact "metadata-current" vocabulary is
        pinned by test_verify_content_proves_current_default_is_metadata_current
        instead."""
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")

            conn = memidx.open_code_db(db)
            chunk_id_before = conn.execute(
                "SELECT id FROM chunks WHERE path=?", ("a.py",)
            ).fetchone()["id"]
            conn.close()

            os.utime(p, (time.time() + 3600, time.time() + 3600))

            def _boom(*a, **kw):
                raise AssertionError("compute_embeddings must not be called for a bare touch")

            with mock.patch.object(memidx, "compute_embeddings", side_effect=_boom):
                code_reindex(root, db, no_embed=True, lang="python")

            conn = memidx.open_code_db(db)
            chunk_id_after = conn.execute(
                "SELECT id FROM chunks WHERE path=?", ("a.py",)
            ).fetchone()["id"]
            emb_count = conn.execute("SELECT COUNT(*) AS c FROM embeddings").fetchone()["c"]
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(chunk_id_before, chunk_id_after)
            self.assertEqual(emb_count, 0)
            self.assertNotEqual(rep["state"], "stale", rep)

    def test_git_trigger_catches_a_stat_identical_commit(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            _init_git_repo(root)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")

            _rewrite_same_size(p, "alpha", "bravo")
            _copy_new_signals_into_file_sha(db, memidx.DEFAULT_PROJECT, root, "a.py")
            _git_commit_all(root, "rewrite alpha to bravo")

            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(rep["state"], "stale", rep)
            self.assertEqual(rep["roots"][0]["git_delta"], "changed", rep)

    def test_git_trigger_does_not_double_count_a_new_committed_file(self):
        """A brand-new file is already counted `changed` by the stat pass
        (`prev is None`, never hashed there); the git trigger's diff also
        names it (a new file IS a diffed path) -- it must not re-count the
        same file a second time just because it wasn't in `hashed`."""
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            _init_git_repo(root)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")

            (root / "b.py").write_text("def bravo():\n    return 2\n")
            _git_commit_all(root, "add b.py")

            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(rep["state"], "stale", rep)
            self.assertEqual(rep["changed"], 1, rep)
            self.assertEqual(rep["roots"][0]["git_delta"], "changed", rep)

    def test_git_trigger_skips_a_not_indexed_row_without_double_counting(self):
        """task-3-review LOW #4: a not-indexed row (sha256 IS NULL) a
        commit's diff touches must not be re-counted -- the retry rule
        (cmd_code_reindex's own attempt_key/chunker_version check) owns
        it, never the git trigger. `_root_report`'s diff loop has this
        exact skip (`if prev is not None and prev["sha256"] is None:
        continue`); this is its dedicated red/green test."""
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            _init_git_repo(root)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")

            # Simulate a not-indexed row directly -- status/sha as
            # cmd_code_reindex's own failure path would stamp them,
            # chunker_version left untouched so the stat pass's OWN
            # chunker-version check (which runs before the sha256-IS-NULL
            # skip) does not itself count this row as changed.
            # Fix round 3: match on the RESOLVED code_root, the same value
            # cmd_code_reindex actually stored (Path(args.code_root)
            # .resolve()) -- matching on the raw `root` string finds zero
            # rows on a platform where it sits behind a symlinked ancestor
            # (macOS's /var/folders/... vs /private/var/folders/...),
            # silently no-oping the simulated not-indexed row and leaving
            # the git trigger to see a genuinely-indexed file instead.
            conn = sqlite3.connect(str(db))
            cur = conn.execute(
                "UPDATE file_sha SET status='not-indexed', sha256=NULL "
                "WHERE project=? AND code_root=? AND path=?",
                (memidx.DEFAULT_PROJECT, str(root.resolve()), "a.py"),
            )
            assert cur.rowcount == 1, (
                f"fixture bug: expected exactly one file_sha row, updated {cur.rowcount}"
            )
            conn.commit()
            conn.close()

            # Touch and commit the SAME not-indexed path -- the commit's
            # diff names it, but it must not be re-counted as changed.
            p.write_text("def alpha():\n    return 2\n")
            _git_commit_all(root, "edit a.py again")

            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(rep["changed"], 0, rep)
            self.assertEqual(rep["roots"][0]["git_delta"], "verified", rep)

    def test_head_moved_on_a_non_source_file_still_verifies_and_refreshes(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            _init_git_repo(root)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")
            conn = memidx.open_code_db(db)
            head_before = conn.execute(
                "SELECT head_sha FROM code_meta WHERE project=?", (memidx.DEFAULT_PROJECT,)
            ).fetchone()["head_sha"]
            conn.close()

            (root / "README.md").write_text("notes\n")
            _git_commit_all(root, "add readme")

            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            head_after = conn.execute(
                "SELECT head_sha FROM code_meta WHERE project=?", (memidx.DEFAULT_PROJECT,)
            ).fetchone()["head_sha"]
            conn.close()
            self.assertEqual(rep["state"], "metadata-current", rep)
            self.assertEqual(rep["roots"][0]["git_delta"], "verified", rep)
            self.assertNotEqual(head_before, head_after)

    def test_non_git_root_git_delta_is_unavailable(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")
            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(rep["roots"][0]["git_delta"], "unavailable", rep)
            self.assertEqual(rep["state"], "metadata-current", rep)

    def test_verify_content_proves_current_default_is_metadata_current(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")

            conn = memidx.open_code_db(db)
            rep_default = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            rep_verified = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT, verify_content=True)
            conn.close()
            self.assertEqual(rep_default["state"], "metadata-current", rep_default)
            self.assertEqual(rep_verified["state"], "current", rep_verified)

            out_buf = io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(io.StringIO()):
                memidx.cmd_code_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query="alpha", mode="fts",
                    limit=10, json=True, decision_db=None, no_heal=True, verify_content=True,
                ))
            data = json.loads(out_buf.getvalue())
            self.assertEqual(data["state"], "current", data)

    def test_why_fast_path_trusts_metadata_current_and_distrusts_stale(self):
        """7 (brief note): _resolve_symbol_via_code_index's own gate
        (`report["state"] == "stale"`) is unchanged code -- "metadata-
        current" is simply not "stale", so the fast path already trusts
        it; this proves that and the stale-bypass side by side."""
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            project = "why-fastpath-freshness"
            home = Path(td) / "home"
            home.mkdir()
            prev_home = os.environ.get("MEMCONTINUUM_HOME")
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                code_args = ns(
                    project=project, code_root=str(root), db=None,
                    no_embed=True, full=False, lang="python",
                )
                self.assertEqual(memidx.cmd_code_reindex(code_args), 0)

                with mock.patch.object(memidx, "fragment_declared_in_text", return_value=False):
                    resolved = memidx.resolve_symbol_to_path(root, "alpha", project=project)
                self.assertEqual(resolved, "a.py")

                # a real content + mtime change -- genuine stale drift.
                p.write_text("def alpha():\n    return 2\n")
                os.utime(p, (time.time() + 3600, time.time() + 3600))
                with mock.patch.object(memidx, "fragment_declared_in_text", return_value=False):
                    resolved_stale = memidx.resolve_symbol_to_path(root, "alpha", project=project)
                self.assertIsNone(
                    resolved_stale,
                    "the fast path must not trust a stale index (fragment fallback disabled)",
                )
            finally:
                if prev_home is None:
                    os.environ.pop("MEMCONTINUUM_HOME", None)
                else:
                    os.environ["MEMCONTINUUM_HOME"] = prev_home

    def test_v2_db_rebuilds_to_v3_preserving_roots_and_langs(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "c.sqlite"
            conn = sqlite3.connect(str(db))
            conn.executescript(CODE_SCHEMA_V2_DDL)
            conn.execute(
                "INSERT INTO code_project (project, langs, embedding_mode) VALUES ('p','python','none')"
            )
            conn.execute(
                "INSERT INTO code_meta (project, code_root, last_indexed_at, head_sha) "
                "VALUES ('p','/old/root',0,'abc')"
            )
            conn.execute("INSERT INTO code_schema (version) VALUES (2)")
            conn.commit()
            conn.close()

            conn = memidx.open_code_db(db)
            self.assertEqual(memidx.code_schema_version(conn), 3)
            self.assertEqual(
                conn.execute("SELECT project, code_root FROM code_meta").fetchall()[0][:],
                ("p", "/old/root"),
            )
            self.assertEqual(
                conn.execute("SELECT langs FROM code_project").fetchone()[0], "python",
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0], 0)
            conn.close()

    def test_v1_fixture_still_rebuilds_to_v3(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "c.sqlite"
            conn = sqlite3.connect(str(db))
            conn.executescript(TestCodeSchemaV3.V1_DDL)
            conn.commit()
            conn.close()
            conn = memidx.open_code_db(db)
            self.assertEqual(memidx.code_schema_version(conn), 3)
            conn.close()


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
            conn = sqlite3.connect(str(v1)); conn.executescript(TestCodeSchemaV3.V1_DDL); conn.commit(); conn.close()
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
            self.assertEqual(mode, "partial"); self.assertEqual(chunks, 2); self.assertEqual(embedded, 1)
            self.assertIn("embedding mode set to partial", buf.getvalue())
            self.assertIn("1 chunk(s) awaiting embedding", buf.getvalue())
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

    def test_embedded_run_on_one_root_never_claims_full_while_another_lacks_vectors(self):
        """Fix-wave item 1 (CRITICAL): an embedded run over root A must
        derive `full` from PROJECT-wide coverage, not just "this run wasn't
        --no-embed". Grok's sequence: A embedded (full) -> B --no-embed
        (none, since B's new chunks have no vectors) -> A embedded AGAIN
        with no edits at all (mutated == False for this run) must STILL
        read none -- A's own chunk already has a vector, so the buggy
        unconditional "not --no-embed -> full" line used to relapse to
        full here even though B's chunk still has none."""
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python", no_embed=False)          # A: full (1 chunk, 1 vector)
            self.assertEqual(self._coverage(db), ("full", 1, 1))

            buf1 = io.StringIO()
            with contextlib.redirect_stdout(buf1):
                code_reindex(b, db, lang="python", no_embed=True)       # B: no-embed -> downgrades to partial
            self.assertEqual(self._coverage(db), ("partial", 2, 1))
            self.assertIn("embedding mode set to partial", buf1.getvalue())

            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                code_reindex(a, db, lang="python", no_embed=False)      # A again, embedded, NO edits
            mode, chunks, embedded = self._coverage(db)
            self.assertEqual((mode, chunks, embedded), ("partial", 2, 1))  # still partial: B's chunk is still bare
            self.assertTrue(buf2.getvalue().rstrip().endswith("1 chunk(s) awaiting embedding"), buf2.getvalue())

            buf3 = io.StringIO()
            with contextlib.redirect_stdout(buf3):
                code_reindex(b, db, lang="python", no_embed=False)      # B embedded too -> now genuinely full
            self.assertEqual(self._coverage(db), ("full", 2, 2))
            self.assertTrue(buf3.getvalue().rstrip().endswith("0 chunk(s) awaiting embedding"), buf3.getvalue())

    def test_no_embed_root_then_embedded_root_then_no_embed_original_stays_full(self):
        """The other order from finding 1: root A --no-embed, root B
        embedded -> project-wide coverage is still incomplete (A's chunk
        has no vector) so mode reads partial + counts it as awaiting; embedding
        A afterwards makes coverage complete -> full."""
        with tempfile.TemporaryDirectory() as td:
            a, b, db = self._mk(td)
            code_reindex(a, db, lang="python", no_embed=True)
            self.assertEqual(self._coverage(db)[0], "none")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code_reindex(b, db, lang="python", no_embed=False)      # B embedded, A still bare
            self.assertEqual(self._coverage(db), ("partial", 2, 1))
            self.assertTrue(buf.getvalue().rstrip().endswith("1 chunk(s) awaiting embedding"), buf.getvalue())
            code_reindex(a, db, lang="python", no_embed=False)          # A embedded -> now full
            self.assertEqual(self._coverage(db), ("full", 2, 2))

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


class TestNestedWorktreeExcludedFromCodeWalk(unittest.TestCase):
    """Worktree-gap round 2 (Codex's "code index check" instruction, fix
    round 2 report): a `git worktree add` checkout made INSIDE a
    configured code root (e.g. `<root>/.worktrees/feat/`) is now the
    layout hooks/mc-path-lib.sh's mc_remap_worktree_path gives retrieval
    and ledger coverage to -- but its own tracked-file content is a
    DIFFERENT git checkout than `root`'s own, and must never be silently
    indexed as if it belonged to `root`. `iter_code_files`,
    `iter_code_source_files` (what `code-reindex` actually walks), and
    `code_census` all share `_prune_dirnames_for_code_walk`, which prunes
    any directory whose own top holds a `.git` FILE (never a directory --
    an ordinary checkout's `.git` stays walked as before)."""

    @staticmethod
    def _make_nested_worktree(root):
        wt = root / ".worktrees" / "feat"
        (wt / "src").mkdir(parents=True)
        (wt / "src" / "other.py").write_text("def other():\n    return 2\n")
        # A real `git worktree add` leaves a `.git` FILE (not directory)
        # at the worktree's own top, containing a `gitdir: ...` pointer.
        # The exclusion is keyed on that FILE, never on the "worktrees"
        # directory name itself, so a plain text file reproduces it
        # without needing a real git repo for this unit-level test.
        (wt / ".git").write_text("gitdir: /nonexistent/main/.git/worktrees/feat\n")
        return wt

    def test_iter_code_files_excludes_nested_worktree_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            self._make_nested_worktree(root)

            paths = {p.relative_to(root) for p in memidx.iter_code_files(root)}
            self.assertIn(Path("basic_functions.py"), paths)
            self.assertFalse(
                any(".worktrees" in p.parts for p in paths),
                f"nested worktree content must be excluded, got: {paths}",
            )

    def test_code_reindex_excludes_nested_worktree_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            self._make_nested_worktree(root)
            db = Path(td) / "idx-code.sqlite"

            rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0)

            conn = memidx.open_code_db(db)
            paths = {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
            conn.close()
            self.assertIn("basic_functions.py", paths)
            self.assertFalse(
                any(".worktrees" in Path(p).parts for p in paths),
                f"nested worktree content must not be chunked/indexed, got: {paths}",
            )

    def test_code_census_excludes_nested_worktree_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            self._make_nested_worktree(root)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["code-census", "--root", str(root), "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            # basic_functions.py counted; the nested worktree's other.py
            # must not inflate the same python count.
            self.assertEqual(data["python"], {"files": 1, "status": "supported"})

    def test_ordinary_dot_git_directory_still_pruned_as_before(self):
        """The exclusion is `.git`-FILE-specific; an ordinary checkout's
        own `.git` DIRECTORY (the pre-existing, unrelated
        chunkers.UNIVERSAL_SKIP_DIRS rule) must keep being pruned too --
        this is a regression guard, not new behavior."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(PY_FIXTURES / "basic_functions.py", root / "basic_functions.py")
            git_dir = root / ".git" / "objects"
            git_dir.mkdir(parents=True)
            (root / ".git" / "stray.py").write_text("def stray():\n    return 1\n")

            paths = {p.relative_to(root) for p in memidx.iter_code_files(root)}
            self.assertIn(Path("basic_functions.py"), paths)
            self.assertFalse(any(".git" in p.parts for p in paths))


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
            self.assertEqual(counts["php"], {"files": 1, "status": "supported"})
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
                self.assertRegex(out2.getvalue(), r"\b0 not indexed\b")  # not "10 not indexed" etc.
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
                self.assertRegex(out.getvalue(), r"\b0 not indexed\b")  # not "10 not indexed" etc.
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
                # design R3 (audit MC-P1-02): no --verify-content here, so
                # a clean report reads "metadata-current", not "current".
                self.assertEqual(state_before, "metadata-current")

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
            # design R3 (audit MC-P1-02): no --verify-content -- a clean
            # reindex+search reads "metadata-current", not "current".
            "assert env['state'] == 'metadata-current', env['state']\n"
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
            # design R3 (audit MC-P1-02): touch is bookkeeping-refreshed,
            # not a change -- but no --verify-content, so "metadata-current".
            self.assertEqual(memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"], "metadata-current"); conn.close()
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
            # design R3 (audit MC-P1-02): a failed-only report still reads
            # "metadata-current" (not "degraded") with no --verify-content.
            self.assertEqual(env["state"], "metadata-current"); self.assertEqual(env["failed"], 1); self.assertEqual(env["not_indexed"], 0)
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
            # design R3 (audit MC-P1-02): a heal succeeds into
            # "metadata-current" without --verify-content -- "index healed"
            # prints for either honest post-heal state, this run asked for
            # neither proof.
            self.assertEqual(env["state"], "metadata-current")
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

    def test_heal_of_an_unembedded_project_adds_no_vectors(self):
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

    def test_metadata_current_index_json_carries_indexed_at_and_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"
            root.mkdir()
            shutil.copy(FIXTURES / "NestedTypes.swift", root / "NestedTypes.swift")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True)

            result = self._run(["code-search", "--db", str(db), "outer func", "--mode", "fts", "--json"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = json.loads(result.stdout)
            # design R3 (audit MC-P1-02): no --verify-content -- a clean
            # reindex+search reads "metadata-current", not "current".
            self.assertEqual(data["state"], "metadata-current")
            # code_reindex canonicalises via Path(...).resolve() (memidx.py's
            # cmd_reindex), so the stored/reported code_root is the RESOLVED
            # path -- on macOS, root itself (built from tempfile's TMPDIR) is
            # the raw /var/folders/... form, a symlink to /private/var/folders/...
            # Comparing against the raw root was a Linux-only assumption: /tmp
            # is not usually symlinked there, so raw == resolved by coincidence.
            self.assertEqual(data["code_root"], str(root.resolve()))
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
            k = memidx.CODE_RRF_K
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
        non-zero, instead.

        F1 (ruling 68): `why` now refuses on a wholly missing decision db
        (never silently creates one), so this test's own throwaway decision
        store needs one real (empty-root) reindex first -- unrelated to the
        bare-symbol/code-index fast path under test, which never touches
        this db until the decision-store lookup step."""
        decisions_db = Path(self._tmpdir.name) / "decisions.sqlite"
        empty_root = Path(self._tmpdir.name) / "empty-decisions-root"
        empty_root.mkdir(exist_ok=True)
        reindex_args = ns(root=str(empty_root), project=self.PROJECT, db=str(decisions_db),
                           full=False, no_embed=True)
        rc = memidx.cmd_reindex(reindex_args)
        self.assertEqual(rc, 0)

        args = ns(
            symbol_or_path="parse_frontmatter",
            code_root=str(TOOLS_DIR),
            project=self.PROJECT,
            db=str(decisions_db),
            json=False,
        )
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = memidx.cmd_why(args)
        self.assertEqual(rc, 0, buf.getvalue() + err.getvalue())
        self.assertIn("memidx.py", buf.getvalue(), buf.getvalue())


class TestWhyNeverRebuildsAnOlderIndex(unittest.TestCase):
    """Fix-wave item 3: _resolve_symbol_via_code_index must never call
    open_code_db on a db whose schema is older than CODE_SCHEMA_VERSION --
    that call performs the v1->v2 rebuild as a SIDE EFFECT of opening, and
    `why` gets exactly one shot per call (it never heals, see that
    function's own docstring). If the fast path were the thing that
    triggered the rebuild, the first bare-symbol `why` right after an
    engine upgrade would silently empty the whole index and leave it
    stale until the next code-search happened to run -- worse than simply
    missing the fast path once. The fix reads the schema version first
    with a throwaway, read-only connection and returns None (sending the
    caller to the disk-scan fallback) without ever opening the db for
    real when the version is behind."""

    def test_v1_db_is_left_untouched_and_lookup_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            project = "why-v1-guard"
            db_path = home / f"{project}-code.sqlite"
            conn = sqlite3.connect(str(db_path))
            conn.executescript(TestCodeSchemaV3.V1_DDL)
            conn.commit()
            conn.close()

            prev_home = os.environ.get("MEMCONTINUUM_HOME")
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                result = memidx._resolve_symbol_via_code_index(Path("/old/root"), "f", project)
            finally:
                if prev_home is None:
                    os.environ.pop("MEMCONTINUUM_HOME", None)
                else:
                    os.environ["MEMCONTINUUM_HOME"] = prev_home

            self.assertIsNone(result)
            conn = sqlite3.connect(str(db_path))
            still_v1 = not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_schema'"
            ).fetchone()
            chunk_count = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            conn.close()
            self.assertTrue(still_v1, "why must not trigger the v1->v2 rebuild")
            self.assertEqual(chunk_count, 1)  # the V1_DDL fixture row, untouched

    def test_corrupt_db_never_crashes_the_lookup(self):
        """Regression guard on the version pre-check itself: sqlite3.connect
        is lazy and never raises on a non-database file -- the DatabaseError
        only surfaces on the first real read (code_schema_version's own
        query). That read must stay inside the same try/except as the
        connect, or a corrupt db crashes `why` exactly like the bug this
        whole function exists to prevent, instead of sending the caller to
        the disk-scan fallback like every other unreadable-db case here."""
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            project = "why-corrupt-db-guard"
            db_path = home / f"{project}-code.sqlite"
            db_path.write_bytes(b"not a sqlite database at all")

            prev_home = os.environ.get("MEMCONTINUUM_HOME")
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                result = memidx._resolve_symbol_via_code_index(Path("/old/root"), "f", project)
            finally:
                if prev_home is None:
                    os.environ.pop("MEMCONTINUUM_HOME", None)
                else:
                    os.environ["MEMCONTINUUM_HOME"] = prev_home
            self.assertIsNone(result)


class TestHealIgnoresMissingRootAvailability(unittest.TestCase):
    """Fix-wave item 4 (Grok G2): a MISSING root's availability_changed
    flag must not make the project eligible for heal forever -- heal
    skips exists=False roots outright, so nothing there ever actually
    gets fixed; counting a missing root's stale not-indexed rows into the
    project-wide aggregate just re-triggers "eligible" (and a no-op
    reindex pass) on every single search."""

    def _run(self, db, q, *extra):
        script = ("import sys; sys.path.insert(0, %r); import memidx; "
                  "memidx.main(['code-search', '--db', %r, %r, '--mode', 'fts'] + %r)"
                  ) % (str(TOOLS_DIR), str(db), q, list(extra))
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)

    def test_missing_root_with_stale_not_indexed_row_never_heals_and_stays_degraded(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"; b = Path(td) / "b"; a.mkdir(); b.mkdir()
            (a / "x.py").write_text("def fx():\n    pass\n")
            (b / "y.py").write_text("def fy():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(a, db, lang="python")
            with mock.patch.multiple(
                chunkers,
                get_chunker=lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("m")),
                backend_availability=lambda: "python=missing;swift=ok",
            ):
                code_reindex(b, db, lang="python")   # b/y.py: not-indexed, stale attempt_key
            shutil.rmtree(b)                          # root b now missing entirely

            def _root_a_last_indexed_at():
                conn = memidx.open_code_db(db)
                row = conn.execute(
                    "SELECT last_indexed_at FROM code_meta WHERE code_root=?", (str(a.resolve()),)
                ).fetchone()
                conn.close()
                return row[0]

            # A missing root's stale not-indexed row must not make root A
            # (present, unchanged, nothing to fix) eligible for a heal --
            # the buggy version reindexed A on every single search anyway
            # (last_indexed_at kept moving forward), even though nothing
            # about A ever changed and heal_code_index's own loop SKIPS
            # exists=False roots (so B, the actual not-indexed row, is
            # never touched either way).
            before = _root_a_last_indexed_at()
            r1 = self._run(db, "fx")
            self.assertNotIn("index healed", r1.stderr, r1.stderr)
            after1 = _root_a_last_indexed_at()
            self.assertEqual(before, after1, "root A must not be spuriously re-reindexed by heal")

            r2 = self._run(db, "fx")
            self.assertNotIn("index healed", r2.stderr, r2.stderr)
            after2 = _root_a_last_indexed_at()
            self.assertEqual(before, after2)

            conn = memidx.open_code_db(db)
            self.assertEqual(memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)["state"], "degraded")
            conn.close()

    def test_missing_root_never_reports_verified_true_under_verify_content(self):
        """task-3-review MODERATE #3: a missing root must not read
        verified: True even under --verify-content -- nothing was hashed
        for a root that doesn't exist, so `_root_report`'s early-return
        branch must set verified back to False despite verify_content=True
        being requested for the whole call."""
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"; b = Path(td) / "b"; a.mkdir(); b.mkdir()
            (a / "x.py").write_text("def fx():\n    pass\n")
            (b / "y.py").write_text("def fy():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(a, db, lang="python")
            code_reindex(b, db, lang="python")
            shutil.rmtree(b)  # root b now missing entirely

            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT, verify_content=True)
            conn.close()
            missing = [r for r in report["roots"] if not r["exists"]]
            self.assertEqual(len(missing), 1, report["roots"])
            self.assertFalse(missing[0]["verified"], missing[0])
            # the present root really was proven, so the aggregate state
            # must not read this test as a false negative on the fix itself
            present = [r for r in report["roots"] if r["exists"]]
            self.assertTrue(present[0]["verified"], present[0])


class TestHealLimitAndFailurePathCleanup(unittest.TestCase):
    """Fix-wave items 6 and 7: the heal-limit refusal names the report's
    actual state (not a hardcoded "stale"), and a heal failure after the
    post-reindex reopen closes that connection before opening a third one
    (no leaked sqlite3.Connection)."""

    def test_heal_limit_refusal_names_degraded_not_stale(self):
        """A `degraded` report (not_indexed-driven, changed == 0) never
        actually hits the >limit branch -- degraded implies changed == 0 --
        so this proves the WORDING fix directly against the report dict
        heal_code_index reads, independent of whether --heal-limit can be
        tuned low enough to reach it in practice."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with mock.patch.multiple(
                chunkers,
                get_chunker=lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("m")),
                backend_availability=lambda: "python=missing;swift=ok",
            ):
                code_reindex(root, db, lang="python")
            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            self.assertEqual(report["state"], "degraded")
            report = dict(report, changed=1)  # force the >limit branch open regardless of shape
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                memidx.heal_code_index(conn, db, memidx.DEFAULT_PROJECT, report, limit=0)
            self.assertIn("index is degraded", buf.getvalue())
            self.assertNotIn("index is stale", buf.getvalue())

    def test_heal_failure_after_reopen_does_not_leak_the_reopened_connection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            p = root / "x.py"; p.write_text("def old_name():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")
            p.write_text("def new_name():\n    pass\n")
            os.utime(p, (time.time() + 3600,) * 2)
            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            self.assertEqual(report["state"], "stale")

            real_report = memidx.code_index_report
            seen_conns = []

            def spying_report(c, project, **kwargs):
                # design R3 (audit MC-P1-02): heal_code_index now calls
                # code_index_report with verify_content= -- accept it (and
                # any future keyword) so this mock's signature can't itself
                # become the TypeError the except-block below swallows,
                # silently changing what this test proves.
                seen_conns.append(c)
                if len(seen_conns) == 1:
                    raise RuntimeError("boom after reopen")
                return real_report(c, project, **kwargs)

            memidx.code_index_report = spying_report
            try:
                buf = io.StringIO()
                with contextlib.redirect_stderr(buf):
                    result_report, result_conn = memidx.heal_code_index(
                        conn, db, memidx.DEFAULT_PROJECT, report, limit=500
                    )
            finally:
                memidx.code_index_report = real_report

            self.assertIn("heal failed", buf.getvalue())
            self.assertEqual(len(seen_conns), 1)  # the reopened conn that raised
            reopened = seen_conns[0]
            with self.assertRaises(sqlite3.ProgrammingError):
                reopened.execute("SELECT 1")       # closed by the except handler, not leaked
            result_conn.execute("SELECT 1")         # the returned connection is still live
            result_conn.close()


class TestCodeIndexTooNew(unittest.TestCase):
    """Fix-wave item 9: a db whose code_schema.version is GREATER than
    this engine's CODE_SCHEMA_VERSION was written by a newer engine -- it
    must never be silently used (an older engine guessing at a newer
    schema's meaning corrupts data quietly) and never silently rebuilt
    (that would destroy the newer engine's index). open_code_db refuses it
    outright; code-search and why fail open around that refusal."""

    def _mk_too_new_db(self, td):
        db = Path(td) / "idx-code.sqlite"
        conn = memidx.open_code_db(db)   # normal v2 db
        conn.execute("UPDATE code_schema SET version=?", (memidx.CODE_SCHEMA_VERSION + 1,))
        conn.commit()
        conn.close()
        return db

    def test_open_code_db_refuses_a_newer_schema(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._mk_too_new_db(td)
            with self.assertRaises(memidx.CodeIndexTooNew) as ctx:
                memidx.open_code_db(db)
            msg = str(ctx.exception)
            self.assertIn(str(memidx.CODE_SCHEMA_VERSION + 1), msg)
            self.assertIn(str(memidx.CODE_SCHEMA_VERSION), msg)
            self.assertIsInstance(ctx.exception, sqlite3.DatabaseError)

    def test_code_search_fails_open_on_a_newer_schema_db(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._mk_too_new_db(td)
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = memidx.cmd_code_search(
                    ns(db=str(db), query="anything", mode="fts", limit=5, json=False)
                )
            self.assertEqual(rc, 0, buf.getvalue() + err.getvalue())
            self.assertIn("newer engine", err.getvalue())

    def test_why_falls_back_to_disk_scan_on_a_newer_schema_db(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"; home.mkdir()
            project = "too-new-guard"
            root = Path(td) / "code"; root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            db_path = home / f"{project}-code.sqlite"
            conn = memidx.open_code_db(db_path)
            conn.execute("UPDATE code_schema SET version=?", (memidx.CODE_SCHEMA_VERSION + 1,))
            conn.commit()
            conn.close()

            prev_home = os.environ.get("MEMCONTINUUM_HOME")
            os.environ["MEMCONTINUUM_HOME"] = str(home)
            try:
                resolved = memidx.resolve_symbol_to_path(root, "f", project=project)
            finally:
                if prev_home is None:
                    os.environ.pop("MEMCONTINUUM_HOME", None)
                else:
                    os.environ["MEMCONTINUUM_HOME"] = prev_home
            self.assertEqual(resolved, "x.py")  # disk-scan fallback, never a crash

    def _version_and_table_names(self, db):
        conn = sqlite3.connect(str(db))
        version = conn.execute("SELECT version FROM code_schema").fetchone()[0]
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        conn.close()
        return version, tables

    def test_code_reindex_refuses_a_newer_schema_db_without_a_traceback(self):
        """Coordinator ruling (fix-wave follow-up): cmd_code_reindex's own
        open_code_db call must catch CodeIndexTooNew too -- code-search/why
        degrade with rc=0, but code-reindex has real work it could
        otherwise start, so it fails CLOSED (rc=1, same message, db
        untouched) rather than letting the exception traceback all the way
        out (repo-init turns rc 1 into its own exit 13, with the captured
        stderr -- a raw traceback there is not that)."""
        with tempfile.TemporaryDirectory() as td:
            db = self._mk_too_new_db(td)
            before_version, before_tables = self._version_and_table_names(db)
            root = Path(td) / "code"; root.mkdir(); (root / "x.py").write_text("def f():\n    pass\n")

            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = memidx.cmd_code_reindex(ns(
                    code_root=str(root), drop_root=None, db=str(db), project=memidx.DEFAULT_PROJECT,
                    no_embed=True, full=False, lang="python",
                ))
            self.assertEqual(rc, 1, buf.getvalue() + err.getvalue())
            self.assertIn("newer engine", err.getvalue())

            after_version, after_tables = self._version_and_table_names(db)
            self.assertEqual(before_version, after_version)   # not silently rebuilt down
            self.assertEqual(before_tables, after_tables)     # nothing dropped, nothing added

    def test_code_reindex_drop_root_refuses_a_newer_schema_db_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._mk_too_new_db(td)
            before_version, before_tables = self._version_and_table_names(db)

            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                rc = memidx.cmd_code_reindex(ns(
                    code_root=None, drop_root=str(Path(td) / "some-root"), db=str(db),
                    project=memidx.DEFAULT_PROJECT, no_embed=True, full=False, lang=None,
                ))
            self.assertEqual(rc, 1, buf.getvalue() + err.getvalue())
            self.assertIn("newer engine", err.getvalue())

            after_version, after_tables = self._version_and_table_names(db)
            self.assertEqual(before_version, after_version)
            self.assertEqual(before_tables, after_tables)


class TestBackendPreflight(unittest.TestCase):
    def test_json_reports_every_row_ok_or_missing_with_reason(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = memidx.main(["backend-preflight", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("python", data)
        self.assertTrue(data["python"]["ok"])
        self.assertIsNone(data["python"]["reason"])

    @unittest.skipUnless(os.environ.get("MEMCONTINUUM_PYTHON", ""), "needs the fixed venv with the seven pins installed")
    def test_reports_a_positive_ok_true_for_a_real_installed_tree_sitter_row(self):
        # Pre-flight finding #1/table-2 gap: neither of the two original
        # tests here ever asserted ok:true for an actually-installed
        # tree-sitter row -- both checked only python (native) and
        # lua-with-the-wheel-mocked-out (negative). Against the real,
        # unmocked wheels Task 1's coordinator step installed, EVERY
        # tree-sitter row (all seven, ts/tsx included) must report ok:true.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = memidx.main(["backend-preflight", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        for lang in ("javascript", "typescript", "tsx", "java", "php", "rust", "lua"):
            self.assertTrue(data[lang]["ok"], f"{lang}: expected ok:true, got {data[lang]}")
            self.assertIsNone(data[lang]["reason"])

    def test_missing_wheel_is_reported_by_name_not_crashed(self):
        chunkers.treesitter.reset_cache()
        with mock.patch.dict(sys.modules, {"tree_sitter_lua": None}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["backend-preflight", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
        chunkers.treesitter.reset_cache()
        self.assertFalse(data["lua"]["ok"])
        self.assertEqual(data["lua"]["state"], "missing")
        self.assertIsNotNone(data["lua"]["reason"])

    @unittest.skipUnless(os.environ.get("MEMCONTINUUM_PYTHON", ""),
                         "needs the fixed venv with the seven pins installed")
    def test_an_installed_version_off_the_pin_is_reported_as_pin_mismatch(self):
        """Ruling 108: a wheel that imports but sits at a version the row
        does not pin is a THIRD state beside ok and missing. The backend
        runs, so `ok` stays true and the exit code stays 0; what it produces
        is not what the pins describe, so the report names the distribution
        and both versions rather than saying `ok`."""
        real = importlib.metadata.version

        def fake(name):
            return "9.9.9" if name == "tree-sitter-lua" else real(name)

        chunkers.treesitter.reset_cache()
        with mock.patch.object(importlib.metadata, "version", fake):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.main(["backend-preflight", "--json"])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            text = io.StringIO()
            with contextlib.redirect_stdout(text):
                memidx.main(["backend-preflight"])
        chunkers.treesitter.reset_cache()
        self.assertEqual(data["lua"]["state"], "pin-mismatch")
        self.assertTrue(data["lua"]["ok"], "a row off its pins still runs")
        self.assertIn("9.9.9", data["lua"]["reason"])
        self.assertIn(chunkers.LANGUAGE_TABLE["lua"]["grammar_pin"], data["lua"]["reason"])
        self.assertEqual(data["javascript"]["state"], "ok")
        self.assertIn("PIN-MISMATCH", text.getvalue())
        # Ruling 111: `drift` is this CLI's decision-vs-code subcommand and
        # nothing else. The installed-vs-pinned state must not borrow it.
        self.assertNotIn("DRIFT (", text.getvalue())


class TestTreeSitterReindexIntegration(unittest.TestCase):
    """Task 10: proves the M2a reindex loop, code-search's provenance line/
    JSON, and code_census are generic over LANGUAGE_TABLE for the six new
    tree-sitter languages -- no new production code is expected here (see
    the task-10 brief); a failure in one of these means a real gap, fixed
    in the file it is found in."""

    def test_not_indexed_row_names_the_missing_wheel_reason(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "x.lua").write_text("function f()\n  return 1\nend\n")
            db = Path(td) / "idx-code.sqlite"
            chunkers.treesitter.reset_cache()
            with mock.patch.dict(sys.modules, {"tree_sitter_lua": None}):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code_reindex(root, db, lang="lua")
                self.assertIn("1 not indexed", out.getvalue())
            chunkers.treesitter.reset_cache()
            conn = memidx.open_code_db(db)
            row = conn.execute("SELECT status, reason FROM file_sha WHERE path=?", ("x.lua",)).fetchone()
            self.assertEqual(row["status"], "not-indexed")
            self.assertIn("lua", row["reason"].lower())

    def test_code_search_provenance_line_and_json_not_indexed_count(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "x.rs").write_text("fn f() -> i32 { 1 }\n")
            db = Path(td) / "idx-code.sqlite"
            chunkers.treesitter.reset_cache()
            with mock.patch.dict(sys.modules, {"tree_sitter_rust": None}):
                code_reindex(root, db, lang="rust")
            chunkers.treesitter.reset_cache()
            script = ("import sys; sys.path.insert(0, %r); import memidx; "
                      "memidx.main(['code-search', '--db', %r, 'f', '--mode', 'fts', '--no-heal', '--json'])"
                      ) % (str(TOOLS_DIR), str(db))
            r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
            data = json.loads(r.stdout)
            self.assertEqual(data["not_indexed"], 1)

    def test_census_reports_all_seven_rows_as_supported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "a.js").write_text("function f(){}\n")
            (root / "a.ts").write_text("function f(){}\n")
            (root / "a.tsx").write_text("function f(){ return null; }\n")
            (root / "a.java").write_text("class C { void f() {} }\n")
            (root / "a.php").write_text("<?php\nfunction f() {}\n")
            (root / "a.rs").write_text("fn f() {}\n")
            (root / "a.lua").write_text("function f() end\n")
            rep = memidx.code_census(root)
            for lang in ("javascript", "typescript", "tsx", "java", "php", "rust", "lua"):
                self.assertEqual(rep[lang]["status"], "supported", f"{lang} not supported in census")


class TestEmbeddingFingerprint(unittest.TestCase):
    """Design R4 (audit MC-P1-06, TOP-0123 L4), code side: embed_fp/dim
    gating in code_hits_vector, the batch-length check before the
    code-reindex zip, fingerprint-mismatch handling in code-search, and
    an old (pre-fingerprint) code db's vector layer safely invalidating
    without touching FTS."""

    FP1 = "model=fake;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=rev1"
    FOREIGN_FP = "model=other;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=revX"
    TWO_FUNCS = "def alpha():\n    return 1\n\n\ndef bravo():\n    return 2\n"

    def _one_file_root(self, td, name="a.py", body="def needle():\n    return 1\n"):
        root = Path(td) / "code"
        root.mkdir()
        (root / name).write_text(body)
        return root, root / name

    @staticmethod
    def _fake_embed(texts, model=None):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    @staticmethod
    def _fake_loader(fp):
        return lambda: (object(), fp)

    def _emb_rows(self, db):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT chunk_id, embed_fp, dim FROM embeddings ORDER BY chunk_id").fetchall()
        conn.close(); return [dict(r) for r in rows]

    def _mode(self, db):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT embedding_mode FROM code_project WHERE project=?", (memidx.DEFAULT_PROJECT,)
        ).fetchone()
        conn.close(); return r["embedding_mode"] if r else "none"

    # -- Red 4 (code side): short batch -> nothing written, reembeds
    # reports the count of vectors actually written (0).

    def test_short_batch_writes_nothing_and_reembeds_reports_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td, body=self.TWO_FUNCS)
            db = Path(td) / "idx-code.sqlite"

            def short_embed(texts, model=None):
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts[:-1]]

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "compute_embeddings", side_effect=short_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = code_reindex(root, db, no_embed=False, lang="python")
            self.assertEqual(rc, 0)
            self.assertIn("backend returned 1 vectors for 2 texts", buf_err.getvalue())
            self.assertIn("0 chunk(s) (re-)embedded", buf_out.getvalue())
            self.assertEqual(len(self._emb_rows(db)), 0)
            self.assertEqual(self._mode(db), "none")

    # Task 4 review finding L2 (carried into Task 5's commit per brief): the
    # decision side already covers a long batch (test_memidx.py's
    # TestEmbeddingFingerprint.test_long_batch_also_writes_nothing); the
    # code side only had the short-batch case above, though the underlying
    # guard (`len(vecs) != len(pending_texts)`) is symmetric either way.

    def test_long_batch_writes_nothing_code_side(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td, body=self.TWO_FUNCS)
            db = Path(td) / "idx-code.sqlite"

            def long_embed(texts, model=None):
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts] + [[0.9, 0.9, 0.9, 0.9]]

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "compute_embeddings", side_effect=long_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = code_reindex(root, db, no_embed=False, lang="python")
            self.assertEqual(rc, 0)
            self.assertIn("backend returned 3 vectors for 2 texts", buf_err.getvalue())
            self.assertIn("0 chunk(s) (re-)embedded", buf_out.getvalue())
            self.assertEqual(len(self._emb_rows(db)), 0)
            self.assertEqual(self._mode(db), "none")

    # -- Red 3 (code side): a dimension-mismatched row is skipped by
    # code_hits_vector, not ranked, and counted.

    def test_dimension_mismatch_row_skipped_in_code_hits_vector(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td, body=self.TWO_FUNCS)
            db = Path(td) / "idx-code.sqlite"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                code_reindex(root, db, no_embed=False, lang="python")
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            chunk_ids = [r["chunk_id"] for r in conn.execute("SELECT chunk_id FROM embeddings ORDER BY chunk_id")]
            self.assertEqual(len(chunk_ids), 2, chunk_ids)
            conn.execute(
                "UPDATE embeddings SET vector=?, dim=4 WHERE chunk_id=?",
                (memidx.pack_vector([0.1, 0.2, 0.3]), chunk_ids[0]),
            )
            conn.commit()

            def fake_qe(text, model=None):
                return [1.0, 0.0, 0.0, 0.0]

            with mock.patch.object(memidx, "compute_query_embedding", side_effect=fake_qe), \
                 mock.patch.object(memidx, "embedding_fingerprint", return_value=self.FP1):
                stats = {}
                ranked = memidx.code_hits_vector(conn, "alpha", memidx.DEFAULT_PROJECT, model=object(), stats=stats)
            conn.close()
            ranked_ids = [cid for cid, _ in ranked]
            self.assertNotIn(chunk_ids[0], ranked_ids, ranked)
            self.assertIn(chunk_ids[1], ranked_ids, ranked)
            self.assertEqual(stats.get("dimension_mismatch_rows"), 1, stats)

    # -- Red 5/8: code-search --json under a fingerprint mismatch falls
    # back to FTS and carries "embedding": "fingerprint-mismatch".

    def test_code_search_json_reports_fingerprint_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                code_reindex(root, db, no_embed=False, lang="python")

            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE code_project SET embedding_fingerprint=? WHERE project=?",
                (self.FOREIGN_FP, memidx.DEFAULT_PROJECT),
            )
            conn.commit(); conn.close()

            def _boom(*a, **kw):
                raise AssertionError("compute_query_embedding must not run under a fingerprint mismatch")

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "compute_query_embedding", side_effect=_boom), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_code_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query="needle", mode="hybrid",
                    limit=10, json=True, no_heal=True,
                ))
            self.assertEqual(rc, 0)
            out = json.loads(buf_out.getvalue())
            self.assertEqual(out["embedding"], "fingerprint-mismatch", out)
            self.assertIn("different model", buf_err.getvalue())
            self.assertGreater(len(out["results"]), 0, out)

    # -- Task 4 review finding M1 (carried into Task 5's commit per
    # brief), code side: zero embeddings rows but a FOREIGN stored
    # fingerprint used to be silent (no "embedding" key, no stderr)
    # because the old gate was `has_vectors` alone; the shared
    # `_fingerprint_mismatch_check` disjunction now catches it too.

    def test_zero_rows_but_foreign_fingerprint_still_reports_mismatch_code_side(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")  # zero embeddings rows

            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE code_project SET embedding_fingerprint=? WHERE project=?",
                (self.FOREIGN_FP, memidx.DEFAULT_PROJECT),
            )
            conn.commit(); conn.close()

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_code_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query="needle", mode="vector",
                    limit=10, json=True, no_heal=True,
                ))
            self.assertEqual(rc, 0)
            out = json.loads(buf_out.getvalue())
            self.assertEqual(out["embedding"], "fingerprint-mismatch", out)
            self.assertIn("different model", buf_err.getvalue())

    # -- Red 6/11 (code side): an old code db (vectors with NULL embed_fp)
    # invalidates only the vector layer; one embedding reindex refills
    # every row and reads "full"; code-search --json always carries the
    # stored embedding_fingerprint.

    def test_old_code_db_with_null_embed_fp_invalidates_vector_layer_not_fts(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, no_embed=True, lang="python")   # rows exist, never embedded

            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            chunk_id = conn.execute("SELECT id FROM chunks").fetchone()["id"]
            conn.execute(
                "INSERT INTO embeddings (chunk_id, project, dim, vector, embed_fp) VALUES (?,?,?,?,?)",
                (chunk_id, memidx.DEFAULT_PROJECT, 4, memidx.pack_vector([0.1, 0.2, 0.3, 0.4]), None),
            )
            conn.commit(); conn.close()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                memidx.cmd_code_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query="needle", mode="fts",
                    limit=10, json=True, no_heal=True,
                ))
            fts_out = json.loads(buf.getvalue())
            self.assertGreater(len(fts_out["results"]), 0, fts_out)
            self.assertIn("embedding_fingerprint", fts_out, fts_out)

            buf2 = io.StringIO()
            with mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf2), contextlib.redirect_stderr(io.StringIO()):
                rc2 = memidx.cmd_code_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query="needle", mode="vector",
                    limit=10, json=True, no_heal=True,
                ))
            self.assertEqual(rc2, 0)
            vec_out = json.loads(buf2.getvalue())
            self.assertGreater(len(vec_out["results"]), 0, vec_out)

            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                code_reindex(root, db, no_embed=False, lang="python")
            rows = self._emb_rows(db)
            self.assertTrue(all(r["embed_fp"] for r in rows), rows)
            self.assertEqual(self._mode(db), "full")

    # -- Red 9 (code side): a --no-embed reindex on a mismatched db
    # reports and does not repair.

    def test_no_embed_reindex_on_mismatched_db_reports_and_does_not_repair(self):
        with tempfile.TemporaryDirectory() as td:
            root, p = self._one_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                code_reindex(root, db, no_embed=False, lang="python")
            rows_before = self._emb_rows(db)

            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE code_project SET embedding_fingerprint=? WHERE project=?",
                (self.FOREIGN_FP, memidx.DEFAULT_PROJECT),
            )
            conn.commit(); conn.close()

            buf_err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
                rc = code_reindex(root, db, no_embed=True, lang="python")
            self.assertEqual(rc, 0)
            self.assertIn("different model", buf_err.getvalue())
            self.assertEqual(self._emb_rows(db), rows_before)


class TestIndexIntegrityFailure(unittest.TestCase):
    """Design R7 (audit MC-P2-03, TOP-0123 L7), code side: per-file
    savepoints in `cmd_code_reindex`, `_record_index_failure` raising
    `IndexIntegrityError` instead of swallowing a purge/status-write
    failure, `cmd_code_reindex` exiting 5 on an integrity failure, and
    `heal_code_index` never claiming "index healed" over one."""

    def _three_file_root(self, td):
        root = Path(td) / "code"
        root.mkdir()
        (root / "a.py").write_text("def a():\n    return 1\n")
        (root / "b.py").write_text("def b():\n    return 2\n")
        (root / "c.py").write_text("def c():\n    return 3\n")
        return root

    def _chunk_count(self, db, path):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        n = conn.execute("SELECT COUNT(*) AS n FROM chunks WHERE path=?", (path,)).fetchone()["n"]
        conn.close()
        return n

    def _file_status_row(self, db, path):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT sha256, status, reason FROM file_sha WHERE path=?", (path,)
        ).fetchone()
        conn.close()
        return dict(row) if row is not None else None

    # -- Red 3: a purge failure on the second of three files rolls back to
    # that file's savepoint (its previous chunks/status untouched), files 1
    # and 3 are unaffected, and the whole run exits 5.

    def test_purge_failure_rolls_back_to_savepoint_and_exits_5(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._three_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            rc0 = code_reindex(root, db, lang="python")
            self.assertEqual(rc0, 0)
            before = self._file_status_row(db, "b.py")
            self.assertEqual(before["status"], "ok")
            before_chunks = self._chunk_count(db, "b.py")
            self.assertGreater(before_chunks, 0)

            real_delete = memidx.delete_code_chunks_for_path

            def flaky_delete(conn, project, code_root, path):
                if path == "b.py":
                    raise sqlite3.OperationalError("disk I/O error (injected)")
                return real_delete(conn, project, code_root, path)

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "delete_code_chunks_for_path", side_effect=flaky_delete), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = code_reindex(root, db, lang="python", full=True)
            self.assertEqual(rc, 5)
            self.assertIn("integrity failure", buf_out.getvalue())
            self.assertIn("cannot purge stale rows for b.py", buf_err.getvalue())
            self.assertIn("index integrity not guaranteed", buf_err.getvalue())

            after_b = self._file_status_row(db, "b.py")
            self.assertEqual(after_b["sha256"], before["sha256"])
            self.assertEqual(after_b["status"], before["status"])
            self.assertEqual(self._chunk_count(db, "b.py"), before_chunks,
                              "b.py's previous chunks must still be present")

            after_a = self._file_status_row(db, "a.py")
            self.assertEqual(after_a["status"], "ok")
            after_c = self._file_status_row(db, "c.py")
            self.assertEqual(after_c["status"], "ok")

    def test_write_file_status_failure_also_rolls_back(self):
        """The brief's alternative trigger: write_file_status (not the
        purge) is what fails -- same outcome, proven independently so the
        rollback isn't accidentally coupled to which of the two calls
        raised."""
        with tempfile.TemporaryDirectory() as td:
            root = self._three_file_root(td)
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")
            before = self._file_status_row(db, "b.py")
            before_chunks = self._chunk_count(db, "b.py")

            real_write = memidx.write_file_status

            def flaky_write(conn, project, code_root, rel, **kw):
                if rel == "b.py":
                    raise sqlite3.OperationalError("disk I/O error (injected)")
                return real_write(conn, project, code_root, rel, **kw)

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "write_file_status", side_effect=flaky_write), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = code_reindex(root, db, lang="python", full=True)
            self.assertEqual(rc, 5)
            after_b = self._file_status_row(db, "b.py")
            self.assertEqual(after_b, before)
            self.assertEqual(self._chunk_count(db, "b.py"), before_chunks)

    # -- Regression (advisor review, post-fix), code side: same invariant
    # as the decision side's single-commit test -- pins the guarantee
    # rather than the guard itself, since a prior root-registration write
    # already opens the transaction on this side before the per-file loop
    # even starts (the BEGIN guard is a no-op here in this scenario), so
    # this test passes with or without it. Still worth having: it proves
    # the per-file SAVEPOINTs never micro-commit even when nothing forces
    # them to.

    def test_code_single_commit_nothing_lands_if_post_loop_step_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._three_file_root(td)
            db = Path(td) / "idx-code.sqlite"

            with mock.patch.object(memidx, "_git_head_sha", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    code_reindex(root, db, lang="python")

            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            n_chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            n_status = conn.execute("SELECT COUNT(*) AS n FROM file_sha").fetchone()["n"]
            n_meta = conn.execute("SELECT COUNT(*) AS n FROM code_meta").fetchone()["n"]
            conn.close()
            self.assertEqual(n_chunks, 0, "a failure after the per-file loop must roll back "
                                           "every chunk the loop wrote")
            self.assertEqual(n_status, 0, "and every file_sha row too")
            self.assertEqual(n_meta, 0, "the code_meta upsert itself is what raised -- it must "
                                         "not have landed partially either")

    # -- Red 4: heal never claims "index healed" over an integrity failure.

    def test_heal_over_integrity_failure_never_says_healed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            p = root / "x.py"; p.write_text("def old_name():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")
            p.write_text("def new_name():\n    pass\n")
            os.utime(p, (time.time() + 3600,) * 2)
            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            self.assertEqual(report["state"], "stale")

            def always_raise(conn, project, code_root, path):
                raise sqlite3.OperationalError("disk I/O error (injected)")

            buf = io.StringIO()
            with mock.patch.object(memidx, "delete_code_chunks_for_path", side_effect=always_raise), \
                 contextlib.redirect_stderr(buf):
                result_report, result_conn = memidx.heal_code_index(
                    conn, db, memidx.DEFAULT_PROJECT, report, limit=500
                )
            self.assertIn("heal did not complete (code-reindex exit 5)", buf.getvalue())
            self.assertNotIn("index healed", buf.getvalue())
            result_conn.execute("SELECT 1")  # code-search still answers from a live connection

    # -- Red 6: a missing backend still stays fail-open with a stable
    # not-indexed status/reason/attempt_key, exit 0, after the savepoint
    # wrap -- regression guard on the pre-existing behavior.

    def test_backend_missing_stays_fail_open_with_stable_reason(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "x.py").write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            with mock.patch.multiple(
                chunkers,
                get_chunker=lambda lang: (_ for _ in ()).throw(chunkers.BackendUnavailable("missing")),
                backend_availability=lambda: "python=missing;swift=ok",
            ):
                rc = code_reindex(root, db, lang="python")
            self.assertEqual(rc, 0)
            row = self._file_status_row(db, "x.py")
            self.assertEqual(row["status"], "not-indexed")
            self.assertIn("BackendUnavailable", row["reason"])
            self.assertIsNone(row["sha256"])

    # -- Direct unit coverage of _record_index_failure's new contract.

    def test_record_index_failure_returns_true_on_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            f = root / "x.py"; f.write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")
            conn = memidx.open_code_db(db)
            ok = memidx._record_index_failure(
                conn, memidx.DEFAULT_PROJECT, str(root), "x.py", f,
                sha=None, cv="1", status="not-indexed", reason="test", attempt_key="k",
            )
            conn.commit(); conn.close()
            self.assertTrue(ok)

    def test_record_index_failure_raises_index_integrity_error_on_purge_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            f = root / "x.py"; f.write_text("def f():\n    pass\n")
            db = Path(td) / "idx-code.sqlite"
            code_reindex(root, db, lang="python")
            conn = memidx.open_code_db(db)

            def always_raise(conn, project, code_root, path):
                raise sqlite3.OperationalError("disk I/O error (injected)")

            with mock.patch.object(memidx, "delete_code_chunks_for_path", side_effect=always_raise):
                with self.assertRaises(memidx.IndexIntegrityError) as ctx:
                    memidx._record_index_failure(
                        conn, memidx.DEFAULT_PROJECT, str(root), "x.py", f,
                        sha=None, cv="1", status="not-indexed", reason="test", attempt_key="k",
                    )
            conn.rollback(); conn.close()
            self.assertEqual(ctx.exception.rel, "x.py")
            self.assertIsInstance(ctx.exception.cause, sqlite3.OperationalError)


class TestGitignoreHonouredByCodeWalk(unittest.TestCase):
    """TOP-0137 L2 step 4: the code-tree walk enumerates through ONE
    `git ls-files --cached --others --exclude-standard` when the root is in
    a work tree, so gitignored content (a cloned corpus under docs/, build
    output) is never indexed; the os.walk fallback is unchanged."""

    def _repo(self, td):
        root = Path(td) / "code"
        (root / "src").mkdir(parents=True)
        (root / "vendor_clone").mkdir()
        (root / "src" / "keep.py").write_text("def keep():\n    return 1\n")
        (root / "vendor_clone" / "ignored.py").write_text("def ignored():\n    return 2\n")
        (root / ".gitignore").write_text("vendor_clone/\n")
        _init_git_repo(root)
        return root

    @staticmethod
    def _paths(db):
        conn = memidx.open_code_db(db)
        try:
            return {r["path"] for r in conn.execute("SELECT DISTINCT path FROM chunks")}
        finally:
            conn.close()

    def test_gitignored_subfolder_is_not_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            db = Path(td) / "idx-code.sqlite"
            self.assertEqual(code_reindex(root, db, lang="python"), 0)
            self.assertEqual(self._paths(db), {"src/keep.py"})
            walked = {str(p.relative_to(root)) for p in memidx.iter_code_files(root)}
            self.assertNotIn("vendor_clone/ignored.py", walked)
            self.assertIn("src/keep.py", walked)

    def test_untracked_unignored_file_is_indexed_and_deleted_tracked_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            (root / "src" / "new_untracked.py").write_text("def fresh():\n    return 3\n")
            (root / "src" / "keep.py").unlink()  # tracked, deleted from the work tree
            listed = {str(p.relative_to(root)) for p in memidx.iter_code_files(root)}
            self.assertIn("src/new_untracked.py", listed)
            self.assertNotIn("src/keep.py", listed)

    def test_walker_still_prunes_noise_dirs_and_nested_worktrees_in_git_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            wt = root / ".worktrees" / "feat"
            wt.mkdir(parents=True)
            (wt / "other.py").write_text("def other():\n    return 4\n")
            (wt / ".git").write_text("gitdir: /nonexistent/.git/worktrees/feat\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.py").write_text("def dep():\n    return 5\n")
            listed = {str(p.relative_to(root)) for p in memidx.iter_code_files(root)}
            self.assertFalse(any(x.startswith((".worktrees/", "node_modules/")) for x in listed), listed)

    def test_fallback_without_git_walks_the_tree_and_says_so_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            empty = Path(td) / "emptybin"
            empty.mkdir()
            err = io.StringIO()
            with mock.patch.dict(os.environ, {"PATH": str(empty)}), \
                    mock.patch.object(memidx, "_GIT_WALK_FALLBACK_SAID", False), \
                    contextlib.redirect_stderr(err):
                first = {str(p.relative_to(root)) for p in memidx.iter_code_files(root)}
                second = {str(p.relative_to(root)) for p in memidx.iter_code_files(root)}
            self.assertIn("vendor_clone/ignored.py", first)  # os.walk honours no .gitignore
            self.assertEqual(first, second)
            self.assertEqual(err.getvalue().count("git file list unavailable"), 1, err.getvalue())

    def test_non_work_tree_root_falls_back_to_the_walk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "plain"
            root.mkdir()
            (root / "a.py").write_text("def a():\n    return 1\n")
            with contextlib.redirect_stderr(io.StringIO()):
                listed = {str(p.relative_to(root)) for p in memidx.iter_code_files(root)}
            self.assertEqual(listed, {"a.py"})

    def test_root_that_is_itself_gitignored_is_still_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td) / "parent"
            (parent / "ign").mkdir(parents=True)
            (parent / ".gitignore").write_text("ign/\n")
            (parent / "ign" / "a.py").write_text("def a():\n    return 1\n")
            _init_git_repo(parent)
            with contextlib.redirect_stderr(io.StringIO()):
                listed = {str(p.relative_to(parent / "ign")) for p in memidx.iter_code_files(parent / "ign")}
            self.assertEqual(listed, {"a.py"})

    def test_ignored_content_an_older_walk_indexed_counts_as_removed_and_is_purged(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            db = Path(td) / "idx-code.sqlite"
            empty = Path(td) / "emptybin"
            empty.mkdir()
            # An older full reindex (no gitignore awareness) let the clone in.
            with mock.patch.dict(os.environ, {"PATH": str(empty)}), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(code_reindex(root, db, lang="python"), 0)
            self.assertEqual(self._paths(db), {"src/keep.py", "vendor_clone/ignored.py"})
            conn = memidx.open_code_db(db)
            rep = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertEqual(rep["state"], "stale")
            self.assertEqual(rep["roots"][0]["removed"], 1)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(code_reindex(root, db, lang="python"), 0)
            # cmd_code_reindex and _root_report share one enumerator: same removed count.
            self.assertIn("1 removed", out.getvalue())
            self.assertEqual(self._paths(db), {"src/keep.py"})

    def test_report_and_reindex_use_the_same_enumerator(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._repo(td)
            seen = []
            real = memidx._iter_code_tree

            def spy(r, skip_dirs):
                seen.append(str(r))
                return real(r, skip_dirs)

            db = Path(td) / "idx-code.sqlite"
            with mock.patch.object(memidx, "_iter_code_tree", spy):
                code_reindex(root, db, lang="python")
                conn = memidx.open_code_db(db)
                memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
                conn.close()
            self.assertEqual(len(seen), 2, seen)  # one walk for reindex, one for the report


class TestHealSharedWithCodeRefresh(unittest.TestCase):
    """TOP-0137 L2 step 4: heal_code_index is the ONE incremental call both
    code-search and hooks/code-refresh.sh make. It names its caller, can
    hand its messages back instead of printing them, says WHY it did not
    heal (over-limit vs fail-open exception vs a held refresh lock) and
    treats a deleted file as heal-eligible."""

    def _indexed(self, td):
        root = Path(td) / "code"; root.mkdir()
        (root / "a.py").write_text("def alpha():\n    pass\n")
        (root / "b.py").write_text("def beta():\n    pass\n")
        db = Path(td) / "idx-code.sqlite"
        code_reindex(root, db, lang="python")
        return root, db

    def _heal(self, db, **kw):
        conn = memidx.open_code_db(db)
        report = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
        out = {}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            after, conn2 = memidx.heal_code_index(
                conn, db, memidx.DEFAULT_PROJECT, report, limit=kw.pop("limit", 500), result=out, **kw
            )
        return report, after, conn2, out, err.getvalue()

    def test_a_deleted_file_alone_makes_the_heal_eligible_and_purges_it(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._indexed(td)
            (root / "b.py").unlink()
            report, after, conn, out, err = self._heal(db)
            self.assertEqual((report["changed"], report["removed"]), (1, 1))
            self.assertEqual(out["outcome"], "healed")
            self.assertEqual(after["changed"], 0)
            names = {r["symbol"] for r in conn.execute("SELECT symbol FROM chunks")}
            conn.close()
            self.assertEqual(names, {"alpha"})

    def test_result_dict_captures_messages_and_nothing_reaches_stderr(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._indexed(td)
            (root / "a.py").write_text("def alpha2():\n    pass\n")
            report, after, conn, out, err = self._heal(db, caller="code-refresh")
            conn.close()
            self.assertEqual(err, "")
            self.assertEqual(out["outcome"], "healed")
            self.assertEqual(out["reindexed"], 1)
            self.assertEqual(out["messages"], ["code-refresh: index healed (1 file(s) re-indexed)"])

    def test_default_caller_and_stderr_output_are_unchanged_for_code_search(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._indexed(td)
            (root / "a.py").write_text("def alpha3():\n    pass\n")
            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                _, conn2 = memidx.heal_code_index(conn, db, memidx.DEFAULT_PROJECT, report, limit=500)
            conn2.close()
            self.assertIn("code-search: index healed (1 file(s) re-indexed)", err.getvalue())

    def test_over_limit_and_exception_and_failed_rc_are_distinguishable(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._indexed(td)
            (root / "a.py").write_text("def a2():\n    pass\n")
            (root / "b.py").write_text("def b2():\n    pass\n")
            _, _, conn, out, _ = self._heal(db, limit=1, caller="code-refresh")
            conn.close()
            self.assertEqual(out["outcome"], "over-limit")
            self.assertIn("code-refresh: index is stale (2 file(s) changed", out["messages"][0])

            with mock.patch.object(memidx, "cmd_code_reindex", side_effect=RuntimeError("boom")):
                _, _, conn, out, _ = self._heal(db)
            conn.close()
            self.assertEqual((out["outcome"], out["exc"]), ("error", "RuntimeError"))

            with mock.patch.object(memidx, "cmd_code_reindex", return_value=3):
                _, _, conn, out, _ = self._heal(db)
            conn.close()
            self.assertEqual((out["outcome"], out["rc"]), ("reindex-failed", 3))

    def test_heal_skips_with_a_notice_when_the_refresh_lock_is_held(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._indexed(td)
            (root / "a.py").write_text("def a4():\n    pass\n")
            lock = memidx._code_refresh_lock_path(db, memidx.DEFAULT_PROJECT)
            import fcntl
            fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                conn = memidx.open_code_db(db)
                report = memidx.code_index_report(conn, memidx.DEFAULT_PROJECT)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    after, conn2 = memidx.heal_code_index(conn, db, memidx.DEFAULT_PROJECT, report, limit=500)
                conn2.close()
            finally:
                os.close(fd)
            self.assertIn("code-search: a code-index refresh is already running (lock held); not healing now", err.getvalue())
            self.assertEqual(after["state"], "stale")
            self.assertEqual(err.getvalue().count("\n"), 1)
            conn = memidx.open_code_db(db)
            names = {r["symbol"] for r in conn.execute("SELECT symbol FROM chunks")}
            conn.close()
            self.assertNotIn("a4", names)

    def test_code_search_process_skips_its_heal_when_the_lock_is_held(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            root, db = self._indexed(td)
            (root / "a.py").write_text("def a5():\n    pass\n")
            lock = memidx._code_refresh_lock_path(db, memidx.DEFAULT_PROJECT)
            fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                script = ("import sys; sys.path.insert(0, %r); import memidx; "
                          "memidx.main(['code-search', '--db', %r, 'alpha', '--mode', 'fts'])") % (str(TOOLS_DIR), str(db))
                r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
            finally:
                os.close(fd)
            self.assertIn("a code-index refresh is already running", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertNotIn("heal failed", r.stderr)


class _FakeEmbedModel:
    """A stand-in for a fastembed TextEmbedding: deterministic 384-dim one-hot
    vectors, a revision (via `model._model_dir`, what embedding_fingerprint
    reads) and no model download."""

    def __init__(self, rev="r1"):
        self.model = SimpleNamespace(_model_dir=f"/fake-cache/{rev}")

    @staticmethod
    def _vec(text):
        v = [0.0] * memidx.EMBED_DIM
        v[int(hashlib.sha256(text.encode()).hexdigest(), 16) % memidx.EMBED_DIM] = 1.0
        return v

    def embed(self, texts):
        return [self._vec(t) for t in texts]

    def query_embed(self, texts):
        return [self._vec(t) for t in texts]


def _fake_model_patch(rev="r1"):
    model = _FakeEmbedModel(rev)
    return mock.patch.object(
        memidx, "load_embedding_model", side_effect=lambda: (model, memidx.embedding_fingerprint(model))
    )


class TestCodeEmbedWorker(unittest.TestCase):
    """TOP-0137 L2 step 5: the code index's background embedding lifecycle --
    code-embed-worker (lock + marker + log, batched commits), the three
    embedding modes recomputed from real coverage, the text-only heal and
    partial-coverage search. Temp db dirs only; the model is a fake."""

    PROJECT = memidx.DEFAULT_PROJECT

    def _index(self, td, n=3, embed=False):
        root = Path(td) / "code"; root.mkdir()
        for i in range(n):
            (root / f"m{i}.py").write_text(f"def func_{i}():\n    return {i}\n")
        db = Path(td) / "idx-code.sqlite"
        if embed:
            with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
                code_reindex(root, db, lang="python", no_embed=False)
        else:
            with contextlib.redirect_stdout(io.StringIO()):
                code_reindex(root, db, lang="python")
        return root, db

    def _marker(self, db):
        return memidx._code_embed_marker_path(db, self.PROJECT)

    def _args(self, db, **kw):
        # INC-0129: the worker is handed its roots; `_index` always builds <td>/code.
        kw.setdefault("root", [str(Path(db).parent / "code")])
        return SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, **kw)

    def _worker(self, db, **patches):
        self._marker(db).touch()
        with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
            return memidx.cmd_code_embed_worker(self._args(db))

    def _state(self, db):
        conn = memidx.open_code_db(db)
        try:
            report = memidx.code_index_report(conn, self.PROJECT)
            chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            vecs = conn.execute("SELECT count(*) FROM chunks c JOIN embeddings e ON e.chunk_id=c.id").fetchone()[0]
            mode = conn.execute("SELECT embedding_mode FROM code_project").fetchone()[0]
        finally:
            conn.close()
        return mode, chunks, vecs, report["awaiting_embedding"]

    def test_worker_fills_a_bare_index_and_sets_full(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td)
            self.assertEqual(self._state(db), ("none", 3, 0, 3))
            self.assertEqual(self._worker(db), 0)
            self.assertEqual(self._state(db), ("full", 3, 3, 0))
            self.assertFalse(self._marker(db).exists(), "a clean pass removes the marker")
            log = memidx._code_embed_log_path(db, self.PROJECT).read_text()
            self.assertIn("code-embed-worker pass", log)
            self.assertTrue(log.rstrip().splitlines()[-1].endswith("0 chunk(s) awaiting embedding"), log)

    def test_worker_exits_0_at_once_when_the_lock_is_held(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td)
            self._marker(db).touch()
            fd = os.open(str(memidx._code_embed_lock_path(db, self.PROJECT)), os.O_CREAT | os.O_RDWR, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with _fake_model_patch():
                    rc = memidx.cmd_code_embed_worker(self._args(db))
            finally:
                os.close(fd)
            self.assertEqual(rc, 0)
            self.assertTrue(self._marker(db).exists(), "the running worker owns the marker")
            self.assertEqual(self._state(db)[2], 0, "a worker that lost the lock embeds nothing")

    def test_a_pass_killed_after_the_first_batch_keeps_its_vectors_and_the_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=5)
            calls = []

            def die_on_second(texts, model=None):
                calls.append(len(texts))
                if len(calls) == 2:
                    raise KeyboardInterrupt  # not an Exception: nothing fails open around it
                return _FakeEmbedModel().embed(texts)

            self._marker(db).touch()
            with _fake_model_patch(), mock.patch.object(memidx, "CODE_EMBED_BATCH", 2), \
                 mock.patch.object(memidx, "compute_embeddings", side_effect=die_on_second), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    memidx.cmd_code_embed_worker(self._args(db))
            self.assertEqual(calls, [2, 2])
            mode, chunks, vecs, awaiting = self._state(db)
            self.assertEqual((mode, chunks, vecs, awaiting), ("partial", 5, 2, 3))
            # claim-first protocol: a killed worker leaves its claim file, not the marker;
            # the next hook preflight raises the marker again from the recomputed count
            claim = Path(f"{self._marker(db)}.claim.{os.getpid()}")
            self.assertTrue(claim.exists(), "a killed pass leaves its claim for the age sweep")
            self.assertFalse(self._marker(db).exists())
            # the lock was released (the interpreter would hold it otherwise)
            self.assertEqual(self._worker(db), 0)
            self.assertFalse(claim.exists(), "the next run replaces its own pid's leftover")
            self.assertEqual(self._state(db), ("full", 5, 5, 0))

    def test_a_failed_open_backend_leaves_the_marker_and_exits_3(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td)
            self._marker(db).touch()
            with mock.patch.object(memidx, "load_embedding_model", side_effect=RuntimeError("no fastembed")), \
                 contextlib.redirect_stdout(io.StringIO()):
                rc = memidx.cmd_code_embed_worker(self._args(db))
            self.assertEqual(rc, 3)
            self.assertTrue(self._marker(db).exists())
            self.assertIn("embedding backend failed", memidx._code_embed_log_path(db, self.PROJECT).read_text())

    def test_no_embed_mutation_on_a_full_project_is_partial_with_the_count_then_worker_restores_full(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, embed=True)
            self.assertEqual(self._state(db), ("full", 3, 3, 0))
            (root / "extra.py").write_text("def extra_one():\n    return 1\n\n\ndef extra_two():\n    return 2\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code_reindex(root, db, lang="python", no_embed=True)
            self.assertEqual(self._state(db), ("partial", 5, 3, 2))
            out = buf.getvalue()
            self.assertTrue(out.splitlines()[0].endswith("2 chunk(s) awaiting embedding"), out)
            self.assertIn("embedding mode set to partial", out)
            self.assertEqual(self._worker(db), 0)
            self.assertEqual(self._state(db), ("full", 5, 5, 0))

    def test_a_stale_fingerprint_counts_as_awaiting_and_the_worker_reembeds_it(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, embed=True)
            conn = memidx.open_code_db(db)
            conn.execute("UPDATE embeddings SET embed_fp='model=BAAI/bge-small-en-v1.5;dim=384;pipeline=1;"
                         "prefix=none;norm=l2;fastembed=0.8.0;revision=old'")
            conn.commit(); conn.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                code_reindex(root, db, lang="python", no_embed=True)
            self.assertTrue(buf.getvalue().splitlines()[0].endswith("3 chunk(s) awaiting embedding"), buf.getvalue())
            self.assertEqual(self._state(db), ("none", 3, 3, 3))
            self.assertEqual(self._worker(db), 0)
            self.assertEqual(self._state(db), ("full", 3, 3, 0))
            conn = memidx.open_code_db(db)
            fps = {r[0] for r in conn.execute("SELECT embed_fp FROM embeddings")}
            conn.close()
            self.assertEqual(len(fps), 1)
            self.assertIn("revision=r1", fps.pop())

    def test_heal_never_embeds_a_full_project_is_partial_until_the_worker_runs(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, embed=True)
            (root / "m0.py").write_text("def func_0_renamed():\n    return 0\n")
            os.utime(root / "m0.py", (time.time() + 3600,) * 2)
            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, self.PROJECT)
            self.assertEqual(report["embedding_mode"], "full")
            out = {}
            compute, loader = mock.Mock(), mock.Mock()
            with mock.patch.object(memidx, "compute_embeddings", compute), \
                 mock.patch.object(memidx, "load_embedding_model", loader):
                after, conn = memidx.heal_code_index(conn, db, self.PROJECT, report, limit=500, result=out)
            conn.close()
            # recorded, not raised: try_compute_embeddings swallows exceptions
            compute.assert_not_called()
            loader.assert_not_called()
            self.assertEqual(out["outcome"], "healed")
            self.assertEqual(after["embedding_mode"], "partial")
            self.assertEqual(after["awaiting_embedding"], 1)
            self.assertEqual(self._worker(db), 0)
            self.assertEqual(self._state(db), ("full", 3, 3, 0))

    def _search(self, db, query, mode="hybrid"):
        out, err = io.StringIO(), io.StringIO()
        args = SimpleNamespace(
            db=str(db), project=self.PROJECT, query=query, mode=mode, limit=10, json=True, decision_db=None,
            no_heal=True, heal_limit=500, verify_content=False,
        )
        with _fake_model_patch(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memidx.cmd_code_search(args)
        self.assertEqual(rc, 0, err.getvalue())
        return json.loads(out.getvalue()), err.getvalue()

    def test_hybrid_search_on_a_partial_index_still_returns_fts_only_chunks(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, embed=True)
            (root / "fresh.py").write_text("def zebra_unique():\n    return 9\n")
            with contextlib.redirect_stdout(io.StringIO()):
                code_reindex(root, db, lang="python", no_embed=True)
            for mode in ("hybrid", "vector"):
                data, err = self._search(db, "zebra_unique", mode=mode)
                self.assertEqual(data["embedding_mode"], "partial")
                self.assertEqual(data["awaiting_embedding"], 1)
                self.assertIn("1 of 4 chunks have no vector; the background embed worker fills them", err)
            data, err = self._search(db, "zebra_unique", mode="hybrid")
            self.assertNotIn("use --mode hybrid", err)
            # hybrid REACHES the FTS-only chunk; pure vector mode ranks vectors only and cannot
            self.assertIn("zebra_unique", [r["qualified_name"] for r in data["results"]])
            self.assertEqual(data["results"][0]["qualified_name"], "zebra_unique", data["results"])
            data, err = self._search(db, "zebra_unique", mode="vector")
            self.assertNotIn("zebra_unique", [r["qualified_name"] for r in data["results"]])
            self.assertIn("; use --mode hybrid to reach them", err)
            # nothing awaiting -> no notice
            self.assertEqual(self._worker(db), 0)
            data, err = self._search(db, "zebra_unique")
            self.assertEqual((data["embedding_mode"], data["awaiting_embedding"]), ("full", 0))
            self.assertNotIn("have no vector", err)

    def test_cmd_code_search_hybrid_fusion_uses_k30_not_k60(self):
        # The real cmd_code_search hybrid fusion (only the two rank lists are
        # canned; filler ids match no chunk row and drop out of the output). A = fts rank 1 only; B = rank 40 in BOTH lists. Fused:
        # A = 1/(k+1); B = 2/(k+40). A beats B iff 40 > k+2, i.e. k < 38:
        # k=30 -> A (0.03226 vs 0.02857); k=60 -> B (0.01639 vs 0.02000).
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, embed=True)
            conn = memidx.open_code_db(db)
            ids = {r["qualified_name"]: r["id"] for r in conn.execute("SELECT id, qualified_name FROM chunks")}
            conn.close()
            a, b = ids["func_0"], ids["func_1"]
            fts = [a] + [-i for i in range(1, 39)] + [b]
            vec = [-(100 + i) for i in range(1, 40)] + [b]
            self.assertEqual((fts.index(b), vec.index(b)), (39, 39))
            out, err = io.StringIO(), io.StringIO()
            args = SimpleNamespace(
                db=str(db), project=self.PROJECT, query="anything", mode="hybrid", limit=200, json=True,
                decision_db=None, no_heal=True, heal_limit=500, verify_content=False,
            )
            with _fake_model_patch(), \
                 mock.patch.object(memidx, "code_hits_fts", return_value=fts), \
                 mock.patch.object(memidx, "code_hits_vector", return_value=[(c, 0.5) for c in vec]), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = memidx.cmd_code_search(args)
            self.assertEqual(rc, 0, err.getvalue())
            data = json.loads(out.getvalue())
            self.assertEqual([r["qualified_name"] for r in data["results"]], ["func_0", "func_1"], data)
            self.assertAlmostEqual(data["results"][0]["score"], 1 / 31)
            self.assertAlmostEqual(data["results"][1]["score"], 2 / 70)

    def test_spawn_states(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td)
            popen = mock.Mock()
            with mock.patch.dict(os.environ, {"MEMCONTINUUM_EMBED_WORKER": "0"}), \
                 mock.patch.object(memidx.subprocess, "Popen", popen):
                self.assertEqual(memidx.spawn_code_embed_worker(db, self.PROJECT, [root]), "off")
            self.assertTrue(self._marker(db).exists(), "off still raises the marker")
            popen.assert_not_called()
            env = {k: v for k, v in os.environ.items() if k != "MEMCONTINUUM_EMBED_WORKER"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(memidx.subprocess, "Popen", popen):
                self.assertEqual(memidx.spawn_code_embed_worker(db, self.PROJECT, [root]), "spawned")
                argv = popen.call_args.args[0]
                self.assertEqual(argv[1:], [str(Path(memidx.__file__).resolve()), "code-embed-worker",
                                             "--project", self.PROJECT, "--db", str(db),
                                             "--root", str(root.resolve())])
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
                self.assertTrue(popen.call_args.kwargs["close_fds"])
                popen.reset_mock()
                # a held lock no longer suppresses the spawn: the worker that
                # finds it held exits 0 itself (MAJOR 1)
                fd = os.open(str(memidx._code_embed_lock_path(db, self.PROJECT)), os.O_CREAT | os.O_RDWR, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(memidx.spawn_code_embed_worker(db, self.PROJECT, [root]), "spawned")
                finally:
                    os.close(fd)
                popen.assert_called_once()


class TestStaleRootsNeverRevived(unittest.TestCase):
    """INC-0129 (TOP-0137 L4): the wiring is the only consent for a root.
    The worker and the heal never process a recorded root the caller did not
    name; `code-embed-worker` without `--root` refuses."""

    PROJECT = memidx.DEFAULT_PROJECT

    def _two_roots(self, td):
        root_a, db = TestCodeEmbedWorker._index(self, td, n=2)
        root_b = Path(td) / "old-root"; root_b.mkdir()
        for i in range(2):
            (root_b / f"b{i}.py").write_text(f"def old_{i}():\n    return {i}\n")
        with contextlib.redirect_stdout(io.StringIO()):
            code_reindex(root_b, db, lang="python")
        return root_a, root_b, db

    def _by_root(self, db):
        conn = memidx.open_code_db(db)
        try:
            return {r[0]: (r[1], r[2]) for r in conn.execute(
                "SELECT c.code_root, COUNT(*), COUNT(e.chunk_id) FROM chunks c "
                "LEFT JOIN embeddings e ON e.chunk_id=c.id GROUP BY c.code_root")}
        finally:
            conn.close()

    def test_worker_ignores_a_recorded_root_the_caller_did_not_name(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            (root_b / "b0.py").write_text("def old_0_renamed():\n    return 0\n")  # drift B would reindex
            memidx._code_embed_marker_path(db, self.PROJECT).touch()
            args = SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, root=[str(root_a)])
            with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memidx.cmd_code_embed_worker(args), 0)
            by_root = self._by_root(db)
            self.assertEqual(by_root[str(root_a.resolve())], (2, 2))
            self.assertEqual(by_root[str(root_b.resolve())][1], 0, "B's chunks must keep awaiting a vector")
            conn = memidx.open_code_db(db)
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE symbol='old_0_renamed'").fetchone()[0], 0,
                    "B must not be reindexed")
            finally:
                conn.close()
            log = memidx._code_embed_log_path(db, self.PROJECT).read_text()
            self.assertIn(f"code-embed-worker pass {root_a.resolve()} ", log)
            self.assertNotIn(str(root_b.resolve()), log)

    def test_worker_without_root_refuses_with_exit_2_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, db = TestCodeEmbedWorker._index(self, td)
            marker = memidx._code_embed_marker_path(db, self.PROJECT)
            marker.touch()
            err = io.StringIO()
            args = SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, root=[])
            with _fake_model_patch(), contextlib.redirect_stderr(err):
                self.assertEqual(memidx.cmd_code_embed_worker(args), 2)
            self.assertIn("--root", err.getvalue())
            self.assertEqual(len(err.getvalue().strip().splitlines()), 1)
            self.assertFalse(memidx._code_embed_log_path(db, self.PROJECT).exists())
            self.assertTrue(marker.exists(), "a refused run leaves the marker alone")
            self.assertEqual(self._by_root(db)[str(root_a.resolve())][1], 0)

    def test_argparse_form_refuses_without_root_and_spawn_form_too(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, db = TestCodeEmbedWorker._index(self, td)
            popen = mock.Mock()
            for extra in ([], ["--spawn"]):
                with mock.patch.object(memidx.subprocess, "Popen", popen), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(memidx.main(
                        ["code-embed-worker", "--project", self.PROJECT, "--db", str(db), *extra]), 2, extra)
            popen.assert_not_called()

    def test_spawn_form_passes_every_root_to_the_single_popen_site(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            popen = mock.Mock()
            env = {k: v for k, v in os.environ.items() if k != "MEMCONTINUUM_EMBED_WORKER"}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(memidx.subprocess, "Popen", popen), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memidx.main(
                    ["code-embed-worker", "--project", self.PROJECT, "--db", str(db), "--spawn",
                     "--root", str(root_a), "--root", str(root_b)]), 0)
            popen.assert_called_once()
            argv = popen.call_args.args[0]
            self.assertEqual(argv[2:5], ["code-embed-worker", "--project", self.PROJECT])
            self.assertEqual(argv[argv.index("--root"):],
                             ["--root", str(root_b.resolve()), "--root", str(root_a.resolve())]
                             if str(root_b.resolve()) < str(root_a.resolve())
                             else ["--root", str(root_a.resolve()), "--root", str(root_b.resolve())])

    def test_kick_spawns_nothing_when_the_hook_names_no_roots(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, db = TestCodeEmbedWorker._index(self, td)
            popen = mock.Mock()
            env = {k: v for k, v in os.environ.items()
                   if k not in ("MEMCONTINUUM_CODE_ROOTS", "MEMCONTINUUM_CODE_ROOT", "MEMCONTINUUM_EMBED_WORKER")}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(memidx.subprocess, "Popen", popen):
                self.assertEqual(memidx._kick_code_embed(db, self.PROJECT, {"awaiting_embedding": 3}),
                                 "off reason=no-wired-roots")
            popen.assert_not_called()

    def test_worker_pass_does_not_recreate_a_root_dropped_after_it_captured_its_roots(self):
        # INC-0129 gate round 1 (F2): the Stop drops B while the worker is
        # still loading the model for A; B's text pass must write nothing.
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            b_s = str(root_b.resolve())
            real_model = _FakeEmbedModel("r1")
            fired = []

            def load():
                if not fired:
                    fired.append(1)  # first load = A's pass: roots are already captured
                    c = memidx.open_code_db(db)
                    try:
                        memidx.drop_code_root(c, self.PROJECT, b_s)
                    finally:
                        c.close()
                return real_model, memidx.embedding_fingerprint(real_model)

            memidx._code_embed_marker_path(db, self.PROJECT).touch()
            args = SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False,
                                   root=[str(root_a), str(root_b)])
            with mock.patch.object(memidx, "load_embedding_model", side_effect=load), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memidx.cmd_code_embed_worker(args), 0)
            conn = memidx.open_code_db(db)
            try:
                for table in ("code_meta", "chunks", "file_sha"):
                    self.assertEqual(conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE code_root=?", (b_s,)).fetchone()[0], 0, table)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM embeddings e LEFT JOIN chunks c ON c.id=e.chunk_id "
                    "WHERE c.id IS NULL").fetchone()[0], 0, "no orphan vector")
            finally:
                conn.close()
            self.assertEqual(self._by_root(db), {str(root_a.resolve()): (2, 2)})
            log = memidx._code_embed_log_path(db, self.PROJECT).read_text()
            self.assertIn("is no longer recorded; skipped (dropped by the wiring)", log)

    def test_require_recorded_reindex_of_an_unrecorded_root_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            c = memidx.open_code_db(db)
            try:
                memidx.drop_code_root(c, self.PROJECT, str(root_b.resolve()))
            finally:
                c.close()
            out = io.StringIO()
            with _fake_model_patch(), contextlib.redirect_stdout(out):
                rc = memidx.cmd_code_reindex(SimpleNamespace(
                    code_root=str(root_b), drop_root=None, db=str(db), project=self.PROJECT, lang=None,
                    no_embed=False, full=False, retry_not_indexed=False, require_recorded=True))
            self.assertEqual(rc, 0)
            self.assertIn("no longer recorded", out.getvalue())
            self.assertNotIn(str(root_b.resolve()), self._by_root(db))

    def test_drop_code_root_reads_its_snapshot_under_the_write_lock(self):
        # INC-0129 gate round 1 (F3): BEGIN IMMEDIATE precedes the file_sha read,
        # so a concurrent writer cannot slip a path in between.
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            b_s = str(root_b.resolve())
            conn = memidx.open_code_db(db)
            seen, blocked = [], []

            def trace(stmt):
                seen.append(stmt)
                if stmt.startswith("SELECT path FROM file_sha") and not blocked:
                    other = sqlite3.connect(str(db), timeout=0.05)
                    try:
                        other.execute("BEGIN IMMEDIATE")
                        blocked.append(False)
                    except sqlite3.OperationalError:
                        blocked.append(True)
                    finally:
                        other.close()

            conn.set_trace_callback(trace)
            try:
                memidx.drop_code_root(conn, self.PROJECT, b_s)
            finally:
                conn.set_trace_callback(None)
            self.assertEqual(blocked, [True], "another writer must be locked out during the snapshot read")
            first_read = next(i for i, s in enumerate(seen) if s.startswith("SELECT path FROM file_sha"))
            self.assertTrue(any(s.startswith("BEGIN IMMEDIATE") for s in seen[:first_read]), seen[:first_read])
            self.assertFalse(conn.in_transaction, "drop_code_root commits")
            for table in ("chunks", "file_sha", "code_meta"):
                self.assertEqual(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE code_root=?", (b_s,)).fetchone()[0], 0, table)
            conn.close()

    def test_a_recorded_root_stored_through_a_symlink_alias_is_not_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            alias = Path(td) / "alias-of-a"
            os.symlink(root_a, alias)
            conn = memidx.open_code_db(db)
            try:
                conn.execute("INSERT INTO code_meta (project, code_root, last_indexed_at, head_sha) "
                             "VALUES (?,?,?,?)", (self.PROJECT, str(alias), 0, None))
                conn.commit()
                wired = memidx._resolved_root_set([str(root_a)])
                stale, _n = memidx._stale_code_roots(conn, self.PROJECT, wired)
            finally:
                conn.close()
            self.assertEqual(stale, [str(root_b.resolve())], "the alias of a wired root and the wired root are not stale")

    def test_heal_with_wired_roots_never_touches_a_drifted_unwired_root(self):
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            (root_a / "m0.py").write_text("def func_0():\n    return 'a-edit'\n")
            (root_b / "b0.py").write_text("def old_0_renamed():\n    return 0\n")
            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, self.PROJECT)
            self.assertEqual(report["changed"], 2)
            hres = {}
            after, conn = memidx.heal_code_index(
                conn, db, self.PROJECT, report, limit=500, caller="t", result=hres,
                wired_roots=[str(root_a)])
            try:
                symbols = {r[0] for r in conn.execute("SELECT symbol FROM chunks")}
            finally:
                conn.close()
            self.assertNotIn("old_0_renamed", symbols, "the unwired root must not be reindexed")
            self.assertIn("old_0", symbols)
            self.assertEqual(hres["outcome"], "healed")
            self.assertEqual(after["changed"], 1, "only the unwired root's drift is left")

    def test_heal_with_a_stale_report_never_recreates_a_root_dropped_since(self):
        # INC-0129 re-gate round 2 (G1): code-search captures its report before
        # the refresh lock; a Stop drop of B in between must not be undone.
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            b_s = str(root_b.resolve())
            (root_a / "m0.py").write_text("def func_0():\n    return 'a-edit'\n")
            (root_b / "b0.py").write_text("def old_0_renamed():\n    return 0\n")
            conn = memidx.open_code_db(db)
            report = memidx.code_index_report(conn, self.PROJECT)
            self.assertEqual(report["changed"], 2)
            memidx.drop_code_root(conn, self.PROJECT, b_s)
            hres = {}
            after, conn = memidx.heal_code_index(
                conn, db, self.PROJECT, report, limit=500, caller="t", result=hres, wired_roots=None)
            try:
                for table in ("code_meta", "chunks", "file_sha"):
                    self.assertEqual(conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE code_root=?", (b_s,)).fetchone()[0], 0, table)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM embeddings e LEFT JOIN chunks c ON c.id=e.chunk_id "
                    "WHERE c.id IS NULL").fetchone()[0], 0)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE symbol='old_0_renamed'").fetchone()[0], 0)
                self.assertEqual(after["changed"], 0, "A is healed")
            finally:
                conn.close()
            self.assertEqual(hres["outcome"], "healed")

    def _alias_key(self, root):
        return str(root) + "/../" + root.name

    def _rekey(self, db, old, new):
        conn = memidx.open_code_db(db)
        try:
            for table in ("code_meta", "file_sha", "chunks"):
                conn.execute(f"UPDATE {table} SET code_root=? WHERE project=? AND code_root=?",
                             (new, self.PROJECT, old))
            conn.commit()
        finally:
            conn.close()

    def test_worker_canonicalizes_a_recorded_alias_key_and_fills_its_vectors(self):
        # INC-0129 re-gate round 2 (G2).
        with tempfile.TemporaryDirectory() as td:
            root_a, root_b, db = self._two_roots(td)
            a_s = str(root_a.resolve())
            alias = self._alias_key(root_a)
            self._rekey(db, a_s, alias)
            memidx._code_embed_marker_path(db, self.PROJECT).touch()
            args = SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, root=[str(root_a)])
            with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memidx.cmd_code_embed_worker(args), 0)
            self.assertEqual(self._by_root(db)[a_s], (2, 2))
            conn = memidx.open_code_db(db)
            try:
                for table in ("code_meta", "file_sha", "chunks"):
                    keys = {r[0] for r in conn.execute(f"SELECT DISTINCT code_root FROM {table}")}
                    self.assertIn(a_s, keys, table)
                    self.assertNotIn(alias, keys, table)
            finally:
                conn.close()

    def _chunk_state(self, conn, root_key):
        """{path: {chunk_id: vector bytes or None}} for one root key, plus fts presence."""
        out = {}
        for r in conn.execute(
                "SELECT c.path, c.id, e.vector FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.id "
                "WHERE c.code_root=?", (root_key,)):
            out.setdefault(r[0], {})[r[1]] = None if r[2] is None else bytes(r[2])
        return out

    def test_alias_only_root_cleans_removed_files_and_keeps_unchanged_vectors(self):
        # INC-0129 confirmation gate round 3 (H1): `existing` must be loaded
        # AFTER the alias key is canonicalized.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            for n in ("keep", "gone", "edit"):
                (root / f"{n}.py").write_text(f"def f_{n}():\n    return '{n}'\n")
            db = Path(td) / "idx-code.sqlite"
            with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
                code_reindex(root, db, lang="python", no_embed=False)
            canon = str(root.resolve())
            alias = self._alias_key(root)
            self._rekey(db, canon, alias)
            conn = memidx.open_code_db(db)
            try:
                before = self._chunk_state(conn, alias)
                self.assertEqual(set(before), {"keep.py", "gone.py", "edit.py"})
                for path, chunks in before.items():
                    self.assertTrue(chunks and all(v is not None for v in chunks.values()), path)
                gone_ids = list(before["gone.py"])
                self.assertTrue(all(conn.execute("SELECT COUNT(*) FROM fts WHERE rowid=?", (i,)).fetchone()[0]
                                    for i in gone_ids))
            finally:
                conn.close()
            (root / "gone.py").unlink()
            (root / "edit.py").write_text("def f_edit_v2():\n    return 'edited'\n")
            memidx._code_embed_marker_path(db, self.PROJECT).touch()
            args = SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, root=[canon])
            with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memidx.cmd_code_embed_worker(args), 0)
            conn = memidx.open_code_db(db)
            try:
                after = self._chunk_state(conn, canon)
                self.assertEqual(set(after), {"keep.py", "edit.py"})
                self.assertEqual(after["keep.py"], before["keep.py"], "unchanged file: same chunk ids and vectors")
                self.assertNotEqual(set(after["edit.py"]), set(before["edit.py"]), "modified file re-chunked")
                self.assertTrue(all(v is not None for v in after["edit.py"].values()), "modified file re-embedded")
                for i in gone_ids:
                    for sql in ("SELECT COUNT(*) FROM chunks WHERE id=?",
                                "SELECT COUNT(*) FROM fts WHERE rowid=?",
                                "SELECT COUNT(*) FROM embeddings WHERE chunk_id=?"):
                        self.assertEqual(conn.execute(sql, (i,)).fetchone()[0], 0, sql)
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM file_sha WHERE path='gone.py'").fetchone()[0], 0)
                for table in ("code_meta", "file_sha", "chunks"):
                    keys = {r[0] for r in conn.execute(f"SELECT DISTINCT code_root FROM {table}")}
                    self.assertEqual(keys, {canon}, table)
            finally:
                conn.close()

    def test_guard_drops_the_alias_rows_when_the_canonical_root_is_also_recorded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "shared.py").write_text("def shared():\n    return 1\n")
            (root / "only_canon.py").write_text("def only_canon():\n    return 2\n")
            db = Path(td) / "idx-code.sqlite"
            with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
                code_reindex(root, db, lang="python", no_embed=False)
            canon = str(root.resolve())
            alias = self._alias_key(root)
            conn = memidx.open_code_db(db)
            try:
                canon_before = self._chunk_state(conn, canon)
                self.assertEqual(set(canon_before), {"shared.py", "only_canon.py"})
                # real alias-keyed shadow rows: a path ALSO under the canonical key
                # and a path that is not, each with chunk + fts + vector rows
                conn.execute("INSERT INTO code_meta (project, code_root, last_indexed_at, head_sha) "
                             "VALUES (?,?,?,?)", (self.PROJECT, alias, 0, None))
                cols = [r[1] for r in conn.execute("PRAGMA table_info(chunks)")]
                ecols = [r[1] for r in conn.execute("PRAGMA table_info(embeddings)")]
                fcols = [r[1] for r in conn.execute("PRAGMA table_info(fts)")]
                alias_ids = []
                for src_path, new_path in (("shared.py", "shared.py"), ("only_canon.py", "alias_only.py")):
                    conn.execute("INSERT INTO file_sha (project, code_root, path, sha256, chunker_version, status) "
                                 "VALUES (?,?,?,?,?,?)", (self.PROJECT, alias, new_path, "x", "v", "indexed"))
                    row = conn.execute("SELECT * FROM chunks WHERE code_root=? AND path=? LIMIT 1",
                                       (canon, src_path)).fetchone()
                    vals = {c: row[c] for c in cols if c != "id"}
                    vals["code_root"] = alias
                    vals["path"] = new_path
                    cur = conn.execute(
                        f"INSERT INTO chunks ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                        list(vals.values()))
                    cid = cur.lastrowid
                    alias_ids.append(cid)
                    frow = conn.execute("SELECT * FROM fts WHERE rowid=?", (row["id"],)).fetchone()
                    fv = {c: frow[c] for c in fcols}
                    conn.execute(f"INSERT INTO fts (rowid,{','.join(fv)}) VALUES (?,{','.join('?' * len(fv))})",
                                 [cid, *fv.values()])
                    erow = conn.execute("SELECT * FROM embeddings WHERE chunk_id=?", (row["id"],)).fetchone()
                    ev = {c: erow[c] for c in ecols if c != "chunk_id"}
                    conn.execute(f"INSERT INTO embeddings (chunk_id,{','.join(ev)}) VALUES (?,{','.join('?' * len(ev))})",
                                 [cid, *ev.values()])
                conn.commit()
                for cid in alias_ids:
                    for sql in ("SELECT COUNT(*) FROM chunks WHERE id=?",
                                "SELECT COUNT(*) FROM fts WHERE rowid=?",
                                "SELECT COUNT(*) FROM embeddings WHERE chunk_id=?"):
                        self.assertEqual(conn.execute(sql, (cid,)).fetchone()[0], 1, sql)
            finally:
                conn.close()
            out = io.StringIO()
            with _fake_model_patch(), contextlib.redirect_stdout(out):
                rc = memidx.cmd_code_reindex(SimpleNamespace(
                    code_root=str(root), drop_root=None, db=str(db), project=self.PROJECT, lang=None,
                    no_embed=True, full=False, retry_not_indexed=False, require_recorded=True))
            self.assertEqual(rc, 0)
            self.assertIn("dropped duplicate alias key", out.getvalue())
            conn = memidx.open_code_db(db)
            try:
                for table in ("code_meta", "file_sha", "chunks"):
                    keys = {r[0] for r in conn.execute(f"SELECT DISTINCT code_root FROM {table}")}
                    self.assertEqual(keys, {canon}, table)
                for cid in alias_ids:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks WHERE id=?", (cid,)).fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM embeddings WHERE chunk_id=?", (cid,)).fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM fts WHERE rowid=?", (cid,)).fetchone()[0], 0)
                self.assertEqual(self._chunk_state(conn, canon), canon_before)
                for ids in canon_before.values():
                    for cid in ids:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fts WHERE rowid=?", (cid,)).fetchone()[0], 1)
            finally:
                conn.close()


class TestEmbedWorkerFixRound1(unittest.TestCase):
    """Embeddings-on fix round 1 (TOP-0137 L2 step 5): shutdown race in the
    shared worker loop, atomic batch writes, no write lock across a model
    load, write-safe companion files, one spawn helper."""

    PROJECT = memidx.DEFAULT_PROJECT

    def _index(self, td, n=3, embed=False):
        return TestCodeEmbedWorker._index(self, td, n=n, embed=embed)

    # -- MAJOR 1: no request is lost at worker shutdown ----------------------

    def test_store_spawn_helper_spawns_even_while_the_lock_is_held(self):
        import fcntl
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.sqlite"
            popen = mock.Mock()
            fd = os.open(str(memidx._embed_lock_path(db, "p")), os.O_CREAT | os.O_RDWR, 0o644)
            env = {k: v for k, v in os.environ.items() if k != "MEMCONTINUUM_EMBED_WORKER"}
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(memidx.subprocess, "Popen", popen):
                    self.assertEqual(memidx.spawn_embed_worker(db, "p", td), "spawned")
            finally:
                os.close(fd)
            popen.assert_called_once()
            self.assertEqual(popen.call_args.args[0][2:4], ["embed-worker", "--root"])
            self.assertTrue(memidx._embed_marker_path(db, "p").exists())

    # -- MAJOR 2: atomic existence check + orphan sweep ----------------------

    def test_a_chunk_deleted_by_another_connection_mid_batch_leaves_no_orphan_vector(self):
        """The deleting connection runs in its OWN thread (own timeout), fired at the
        moment the batch has finished its existence guard and is about to write
        (`pack_vector`, the first call after the guard in the pre-fix code). Pre-fix
        there is no write lock there, the delete commits and the vectors it then
        inserts are orphans; with the guarded INSERT inside BEGIN IMMEDIATE the
        delete is serialised out (or, if it wins, the INSERT skips the chunk)."""
        import threading
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=3)
            attempts = []
            real_pack = memidx.pack_vector
            calls = []

            def delete_in_thread():
                c2 = sqlite3.connect(str(db), timeout=0.3)
                try:
                    c2.execute("DELETE FROM chunks WHERE id NOT IN (SELECT chunk_id FROM embeddings)")
                    c2.commit()
                    attempts.append("deleted")
                except sqlite3.OperationalError:
                    attempts.append("blocked")
                finally:
                    c2.close()

            def pack_then_race(v):
                calls.append(1)
                if len(calls) == 2:  # second vector: the first batch (size 1) is already committed
                    t = threading.Thread(target=delete_in_thread)
                    t.start()
                    t.join(5)
                    self.assertFalse(t.is_alive(), "the deleting thread hung")
                return real_pack(v)

            with _fake_model_patch(), mock.patch.object(memidx, "pack_vector", pack_then_race), \
                 mock.patch.object(memidx, "CODE_EMBED_BATCH", 1), contextlib.redirect_stdout(io.StringIO()):
                code_reindex(root, db, lang="python", no_embed=False)
            self.assertTrue(attempts, "the race was never exercised")
            conn = sqlite3.connect(str(db))
            orphans = conn.execute(
                "SELECT COUNT(*) FROM embeddings WHERE chunk_id NOT IN (SELECT id FROM chunks)"
            ).fetchone()[0]
            conn.close()
            self.assertEqual(orphans, 0, f"orphan vector left behind (concurrent delete: {attempts})")

    def test_the_guarded_insert_writes_nothing_for_an_absent_chunk(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=1)
            conn = memidx.open_code_db(db)
            try:
                packed = memidx.pack_vector([0.0] * memidx.EMBED_DIM)
                cur = conn.execute(memidx.CODE_VECTOR_GUARDED_INSERT,
                                   (987654, self.PROJECT, memidx.EMBED_DIM, packed, "fp", 987654))
                self.assertEqual(max(cur.rowcount, 0), 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM embeddings WHERE chunk_id=987654").fetchone()[0], 0)
                cid = conn.execute("SELECT id FROM chunks LIMIT 1").fetchone()[0]
                cur = conn.execute(memidx.CODE_VECTOR_GUARDED_INSERT,
                                   (cid, self.PROJECT, memidx.EMBED_DIM, packed, "fp", cid))
                self.assertEqual(cur.rowcount, 1)
            finally:
                conn.close()

    def test_orphan_sweep_removes_planted_rows_and_vector_search_never_ranks_them(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=3, embed=True)
            query = "func_1"
            qvec = _FakeEmbedModel._vec(query)
            fp = memidx.embedding_fingerprint(_FakeEmbedModel())
            conn = sqlite3.connect(str(db))
            conn.execute(
                "INSERT INTO embeddings (chunk_id, project, dim, vector, embed_fp) VALUES (?,?,?,?,?)",
                (999999, self.PROJECT, memidx.EMBED_DIM, memidx.pack_vector(qvec), fp),
            )
            conn.commit()
            conn.close()
            out, err = io.StringIO(), io.StringIO()
            args = SimpleNamespace(
                db=str(db), project=self.PROJECT, query=query, mode="vector", limit=1, json=True,
                decision_db=None, no_heal=True, heal_limit=500, verify_content=False,
            )
            with _fake_model_patch(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(memidx.cmd_code_search(args), 0, err.getvalue())
            self.assertEqual(len(json.loads(out.getvalue())["results"]), 1, "an orphan consumed the result slot")
            marker = memidx._code_embed_marker_path(db, self.PROJECT)
            marker.touch()
            with _fake_model_patch(), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(memidx.cmd_code_embed_worker(
                    SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, root=[str(root)])), 0)
            conn = sqlite3.connect(str(db))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM embeddings WHERE chunk_id=999999").fetchone()[0], 0)
            conn.close()
            self.assertIn("removed 1 orphan vector(s)",
                          memidx._code_embed_log_path(db, self.PROJECT).read_text())

    # -- MAJOR 3: no write lock across a model load or an embed call ---------

    def test_no_write_lock_is_held_while_the_model_loads_or_embeds(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=3)
            probes = []

            def probe(label):
                c2 = sqlite3.connect(str(db), timeout=0.2)
                try:
                    c2.execute("BEGIN IMMEDIATE")
                    c2.execute("ROLLBACK")
                    probes.append((label, "free"))
                except sqlite3.OperationalError:
                    probes.append((label, "LOCKED"))
                finally:
                    c2.close()

            model = _FakeEmbedModel()
            real_embed = model.embed

            def embed(texts):
                probe("embed")
                return real_embed(texts)

            model.embed = embed

            def load():
                probe("load")
                return model, memidx.embedding_fingerprint(model)

            with mock.patch.object(memidx, "load_embedding_model", side_effect=load), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(code_reindex(root, db, lang="python", no_embed=False), 0)
            self.assertEqual(probes[0], ("load", "free"), probes)
            self.assertTrue(any(l == "embed" for l, _ in probes), probes)
            self.assertEqual([p for p in probes if p[1] != "free"], [], probes)

    # -- MAJOR 4: write-safe companion files ----------------------------------

    def _spawn_env(self):
        return {k: v for k, v in os.environ.items() if k != "MEMCONTINUUM_EMBED_WORKER"}

    def _spawners(self, td):
        db = Path(td) / "p.sqlite"
        return [
            ("code", db, lambda: memidx.spawn_code_embed_worker(db, "p", [td]),
             memidx._code_embed_marker_path(db, "p"), memidx._code_embed_log_path(db, "p")),
            ("store", db, lambda: memidx.spawn_embed_worker(db, "p", td),
             memidx._embed_marker_path(db, "p"), memidx._embed_log_path(db, "p")),
        ]

    def test_a_symlinked_log_is_refused_by_both_spawn_paths(self):
        for kind in ("code", "store"):
            with tempfile.TemporaryDirectory() as td:
                _, db, spawn, marker, log = [s for s in self._spawners(td) if s[0] == kind][0]
                target = Path(td) / "victim.txt"
                target.write_text("keep")
                log.symlink_to(target)
                popen = mock.Mock()
                with mock.patch.dict(os.environ, self._spawn_env(), clear=True), \
                     mock.patch.object(memidx.subprocess, "Popen", popen):
                    self.assertEqual(spawn(), "failed reason=log", kind)
                popen.assert_not_called()
                self.assertEqual(target.read_text(), "keep")

    def test_a_fifo_marker_is_refused_without_blocking_by_both_spawn_paths(self):
        import signal
        for kind in ("code", "store"):
            with tempfile.TemporaryDirectory() as td:
                _, db, spawn, marker, log = [s for s in self._spawners(td) if s[0] == kind][0]
                os.mkfifo(str(marker))
                popen = mock.Mock()
                signal.alarm(10)  # a blocking open would hang the suite; fail instead
                try:
                    with mock.patch.dict(os.environ, self._spawn_env(), clear=True), \
                         mock.patch.object(memidx.subprocess, "Popen", popen):
                        self.assertEqual(spawn(), "failed reason=marker", kind)
                finally:
                    signal.alarm(0)
                popen.assert_not_called()

    def test_a_symlinked_marker_is_refused_by_both_spawn_paths(self):
        for kind in ("code", "store"):
            with tempfile.TemporaryDirectory() as td:
                _, db, spawn, marker, log = [s for s in self._spawners(td) if s[0] == kind][0]
                target = Path(td) / "victim.txt"
                target.write_text("keep")
                marker.symlink_to(target)
                mtime = target.stat().st_mtime_ns
                with mock.patch.dict(os.environ, self._spawn_env(), clear=True), \
                     mock.patch.object(memidx.subprocess, "Popen", mock.Mock()):
                    self.assertEqual(spawn(), "failed reason=marker", kind)
                self.assertEqual(target.stat().st_mtime_ns, mtime)

    def test_a_symlinked_lock_is_refused_by_both_workers(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=1)
            store_root = Path(td) / "store"
            store_root.mkdir()
            target = Path(td) / "victim.txt"
            target.write_text("keep")
            cases = [
                (memidx._code_embed_lock_path(db, self.PROJECT), memidx._code_embed_marker_path(db, self.PROJECT),
                 lambda: memidx.cmd_code_embed_worker(SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, root=[str(root)]))),
                (memidx._embed_lock_path(db, self.PROJECT), memidx._embed_marker_path(db, self.PROJECT),
                 lambda: memidx.cmd_embed_worker(SimpleNamespace(project=self.PROJECT, db=str(db), root=str(store_root)))),
            ]
            for lock, marker, run in cases:
                lock.symlink_to(target)
                marker.touch()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(run(), 3, lock.name)
                self.assertIn("embed=failed reason=lock", err.getvalue())
                self.assertEqual(target.read_text(), "keep")
                self.assertTrue(marker.exists(), "a refused worker leaves the marker for a healthy one")

    def test_a_symlinked_log_is_not_followed_by_a_failing_worker(self):
        with tempfile.TemporaryDirectory() as td:
            marker, log, lock = Path(td) / "m", Path(td) / "l", Path(td) / "k"
            target = Path(td) / "victim.txt"
            target.write_text("keep")
            log.symlink_to(target)
            marker.touch()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = memidx._run_embed_worker_loop(lock, marker, log, "t-worker", lambda: "boom")
            self.assertEqual(rc, 3)
            self.assertEqual(target.read_text(), "keep")
            self.assertIn("embed=failed reason=log", err.getvalue())

    def test_one_popen_site_for_the_embed_workers(self):
        root = Path(memidx.__file__).resolve().parent
        self.assertNotIn("Popen(", (root / "hooks" / "post-commit-reindex.sh").read_text())
        src = Path(memidx.__file__).read_text()
        self.assertEqual(src.count("start_new_session=True, stdin=subprocess.DEVNULL"), 1)
        self.assertEqual(src.count("Path(__file__).resolve()), *argv_tail"), 1)


class TestEmbedWorkerFixRound2(unittest.TestCase):
    """Embeddings-on fix round 2 (TOP-0137 L2 step 5): claim-first marker
    protocol with a path-based producer touch, text pass committed complete
    before inference, project-name validation, no mkdir in the worker logger."""

    PROJECT = memidx.DEFAULT_PROJECT

    def _index(self, td, n=3, embed=False):
        return TestCodeEmbedWorker._index(self, td, n=n, embed=embed)

    def _paths(self, td):
        td = Path(td)
        return td / "p.lock", td / "p.embed-pending", td / "p.log"

    def _claim(self, marker):
        return Path(f"{marker}.claim.{os.getpid()}")

    # -- 1: marker protocol (shared loop) ------------------------------------

    def test_a_touch_during_the_pass_makes_a_new_marker_processed_by_the_same_run(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()
            calls = []

            def run_pass():
                calls.append(1)
                if len(calls) == 1:
                    memidx._touch_aux_marker(marker)  # producer, mid-pass
                    self.assertTrue(self._claim(marker).exists(), "the claim is taken BEFORE the pass")
                return None

            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", run_pass), 0)
            self.assertEqual(len(calls), 2)
            self.assertFalse(marker.exists() or self._claim(marker).exists())

    def test_a_touch_with_an_fd_opened_before_the_rename_and_utime_by_path_after_is_processed(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()
            calls = []
            fd = os.open(str(marker), os.O_WRONLY)  # the producer's open, before the worker's rename

            def run_pass():
                calls.append(1)
                if len(calls) == 1:
                    try:
                        os.utime(marker, None)  # the producer's path-based utime: the marker is gone
                        raised = False
                    except FileNotFoundError:
                        raised = True
                    self.assertTrue(raised)
                    os.close(fd)
                    memidx._touch_aux_marker(marker)  # ... so it recreates (retry), never a lost touch
                return None

            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", run_pass), 0)
            self.assertEqual(len(calls), 2)

    def test_producer_touch_recreates_the_marker_when_utime_loses_the_race(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "p.embed-pending"
            real_utime = os.utime
            seen = []

            def utime_once_missing(path, *a, **kw):
                seen.append(1)
                if len(seen) == 1:
                    os.rename(marker, f"{marker}.claim.1")  # the worker's claim lands in the window
                    return real_utime(path, *a, **kw)  # -> FileNotFoundError
                return real_utime(path, *a, **kw)

            with mock.patch.object(memidx.os, "utime", utime_once_missing):
                memidx._touch_aux_marker(marker)
            self.assertEqual(len(seen), 2)
            self.assertTrue(marker.is_file(), "the touch must leave a live marker")

    def test_producer_touch_gives_up_after_three_lost_races_without_raising(self):
        with tempfile.TemporaryDirectory() as td:
            marker = Path(td) / "p.embed-pending"
            seen = []

            def always_missing(path, *a, **kw):
                seen.append(1)
                raise FileNotFoundError(path)

            with mock.patch.object(memidx.os, "utime", always_missing):
                memidx._touch_aux_marker(marker)
            self.assertEqual(len(seen), 3)

    def test_a_crash_after_the_claim_leaves_the_claim_and_a_later_run_processes_a_fresh_marker(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()

            class Crash(BaseException):
                pass

            def crash():
                raise Crash

            with self.assertRaises(Crash):
                memidx._run_embed_worker_loop(lock, marker, log, "t", crash)
            self.assertTrue(self._claim(marker).is_file())
            self.assertFalse(marker.exists())
            marker.touch()
            calls = []
            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: calls.append(1)), 0)
            self.assertEqual(len(calls), 1)
            self.assertFalse(marker.exists() or self._claim(marker).exists())

    def test_a_failed_pass_puts_the_request_back_unless_a_fresh_marker_exists(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()
            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: "boom"), 3)
            self.assertTrue(marker.is_file())
            self.assertTrue(S_ISREG(os.lstat(marker).st_mode))
            self.assertFalse(self._claim(marker).exists())
            self.assertIn("boom", log.read_text())
            # an exception, with a producer having raised a fresh marker mid-pass
            def raises():
                marker.write_text("fresh")
                raise RuntimeError("kaput")

            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", raises), 3)
            self.assertEqual(marker.read_text(), "fresh")
            self.assertFalse(self._claim(marker).exists())

    def test_stale_claims_are_swept_young_and_lookalikes_are_kept(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()
            old = time.time() - 7200
            stale = Path(f"{marker}.claim.99999999")
            young = Path(f"{marker}.claim.12345")
            lookalikes = [Path(f"{marker}.claim.abc"), Path(f"{marker}.claimx.77"), Path(f"{marker}.claim.5.bak")]
            nl = Path(f"{marker}.claim.123\n")  # `$` would match before a trailing newline
            try:
                nl.write_text("x")
                lookalikes.append(nl)
            except OSError:
                pass  # the filesystem refuses a newline in a name
            stale_dir = Path(f"{marker}.claim.777")
            for f in [stale, young, *[x for x in lookalikes if not x.exists()]]:
                f.write_text("x")
            stale_dir.mkdir()
            for f in [stale, *lookalikes, stale_dir]:
                os.utime(f, (old, old))
            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: None), 0)
            self.assertFalse(stale.exists())
            self.assertTrue(young.exists())
            self.assertTrue(all(f.exists() for f in lookalikes))
            self.assertTrue(stale_dir.is_dir(), "a directory is never swept")
            self.assertIn("swept 1 stale claim", log.read_text())

    def test_an_object_swapped_in_at_the_marker_after_the_lstat_is_not_run_or_removed(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()
            real_rename = os.rename

            def swapping_rename(src, dst):
                if Path(src) == marker:  # between the loop's lstat and its rename
                    marker.unlink()
                    marker.mkdir()
                    (marker / "inner").write_text("x")
                return real_rename(src, dst)

            calls = []
            with mock.patch.object(memidx.os, "rename", swapping_rename):
                rc = memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: calls.append(1))
            self.assertEqual(rc, 3)
            self.assertEqual(calls, [])
            self.assertTrue(self._claim(marker).is_dir())
            self.assertEqual((self._claim(marker) / "inner").read_text(), "x")
            self.assertIn("claimed-not-regular", log.read_text())

    def _lstat_eio_on_claim(self, claim):
        real_lstat = os.lstat
        state = {"hit": 0}

        def flaky(path, *a, **k):
            # Only the call after the rename (the claim exists); the earlier
            # `lexists(claim)` probe must see "absent", not the injected error.
            if Path(path) == claim and not state["hit"] and claim.exists():
                state["hit"] = 1
                raise OSError(errno.EIO, "Input/output error")
            return real_lstat(path, *a, **k)

        return flaky, state

    def test_a_transient_lstat_error_on_the_claim_reraises_the_request_and_leaves_the_claim(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.write_text("req")
            claim = self._claim(marker)
            flaky, state = self._lstat_eio_on_claim(claim)
            calls = []
            with mock.patch.object(memidx.os, "lstat", flaky):
                rc = memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: calls.append(1))
            self.assertEqual(state["hit"], 1)
            self.assertEqual(rc, 3)
            self.assertEqual(calls, [])
            self.assertTrue(marker.is_file())
            self.assertTrue(S_ISREG(os.lstat(marker).st_mode))
            self.assertTrue(claim.is_file(), "an unverified claim is left for the age sweep")
            self.assertEqual(claim.read_text(), "req")
            self.assertIn("claim-inspect-failed", log.read_text())

    def test_a_transient_lstat_error_with_a_fresh_marker_leaves_the_claim(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.write_text("req")
            claim = self._claim(marker)
            real_lstat = os.lstat
            state = {"hit": 0}

            def flaky(path, *a, **k):
                if Path(path) == claim and not state["hit"] and Path(path).exists():
                    state["hit"] = 1
                    marker.write_text("fresh")  # a producer raised a new request
                    raise OSError(errno.EIO, "Input/output error")
                return real_lstat(path, *a, **k)

            calls = []
            with mock.patch.object(memidx.os, "lstat", flaky):
                rc = memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: calls.append(1))
            self.assertEqual(rc, 3)
            self.assertEqual(calls, [])
            self.assertEqual(marker.read_text(), "fresh")
            self.assertEqual(claim.read_text(), "req")
            self.assertIn("claim-inspect-failed", log.read_text())

    def test_a_symlinked_marker_is_refused_and_never_followed(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            victim = Path(td) / "victim.txt"
            victim.write_text("keep")
            marker.symlink_to(victim)
            calls = []
            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: calls.append(1)), 3)
            self.assertEqual(calls, [])
            self.assertTrue(marker.is_symlink())
            self.assertEqual(victim.read_text(), "keep")
            self.assertIn("marker-not-regular", log.read_text())

    def test_a_claim_path_planted_as_a_directory_exits_3_without_spinning(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()
            claim = self._claim(marker)
            claim.mkdir()
            (claim / "inner").write_text("x")
            calls = []
            self.assertEqual(memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: calls.append(1)), 3)
            self.assertLessEqual(len(calls), 1)
            self.assertTrue(marker.is_file(), "the marker stays on a claim failure")
            self.assertIn("claim-failed", log.read_text())

    def test_the_store_worker_processes_a_mid_pass_touch(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "p.sqlite"
            marker = memidx._embed_marker_path(db, "p")
            marker.touch()
            calls = []

            def fake_reindex(ns):
                calls.append(1)
                if len(calls) == 1:
                    memidx._touch_aux_marker(marker)
                return 0

            with mock.patch.object(memidx, "cmd_reindex", fake_reindex):
                rc = memidx.cmd_embed_worker(SimpleNamespace(project="p", db=str(db), root=td))
            self.assertEqual((rc, len(calls)), (0, 2))
            self.assertFalse(marker.exists())

    def test_the_code_worker_processes_a_mid_pass_touch(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=1)
            marker = memidx._code_embed_marker_path(db, self.PROJECT)
            marker.touch()
            calls = []

            def fake_reindex(ns):
                calls.append(1)
                if len(calls) == 1:
                    memidx._touch_aux_marker(marker)
                return 0

            with mock.patch.object(memidx, "cmd_code_reindex", fake_reindex):
                rc = memidx.cmd_code_embed_worker(SimpleNamespace(project=self.PROJECT, db=str(db), spawn=False, root=[str(root)]))
            self.assertEqual((rc, len(calls)), (0, 2))
            self.assertFalse(marker.exists())

    # -- 2: text pass committed complete before inference --------------------

    def test_text_pass_is_complete_and_committed_when_the_first_embed_dies(self):
        with tempfile.TemporaryDirectory() as td:
            root, db = self._index(td, n=3)
            conn = sqlite3.connect(str(db))
            conn.execute("UPDATE code_meta SET last_indexed_at=1.0")
            conn.commit()
            conn.close()
            (root / "m2.py").unlink()
            (root / "m1.py").write_text("def func_1():\n    return 'changed'\n")

            def die(texts, model=None):
                raise KeyboardInterrupt  # not an Exception: nothing fails open around it

            with _fake_model_patch(), mock.patch.object(memidx, "compute_embeddings", side_effect=die), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    code_reindex(root, db, lang="python", no_embed=False)
            conn = memidx.open_code_db(db)
            try:
                self.assertGreater(
                    conn.execute("SELECT last_indexed_at FROM code_meta").fetchone()[0], 1.0,
                    "the code_meta root row must be committed before inference")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM chunks WHERE path='m2.py'").fetchone()[0], 0,
                                 "the removed file's chunks must be gone")
                mode = conn.execute("SELECT embedding_mode FROM code_project").fetchone()[0]
                self.assertIn(mode, ("none", "partial"))
                rep = memidx.code_index_report(conn, self.PROJECT)
                total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            finally:
                conn.close()
            self.assertNotEqual(rep["state"], "uninitialized")
            self.assertEqual(rep["awaiting_embedding"], rep["chunk_total"])
            self.assertEqual(rep["chunk_total"], total)

    def test_a_first_ever_pass_killed_in_inference_is_not_uninitialized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "code"; root.mkdir()
            (root / "a.py").write_text("def a():\n    return 1\n")
            db = Path(td) / "idx-code.sqlite"

            def die(texts, model=None):
                raise KeyboardInterrupt

            with _fake_model_patch(), mock.patch.object(memidx, "compute_embeddings", side_effect=die), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):
                    code_reindex(root, db, lang="python", no_embed=False)
            conn = memidx.open_code_db(db)
            try:
                rep = memidx.code_index_report(conn, self.PROJECT)
            finally:
                conn.close()
            self.assertNotEqual(rep["state"], "uninitialized")
            self.assertGreater(rep["chunk_total"], 0)
            self.assertEqual(rep["awaiting_embedding"], rep["chunk_total"])

    # -- 4: project-name validation, no mkdir in the logger ------------------

    def test_bad_project_names_are_refused_by_workers_and_spawn_helpers(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "home" / "p.sqlite"
            popen = mock.Mock()
            for bad in ["", ".", "..", "a/b", "../x", "a\nb", "a\x00b", "a\\b"]:
                with mock.patch.object(memidx.subprocess, "Popen", popen):
                    self.assertEqual(memidx.spawn_embed_worker(db, bad, td), "failed reason=bad-project", repr(bad))
                    self.assertEqual(memidx.spawn_code_embed_worker(db, bad, [td]), "failed reason=bad-project", repr(bad))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(memidx.cmd_embed_worker(SimpleNamespace(project=bad, db=str(db), root=td)), 2)
                    self.assertEqual(memidx.cmd_code_embed_worker(
                        SimpleNamespace(project=bad, db=str(db), spawn=False, root=[td])), 2)
                self.assertIn("bad-project", err.getvalue())
            popen.assert_not_called()
            self.assertFalse((Path(td) / "home").exists(), "nothing may be created for a refused name")
            self.assertEqual(sorted(p.name for p in Path(td).iterdir()), [])

    def test_the_worker_logger_never_creates_a_missing_home(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "gone"
            lock, marker, log = home / "p.lock", home / "p.embed-pending", home / "p.log"
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = memidx._run_embed_worker_loop(lock, marker, log, "t", lambda: "boom")
            self.assertEqual(rc, 3)  # the lock cannot even be opened
            self.assertFalse(home.exists())
            # and with the lock present but the directory gone before the first log line
            home.mkdir()
            marker.touch()

            def vanish():
                for f in home.iterdir():
                    f.unlink()
                home.rmdir()
                return "boom"

            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = memidx._run_embed_worker_loop(lock, marker, log, "t", vanish)
            self.assertEqual(rc, 3)
            self.assertFalse(home.exists(), "the logger must not recreate the directory")
            self.assertIn("reason=log", err.getvalue())



class TestEmbedWorkerFixRound5(unittest.TestCase):
    """Embeddings-on fix round 5 (TOP-0137 L2 step 5): nothing is ever renamed
    onto the marker path; a request is re-raised by `_touch_aux_marker`."""

    def _paths(self, td):
        return TestEmbedWorkerFixRound2._paths(self, td)

    def _claim(self, marker):
        return Path(f"{marker}.claim.{os.getpid()}")

    def _run(self, lock, marker, log, run_pass):
        return memidx._run_embed_worker_loop(lock, marker, log, "t", run_pass)

    def _eio_once(self, claim, hook=None):
        real_lstat = os.lstat
        state = {"hit": 0}

        def flaky(path, *a, **k):
            if Path(path) == claim and not state["hit"] and claim.exists():
                state["hit"] = 1
                if hook:
                    hook()
                raise OSError(errno.EIO, "Input/output error")
            return real_lstat(path, *a, **k)

        return flaky, state

    def test_inspection_failure_with_a_symlink_installed_at_the_marker_leaves_it_alone(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            victim = Path(td) / "victim.txt"
            victim.write_text("keep")
            marker.write_text("req")
            claim = self._claim(marker)
            flaky, state = self._eio_once(claim, hook=lambda: marker.symlink_to(victim))
            calls = []
            with mock.patch.object(memidx.os, "lstat", flaky):
                rc = self._run(lock, marker, log, lambda: calls.append(1))
            self.assertEqual((rc, state["hit"], calls), (3, 1, []))
            self.assertTrue(S_ISLNK(os.lstat(marker).st_mode))
            self.assertEqual(victim.read_text(), "keep")
            self.assertTrue(claim.is_file())
            self.assertIn("request-reraise-refused", log.read_text())

    def test_inspection_failure_on_a_symlink_claim_never_overwrites_a_fresh_regular_marker(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            victim = Path(td) / "victim.txt"
            victim.write_text("keep")
            marker.write_text("req")
            claim = self._claim(marker)
            real_rename = os.rename

            def swapping_rename(src, dst):
                if Path(src) == marker:
                    marker.unlink()
                    marker.symlink_to(victim)
                return real_rename(src, dst)

            fresh_ino = []

            def raise_fresh():
                # a producer wins the race and raises a new request, just after
                # the worker's last look at the marker path
                if not fresh_ino:
                    marker.write_text("fresh")
                    fresh_ino.append(marker.stat().st_ino)

            real_lexists = os.path.lexists
            real_touch = memidx._touch_aux_marker

            def lexists(path):
                found = real_lexists(path)
                if Path(path) == marker and not found and Path(claim).is_symlink():
                    raise_fresh()
                return found

            def touch(path):
                raise_fresh()
                return real_touch(path)

            flaky, state = self._eio_once(claim)
            calls = []
            with mock.patch.object(memidx.os, "rename", swapping_rename), \
                 mock.patch.object(memidx.os, "lstat", flaky), \
                 mock.patch.object(memidx.os.path, "lexists", lexists), \
                 mock.patch.object(memidx, "_touch_aux_marker", touch):
                rc = self._run(lock, marker, log, lambda: calls.append(1))
            self.assertEqual((rc, state["hit"], calls), (3, 1, []))
            self.assertTrue(S_ISREG(os.lstat(marker).st_mode))
            self.assertEqual(marker.stat().st_ino, fresh_ino[0])
            self.assertEqual(marker.read_text(), "fresh")
            self.assertTrue(S_ISLNK(os.lstat(claim).st_mode))
            self.assertEqual(victim.read_text(), "keep")

    def test_a_failed_pass_reraises_a_regular_marker_and_removes_the_claim(self):
        for run_pass in (lambda: "boom", lambda: (_ for _ in ()).throw(RuntimeError("kaput"))):
            with tempfile.TemporaryDirectory() as td:
                lock, marker, log = self._paths(td)
                marker.touch()
                self.assertEqual(self._run(lock, marker, log, run_pass), 3)
                self.assertTrue(marker.is_file())
                self.assertTrue(S_ISREG(os.lstat(marker).st_mode))
                self.assertFalse(self._claim(marker).exists())

    def test_a_failed_pass_with_a_symlink_planted_at_the_marker_leaves_it_and_the_claim(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            victim = Path(td) / "victim.txt"
            victim.write_text("keep")
            marker.touch()

            def run_pass():
                marker.symlink_to(victim)  # planted mid-pass (the claim moved the marker away)
                return "boom"

            self.assertEqual(self._run(lock, marker, log, run_pass), 3)
            self.assertTrue(S_ISLNK(os.lstat(marker).st_mode))
            self.assertEqual(victim.read_text(), "keep")
            self.assertTrue(self._claim(marker).is_file())
            self.assertIn("request-reraise-refused", log.read_text())

    def test_a_non_regular_claim_also_reraises_the_request(self):
        with tempfile.TemporaryDirectory() as td:
            lock, marker, log = self._paths(td)
            marker.touch()
            real_rename = os.rename

            def swapping_rename(src, dst):
                if Path(src) == marker:
                    marker.unlink()
                    marker.mkdir()
                return real_rename(src, dst)

            with mock.patch.object(memidx.os, "rename", swapping_rename):
                rc = self._run(lock, marker, log, lambda: None)
            self.assertEqual(rc, 3)
            self.assertTrue(marker.is_file())
            self.assertTrue(self._claim(marker).is_dir())
            self.assertIn("claimed-not-regular", log.read_text())


class TestChunkRole(unittest.TestCase):
    """TOP-0137 L7 step 6: chunkers.chunk_role(rel_path, lang), derived from
    root-relative path and language only."""

    CASES = {
        "python": (["tests/a.py", "pkg/test/b.py", "test_x.py", "pkg/x_test.py", "pkg/conftest.py"],
                   ["src/x.py", "pkg/testing/x.py", "pkg/contest.py", "pkg/tests_helper.py"]),
        "swift": (["Tests/A.swift", "MMDCoreTests/A.swift", "Sources/FooTests.swift", "Sources/FooTest.swift"],
                  ["Sources/Foo.swift", "tests/Foo.swift", "Sources/Tests.swift.bak.swift"]),
        "javascript": (["__tests__/a.js", "src/tests/a.js", "src/test/a.js", "src/a.test.js", "src/a.spec.jsx"],
                       ["src/a.js", "src/testing/a.js", "src/contest.js"]),
        "typescript": (["__tests__/a.ts", "src/a.test.ts", "src/a.spec.ts"], ["src/a.ts"]),
        "tsx": (["src/a.test.tsx", "tests/a.tsx"], ["src/a.tsx"]),
        "lua": (["spec/a.lua", "tests/a.lua", "test/a.lua", "a_spec.lua", "x/a_test.lua"],
                ["src/a.lua", "src/spec_helper.lua"]),
        "java": (["src/test/java/A.java", "mod/src/test/A.java", "src/main/ATest.java", "src/main/ATests.java"],
                 ["src/main/java/A.java", "src/testing/A.java", "src/tests/A.java"]),
        "rust": (["tests/a.rs", "src/a_test.rs"], ["src/a.rs", "src/test/a.rs"]),
        "php": (["tests/A.php", "test/A.php", "src/ATest.php"], ["src/A.php", "src/Tester.php"]),
    }

    def test_every_language_has_positive_and_negative_paths(self):
        self.assertEqual(set(self.CASES) | {"javascript"}, set(chunkers.LANGUAGE_TABLE))
        for lang, (tests, code) in self.CASES.items():
            for path in tests:
                self.assertEqual(chunkers.chunk_role(path, lang), "test", (lang, path))
            for path in code:
                self.assertEqual(chunkers.chunk_role(path, lang), "code", (lang, path))

    def test_code_root_name_is_never_consulted(self):
        # the path is root-relative; the root's own name is not part of it
        self.assertEqual(chunkers.chunk_role("src/x.py", "python"), "code")
        self.assertEqual(chunkers.chunk_role("Sources/Foo.swift", "swift"), "code")
        self.assertEqual(chunkers.chunk_role("x.py", "python"), "code")

    def test_basename_is_not_matched_as_a_directory(self):
        self.assertEqual(chunkers.chunk_role("pkg/tests", "python"), "code")
        self.assertEqual(chunkers.chunk_role("pkg/Tests", "swift"), "code")

    def test_unknown_language_is_code(self):
        self.assertEqual(chunkers.chunk_role("tests/a.zz", "zz"), "code")
        self.assertEqual(chunkers.chunk_role("tests/a.py", None), "code")

    def test_case_is_significant(self):
        self.assertEqual(chunkers.chunk_role("Tests/a.py", "python"), "code")
        self.assertEqual(chunkers.chunk_role("tests/A.swift", "swift"), "code")


class TestCodeSearchRoles(unittest.TestCase):
    """TOP-0137 L7 step 6: default code-only, --tests, --all, role and
    tested_by on hits."""

    def _build(self, td):
        root = Path(td) / "code"
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "src" / "mod.py").write_text(
            "def widget_builder(x):\n    return x + 1\n\n\ndef main():\n    return widget_builder(1)\n")
        (root / "tests" / "test_mod.py").write_text(
            "from src.mod import widget_builder\n\n\nclass TestWidget:\n"
            "    def test_widget_builder(self):\n        \"\"\"Covers widget_builder.\"\"\"\n        assert widget_builder(1) == 2\n")
        db = Path(td) / "code.sqlite"
        code_reindex(root, db, lang="python")
        return db

    def _search(self, db, query, **kw):
        import contextlib
        import io
        import json as _json
        buf = io.StringIO()
        base = dict(db=str(db), query=query, mode="fts", limit=10, json=True, no_heal=True)
        base.update(kw)
        with tempfile.TemporaryDirectory() as home, mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_code_search(ns(**base))
        self.assertEqual(rc, 0)
        return buf.getvalue() if not base["json"] else _json.loads(buf.getvalue())

    def test_default_returns_code_only(self):
        with tempfile.TemporaryDirectory() as td:
            out = self._search(self._build(td), "widget_builder")
            self.assertEqual(out["role_filter"], "code")
            self.assertTrue(out["results"])
            self.assertTrue(all(h["role"] == "code" for h in out["results"]), out["results"])

    def test_tests_flag_returns_tests_only_and_all_returns_both(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._build(td)
            tests = self._search(db, "widget_builder", role_filter="tests")
            self.assertEqual(tests["role_filter"], "tests")
            self.assertTrue(tests["results"])
            self.assertTrue(all(h["role"] == "test" and "tested_by" not in h for h in tests["results"]))
            both = self._search(db, "widget_builder", role_filter="all")
            self.assertEqual({h["role"] for h in both["results"]}, {"code", "test"})

    def test_limit_applies_after_the_filter(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._build(td)
            out = self._search(db, "widget_builder", limit=1)
            self.assertEqual(len(out["results"]), 1)
            self.assertEqual(out["results"][0]["role"], "code")

    def test_code_hit_names_its_tests(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._build(td)
            out = self._search(db, "widget_builder")
            hit = [h for h in out["results"] if h["qualified_name"] == "widget_builder"][0]
            self.assertEqual([t["path"] for t in hit["tested_by"]], ["tests/test_mod.py"])
            self.assertIn("test_widget_builder", hit["tested_by"][0]["qualified_name"])
            # `main` is on the too-common list: no lookup at all
            main = [h for h in self._search(db, "main")["results"]
                    if h["qualified_name"] == "main"]
            for h in main:
                self.assertEqual(h["tested_by"], [])

    def test_text_output_marks_tests_and_prints_tested_by(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._build(td)
            text = self._search(db, "widget_builder", json=False, role_filter="all")
            self.assertIn("[test]", text)
            self.assertIn("    tested by: tests/test_mod.py::", text)
            plain = self._search(db, "widget_builder", json=False)
            self.assertNotIn("[test]", plain)

    def test_code_tested_by_skips_short_and_common_symbols(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._build(td)
            conn = memidx.open_code_db(db)
            try:
                for sym in ("abc", "main", "setUp", "init", "test"):
                    self.assertEqual(memidx.code_tested_by(conn, memidx.DEFAULT_PROJECT, sym), [], sym)
                self.assertTrue(memidx.code_tested_by(conn, memidx.DEFAULT_PROJECT, "widget_builder"))
            finally:
                conn.close()

if __name__ == "__main__":
    unittest.main()
