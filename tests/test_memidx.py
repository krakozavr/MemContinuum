import contextlib
import fcntl
import io
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

FIXTURES = TOOLS_DIR / "fixtures"
# The 14 incident records are local copies (fixtures/records/incidents/) of the
# real-world notes originally sourced from an external sandbox directory --
# copied in once, verbatim, never edited, so the test tree is self-contained.
_INCIDENTS_ENV = os.environ.get("MEMCONTINUUM_TEST_INCIDENTS", "")
LOCAL_INCIDENTS = (
    Path(_INCIDENTS_ENV) if _INCIDENTS_ENV else FIXTURES / "records" / "incidents"
)
# The corpus is untracked machine-local data, so tests that assert real hits in
# it must SKIP without it, not fail: a fresh clone has an empty directory here.
PRIVATE_CORPUS_PRESENT = LOCAL_INCIDENTS.is_dir() and any(LOCAL_INCIDENTS.glob("*.md"))
_SKIP_NO_PRIVATE_CORPUS = (
    "no records in fixtures/records/incidents/ -- drop a project's own incident "
    "notes there (or point $MEMCONTINUUM_TEST_INCIDENTS at them) to run this test"
)
# Extra synthetic-markdown corpus for the D8 timing test's fixed file count,
# outside this repo (gitignored territory). Never hardcoded in tracked test
# code -- point $MEMCONTINUUM_TEST_SANDBOX_SYNTH at a local directory of
# synthetic .md files to reproduce the exact D8 file-count assertion; without
# it that one test is skipped.
_SANDBOX_SYNTH_ENV = os.environ.get("MEMCONTINUUM_TEST_SANDBOX_SYNTH", "")
SANDBOX_SYNTH = Path(_SANDBOX_SYNTH_ENV) if _SANDBOX_SYNTH_ENV else None
HIDDEN_FILES_FIXTURE = FIXTURES / "schema" / "topics" / "processing" / "hidden-files-in-count.md"


def build_real_corpus(root: Path) -> None:
    """15 real records: the 14 real-world incidents (copied, never edited) + our
    docs/SCHEMA.md conversion of the hidden-files topic chain."""
    inc_dir = root / "incidents"
    inc_dir.mkdir(parents=True, exist_ok=True)
    for f in LOCAL_INCIDENTS.glob("*.md"):
        shutil.copy(f, inc_dir / f.name)
    topic_dir = root / "topics" / "processing"
    topic_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(HIDDEN_FILES_FIXTURE, topic_dir / HIDDEN_FILES_FIXTURE.name)


def ns(**kw):
    base = dict(project=memidx.DEFAULT_PROJECT, db=None)
    base.update(kw)
    return SimpleNamespace(**base)


def reindex(root, db, project=memidx.DEFAULT_PROJECT, full=False, no_embed=False):
    args = ns(root=str(root), db=str(db), project=project, full=full, no_embed=no_embed)
    return memidx.cmd_reindex(args)


def FIXTURES_COPY_OF(name: str, td) -> Path:
    """A mutable copy of a tracked fixtures/<name> tree, for a test that
    needs to write into it (e.g. a "stale" on-disk-drift state) without
    touching the real tracked fixture."""
    dest = Path(td) / "root"
    shutil.copytree(FIXTURES / name, dest)
    return dest


def _run_search(args):
    # cmd_search prints; pull the underlying data via the SAME ranking
    # helper it uses (memidx._search_hits) rather than reimplementing the
    # mode-dispatch/RRF logic here a second time -- a duplicate
    # implementation could silently drift out of sync with F5's real
    # filter-inside-the-channel/collapse-before-RRF changes and make the
    # paraphrase-probe gate (TestD5Paraphrase, below) measure stale code.
    conn = memidx.open_db(memidx.resolve_db_path(args))
    extra_where, extra_params = memidx.build_filter_clause(args, include_project=False)
    results, _contributing, _embedding_unavailable = memidx._search_hits(conn, args, extra_where, extra_params)
    results = results[: args.limit]
    out = []
    for path, score in results:
        row = memidx.record_row_by_path(conn, path)
        if row is None:
            continue
        # F5: a link-row hit reports the real topic path, same as cmd_search.
        out.append({"path": row["link_topic_path"] or row["path"], "score": score})
    conn.close()
    return out


@unittest.skipUnless(PRIVATE_CORPUS_PRESENT, _SKIP_NO_PRIVATE_CORPUS)
class TestD1RebuildStable(unittest.TestCase):
    """Two of the three queries here ("parallel test flake", "byte limits")
    only resolve against the untracked incident corpus, so this whole class is
    gated on it being present."""

    def test_delete_and_reindex_gives_identical_results(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            build_real_corpus(root)
            db = Path(td) / "idx.sqlite"

            reindex(root, db, no_embed=True)
            queries = ["hidden files count", "parallel test flake", "byte limits"]
            before = {q: [r["path"] for r in _run_search(ns(
                db=str(db), project=memidx.DEFAULT_PROJECT, query=q, mode="fts",
                status=[], type=[], area=None, topic=None, authority=None, limit=5, json=True))]
                for q in queries}

            db.unlink()
            reindex(root, db, no_embed=True)
            after = {q: [r["path"] for r in _run_search(ns(
                db=str(db), project=memidx.DEFAULT_PROJECT, query=q, mode="fts",
                status=[], type=[], area=None, topic=None, authority=None, limit=5, json=True))]
                for q in queries}

            self.assertEqual(before, after)
            for q in queries:
                self.assertTrue(before[q], f"expected at least one result for {q!r}")


class TestD2ProjectIsolation(unittest.TestCase):
    """Finding 2 (reviewer, 2026-08-31): several tables here (records/
    embeddings/concepts/links) key rows by `path` alone, not
    (project, path). This class's ORIGINAL test reindexed two DIFFERENT
    projects into the SAME physical db file and asserted per-project query
    filtering worked -- but root_a/root_b are different absolute
    directories, so their stored `path` values never actually collided on
    that path-only PK either way; the "isolation" it proved was
    incidental, never the collision the reviewer was worried about.
    Superseded by the simpler, equally-safe fix the finding itself
    sanctions ("or clean refusal"): opening the SAME db file under a
    DIFFERENT --project than the one that first claimed it is now a hard,
    named error (enforce_project_isolation) instead of a silent
    cross-project eviction/overwrite risk -- composite-key migration
    across every table/query here was rejected as disproportionate to a
    collision only reachable via an explicit --db override."""

    def test_reopening_the_same_db_file_under_a_different_project_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            root_a = Path(td) / "a"
            root_b = Path(td) / "b"
            build_real_corpus(root_a)
            build_real_corpus(root_b)
            db = Path(td) / "shared.sqlite"  # same db file, different --project

            reindex(root_a, db, project="proj-a", no_embed=True)
            with self.assertRaises(memidx.DbProjectMismatchError) as ctx:
                reindex(root_b, db, project="proj-b", no_embed=True)
            self.assertIn("proj-a", str(ctx.exception))
            self.assertIn("proj-b", str(ctx.exception))

    def test_reopening_the_same_db_file_under_the_same_project_is_unaffected(self):
        with tempfile.TemporaryDirectory() as td:
            root_a = Path(td) / "a"
            build_real_corpus(root_a)
            db = Path(td) / "shared.sqlite"

            reindex(root_a, db, project="proj-a", no_embed=True)
            reindex(root_a, db, project="proj-a", no_embed=True)  # must not raise

            results = _run_search(ns(
                db=str(db), project="proj-a", query="hidden files count", mode="fts",
                status=[], type=[], area=None, topic=None, authority=None, limit=50, json=True,
            ))
            self.assertTrue(results)


class TestD2LegacyDbMigrationSafety(unittest.TestCase):
    """HIGH (2026-08-31 review): enforce_project_isolation's `row is None`
    branch (no db_meta row yet) used to blindly INSERT the *requested*
    project as owner -- fine for a genuinely empty new db, but wrong for a
    real pre-Finding-2 db that already has rows: it would silently rewrite
    ownership to whatever --project the caller happened to pass, defeating
    the whole point of the isolation guard on exactly the db that needs it
    most (an old db that predates db_meta ever being stamped).

    Fix: when db_meta has no project row, inspect DISTINCT project values
    already present in the data tables (records, at minimum). Zero rows ->
    genuinely empty, stamp the requested project. Exactly one distinct
    project and it matches the request -> stamp it (this IS that project's
    db, just never stamped). Otherwise (one DIFFERENT project, or several)
    -> refuse with a named error, do not guess."""

    def _legacy_db_with_rows(self, db_path, *projects):
        # Simulate a pre-db_meta db file: open once with project=None so
        # the schema (including the db_meta table itself) exists but is
        # never stamped, then insert rows directly as an older memidx.py
        # (pre-Finding-2) would have -- one row per project given.
        conn = memidx.open_db(db_path, project=None)
        for i, proj in enumerate(projects):
            conn.execute(
                "INSERT INTO records (path, sha256, mtime, size, project, type, id, "
                "title, area, topic, status, authority, tags, code_refs, body, "
                "ruling_text) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"fake/path-{i}.md", "deadbeef", 0.0, 0, proj, "incident",
                 f"id-{i}", "title", None, None, None, None, "", "", "", ""),
            )
        conn.commit()
        conn.close()
        self.assertIsNone(
            memidx.sqlite3.connect(str(db_path)).execute(
                "SELECT value FROM db_meta WHERE key='project'"
            ).fetchone(),
            "test setup bug: db_meta must genuinely have no project row yet",
        )

    def test_legacy_db_with_rows_of_project_a_opened_as_b_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.sqlite"
            self._legacy_db_with_rows(db, "project-a")
            with self.assertRaises(memidx.DbProjectMismatchError) as ctx:
                memidx.open_db(db, project="project-b")
            self.assertIn("project-a", str(ctx.exception))
            self.assertIn("project-b", str(ctx.exception))
            # and it must NOT have stamped db_meta on the way to refusing
            conn = memidx.sqlite3.connect(str(db))
            conn.row_factory = memidx.sqlite3.Row
            row = conn.execute("SELECT value FROM db_meta WHERE key='project'").fetchone()
            conn.close()
            self.assertIsNone(row, "a refused open must not stamp db_meta")

    def test_legacy_db_with_rows_of_project_a_opened_as_a_is_stamped_and_works(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.sqlite"
            self._legacy_db_with_rows(db, "project-a")
            conn = memidx.open_db(db, project="project-a")  # must not raise
            row = conn.execute("SELECT value FROM db_meta WHERE key='project'").fetchone()
            self.assertEqual(row["value"], "project-a")
            conn.close()
            memidx.open_db(db, project="project-a").close()  # reopen still fine

    def test_empty_legacy_db_is_stamped_with_requested_project(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.sqlite"
            memidx.open_db(db, project=None).close()  # schema only, zero rows
            conn = memidx.open_db(db, project="project-c")  # must not raise
            row = conn.execute("SELECT value FROM db_meta WHERE key='project'").fetchone()
            self.assertEqual(row["value"], "project-c")
            conn.close()

    def test_legacy_db_with_rows_of_several_projects_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.sqlite"
            self._legacy_db_with_rows(db, "project-a", "project-x")
            with self.assertRaises(memidx.DbProjectMismatchError) as ctx:
                memidx.open_db(db, project="project-a")
            msg = str(ctx.exception)
            self.assertIn("project-a", msg)
            self.assertIn("project-x", msg)


class TestOpenDbProjectIsolationDirect(unittest.TestCase):
    """Finding 2, direct-API coverage of open_db/enforce_project_isolation
    (not routed through a full reindex)."""

    def test_open_db_without_project_never_enforces(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "shared.sqlite"
            memidx.open_db(db, project="proj-a").close()
            # no `project` kwarg given here -- must never raise, even
            # though the file is already owned by proj-a (existing direct
            # open_db(path) callers, e.g. tests inspecting a db file, must
            # be unaffected).
            memidx.open_db(db).close()

    def test_open_code_db_has_no_project_isolation_check(self):
        # Finding 2: the CODE db is already project-scoped by its own
        # schema (file_sha PK is (path, project), chunk deletes are
        # project-scoped, code_meta is keyed by project) -- opening it
        # must never be refused, regardless of how many different
        # projects' code-reindex runs land in the same physical file.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "shared-code.sqlite"
            memidx.open_code_db(db).close()
            memidx.open_code_db(db).close()


class TestD3AndFilters(unittest.TestCase):
    def test_status_authority_area_type_and_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES / "schema_filters"
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                status=["active"], type=["topic"], area="processing/alpha",
                topic=None, authority="owner-verbatim",
            )
            conn = memidx.open_db(memidx.resolve_db_path(args))
            paths = memidx.filtered_paths(conn, args)
            conn.close()
            self.assertEqual(len(paths), 1, paths)
            self.assertTrue(any(p.endswith("status-a.md") for p in paths), paths)


class TestD4Chain(unittest.TestCase):
    def _reindexed_db(self, td):
        root = Path(td) / "root"
        root.mkdir()
        topic_dir = root / "topics" / "processing"
        topic_dir.mkdir(parents=True)
        shutil.copy(HIDDEN_FILES_FIXTURE, topic_dir / HIDDEN_FILES_FIXTURE.name)
        db = Path(td) / "idx.sqlite"
        reindex(root, db, no_embed=True)
        return db

    def test_chain_newest_first_with_reasons_and_current(self):
        with tempfile.TemporaryDirectory() as td:
            db = self._reindexed_db(td)
            args = ns(db=str(db), project=memidx.DEFAULT_PROJECT, topic="TOP-0042", json=True)
            conn = memidx.open_db(memidx.resolve_db_path(args))
            topic_row = memidx.find_topic_row(conn, args.project, args.topic)
            self.assertIsNotNone(topic_row)
            link_rows = conn.execute(
                "SELECT * FROM links WHERE topic_path=? ORDER BY seq ASC", (topic_row["path"],)
            ).fetchall()
            conn.close()

            order = [lr["link"] for lr in link_rows]
            self.assertEqual(order, ["L4", "L3", "L2", "L1"], "must be newest-first")

            lines = memidx.chain_lines(topic_row, link_rows)
            header = lines[0]
            self.assertIn("current: L4", header)
            self.assertIn("active", header)

            l4_line = next(l for l in lines if l.strip().startswith("L4"))
            self.assertIn("reverses L3", l4_line)
            self.assertIn("new-evidence", l4_line)


class TestD5Paraphrase(unittest.TestCase):
    # Query/expectation pairs live OUTSIDE tracked code (fixtures/records/ is
    # gitignored): they paraphrase one real project's incident history and are
    # private evaluation data, like the corpus they query.
    _QJ = Path(__file__).resolve().parent.parent / "fixtures" / "records" / "queries.json"
    QUERIES = json.loads(_QJ.read_text()) if _QJ.exists() else []

    def test_paraphrase_top1_at_least_9_of_10(self):
        if not self.QUERIES:
            self.skipTest("private evaluation queries not present (fixtures/records/queries.json)")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            build_real_corpus(root)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)

            hits = 0
            details = []
            for query, expect_substr in self.QUERIES:
                results = _run_search(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, query=query, mode="vector",
                    status=[], type=[], area=None, topic=None, authority=None, limit=1, json=True,
                ))
                top1 = results[0]["path"] if results else ""
                ok = expect_substr in top1
                details.append((query, expect_substr, top1, ok))
                if ok:
                    hits += 1
            self.assertGreaterEqual(hits, 9, f"{hits}/10 -- {details}")


class TestD8TimingAndForPath(unittest.TestCase):
    @unittest.skipUnless(
        SANDBOX_SYNTH is not None and SANDBOX_SYNTH.is_dir(),
        "set $MEMCONTINUUM_TEST_SANDBOX_SYNTH to a directory of synthetic "
        ".md files to reproduce this test's fixed file-count assertion",
    )
    def test_full_reindex_under_2s_no_embed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            build_real_corpus(root)
            synth_dir = root / "synth"
            synth_dir.mkdir()
            for f in SANDBOX_SYNTH.glob("*.md"):
                shutil.copy(f, synth_dir / f.name)
            db = Path(td) / "idx.sqlite"

            t0 = time.time()
            reindex(root, db, no_embed=True)
            elapsed = time.time() - t0
            self.assertLess(elapsed, 2.0, f"reindex took {elapsed:.3f}s")

            n = sum(1 for _ in memidx.walk_markdown(root))
            self.assertEqual(n, 315)

    def test_for_path_under_300ms_in_process(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            topic_dir = root / "topics" / "processing"
            topic_dir.mkdir(parents=True)
            shutil.copy(HIDDEN_FILES_FIXTURE, topic_dir / HIDDEN_FILES_FIXTURE.name)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            args = ns(
                db=str(db), project=memidx.DEFAULT_PROJECT,
                file_path="src/core/scan/scan_plan.py", json=True,
            )
            t0 = time.time()
            rc = memidx.cmd_for_path(args)
            elapsed = time.time() - t0
            self.assertEqual(rc, 0)
            self.assertLess(elapsed, 0.3, f"for-path took {elapsed:.3f}s")

    def test_for_path_does_not_import_fastembed(self):
        """Isolated-process check: importing memidx and running for-path must
        never pull in fastembed (or numpy via it) at all."""
        import subprocess

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            topic_dir = root / "topics" / "processing"
            topic_dir.mkdir(parents=True)
            shutil.copy(HIDDEN_FILES_FIXTURE, topic_dir / HIDDEN_FILES_FIXTURE.name)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            script = (
                "import sys; sys.path.insert(0, %r); import memidx; "
                "memidx.main(['for-path', '--db', %r, "
                "'src/core/scan/scan_plan.py']); "
                "assert 'fastembed' not in sys.modules, 'fastembed was imported'; "
                "assert 'numpy' not in sys.modules, 'numpy was imported'"
            ) % (str(TOOLS_DIR), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_for_path_json_with_chain_text_matches_plain_text_and_wraps_results(self):
        """memidx.py item 1 (round 5, `--with-chain-text`): a caller (the
        pre-edit hook) needs only ONE `for-path` call per candidate instead
        of a second, separate plain-text call for the chain view. The
        `chain_text` field must hold EXACTLY the text `for-path`'s own
        plain-text mode prints for this same path -- one function
        (for_path_chain_lines) renders both, so this also pins "no
        duplicated formatting". The bare list becomes an object carrying
        `results` once the flag is given."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            topic_dir = root / "topics" / "processing"
            topic_dir.mkdir(parents=True)
            shutil.copy(HIDDEN_FILES_FIXTURE, topic_dir / HIDDEN_FILES_FIXTURE.name)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            plain_buf = io.StringIO()
            with contextlib.redirect_stdout(plain_buf):
                rc_plain = memidx.cmd_for_path(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT,
                    file_path="src/core/scan/scan_plan.py", json=False,
                ))
            self.assertEqual(rc_plain, 0)

            json_buf = io.StringIO()
            with contextlib.redirect_stdout(json_buf):
                rc_json = memidx.cmd_for_path(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT,
                    file_path="src/core/scan/scan_plan.py", json=True,
                    with_chain_text=True,
                ))
            self.assertEqual(rc_json, 0)
            payload = json.loads(json_buf.getvalue())
            self.assertIsInstance(payload, dict)
            self.assertEqual(set(payload.keys()), {"results", "chain_text"})
            self.assertIsInstance(payload["results"], list)
            self.assertTrue(payload["results"])
            self.assertEqual(payload["chain_text"], plain_buf.getvalue().rstrip("\n"))

    def test_for_path_json_without_flag_stays_a_bare_list_on_a_real_match(self):
        """Regression pin: --with-chain-text is opt-in -- omitted, --json
        keeps its pre-existing bare-list shape even on a real match (the
        other shape tests around for-path only cover the missing/error
        states, never a positive match)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            root.mkdir()
            topic_dir = root / "topics" / "processing"
            topic_dir.mkdir(parents=True)
            shutil.copy(HIDDEN_FILES_FIXTURE, topic_dir / HIDDEN_FILES_FIXTURE.name)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_for_path(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT,
                    file_path="src/core/scan/scan_plan.py", json=True,
                ))
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertIsInstance(payload, list)
            self.assertTrue(payload)


class TestStoreWalkPruning(unittest.TestCase):
    """walk_markdown must not index markdown that merely happens to sit under
    the store root. The live case: a `.remember/now.md` session buffer in a
    store root was reindexed as a record and returned by `search` next to real
    rulings. `.gitignore` cannot prevent this -- the walk is a filesystem walk,
    not a git one -- so the pruning has to live here."""

    def test_dot_directories_and_node_modules_are_pruned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            (root / "topics" / "area").mkdir(parents=True)
            (root / "topics" / "area" / "real.md").write_text("# real\n")
            (root / "README.md").write_text("# store\n")
            for noise in (".remember", ".claude", ".git", "node_modules"):
                d = root / noise
                d.mkdir()
                (d / "now.md").write_text("# noise\n")
            # nested one level down too -- pruning must apply at every depth
            (root / "topics" / ".remember").mkdir()
            (root / "topics" / ".remember" / "buf.md").write_text("# noise\n")

            # hidden FILES in a kept directory are pruned too
            (root / "topics" / "area" / ".draft.md").write_text("# noise\n")

            found = sorted(p.name for p in memidx.walk_markdown(root))
            self.assertEqual(found, ["README.md", "real.md"])

    def test_a_store_root_that_is_itself_a_dot_directory_still_walks(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".memory"
            (root / "topics").mkdir(parents=True)
            (root / "topics" / "real.md").write_text("# real\n")
            self.assertEqual([p.name for p in memidx.walk_markdown(root)], ["real.md"])


class TestStoreWalkerSymlinks(unittest.TestCase):
    """The store walker must not follow symlinks out of the root (audit
    MC-P1-01): a symlinked directory or file is pruned/skipped instead of
    walked, each skip is warned on stderr and counted, and a --root that is
    itself a symlink to the store still resolves and indexes correctly."""

    def _symlink(self, target, link_path) -> None:
        try:
            os.symlink(target, link_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"os.symlink unsupported on this filesystem: {exc}")

    @staticmethod
    def _record_paths(db, project=None):
        project = project or memidx.DEFAULT_PROJECT
        conn = sqlite3.connect(str(db))
        try:
            return {
                row[0] for row in conn.execute(
                    "SELECT path FROM records WHERE project=?", (project,)
                )
            }
        finally:
            conn.close()

    def test_external_directory_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            (store / "topics").mkdir(parents=True)
            (store / "topics" / "real.md").write_text("# real\n")
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "private.md").write_text("# private\n")
            self._symlink("../outside", str(store / "linked"))

            db = Path(td) / "idx.sqlite"
            rc = reindex(store, db, no_embed=True)
            self.assertEqual(rc, 0)

            paths = self._record_paths(db)
            self.assertEqual(len(paths), 1)
            self.assertTrue(any(p.endswith("real.md") for p in paths))
            self.assertFalse(any("linked" in p for p in paths))
            self.assertFalse(any("private.md" in p for p in paths))

            buf_out = io.StringIO()
            with contextlib.redirect_stdout(buf_out):
                rc_check = memidx.cmd_check(
                    ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(store), json=True)
                )
            self.assertEqual(rc_check, 0)
            report = json.loads(buf_out.getvalue())
            self.assertFalse(report["drift"])
            self.assertGreaterEqual(report["symlinks_skipped"], 1)

            errors, _warnings = memlint.lint_root(store)
            self.assertFalse(any("private.md" in e for e in errors))

    def test_parent_cycle_terminates_and_indexes_each_file_once(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            (store / "topics").mkdir(parents=True)
            (store / "topics" / "one.md").write_text("# one\n")
            (store / "topics" / "two.md").write_text("# two\n")
            self._symlink(".", str(store / "loop"))

            db = Path(td) / "idx.sqlite"
            rc = reindex(store, db, no_embed=True)
            self.assertEqual(rc, 0)

            paths = self._record_paths(db)
            self.assertEqual(len(paths), 2)
            self.assertFalse(any("loop/" in p for p in paths))

    def test_symlinked_file_is_skipped_with_a_warning(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            (store / "topics").mkdir(parents=True)
            (store / "topics" / "real.md").write_text("# real\n")
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "x.md").write_text("# outside x\n")
            self._symlink("../../outside/x.md", str(store / "topics" / "linked-file.md"))

            db = Path(td) / "idx.sqlite"
            buf_err = io.StringIO()
            with contextlib.redirect_stderr(buf_err):
                rc = reindex(store, db, no_embed=True)
            self.assertEqual(rc, 0)

            paths = self._record_paths(db)
            self.assertEqual(len(paths), 1)
            self.assertTrue(any(p.endswith("real.md") for p in paths))

            stderr = buf_err.getvalue()
            self.assertIn("linked-file.md", stderr)
            self.assertIn("symlink skipped", stderr)

    def test_reindex_check_and_memlint_walk_the_same_files(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            (store / "topics").mkdir(parents=True)
            (store / "topics" / "real.md").write_text("# real\n")
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "private.md").write_text("# private\n")
            self._symlink("../outside", str(store / "linked"))
            (outside / "x.md").write_text(
                "---\ntype: topic\nid: TOP-BAD\ntitle: Bad\nlinks: []\n"
                "status: superseded\n---\nBody.\n"
            )
            self._symlink("../../outside/x.md", str(store / "topics" / "linked-file.md"))

            db = Path(td) / "idx.sqlite"
            rc = reindex(store, db, no_embed=True)
            self.assertEqual(rc, 0)
            reindexed_paths = self._record_paths(db)
            self.assertEqual(len(reindexed_paths), 1)

            buf_out = io.StringIO()
            with contextlib.redirect_stdout(buf_out):
                rc_check = memidx.cmd_check(
                    ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(store), json=True)
                )
            self.assertEqual(rc_check, 0)
            report = json.loads(buf_out.getvalue())
            self.assertEqual(report["added"], [])
            self.assertEqual(report["removed"], [])

            errors, _warnings = memlint.lint_root(store)
            self.assertFalse(any("TOP-BAD" in e for e in errors))
            self.assertFalse(any("superseded" in e for e in errors))

    def test_root_given_through_a_symlink_still_indexes(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "tmp"
            store = tmp / "store"
            store.mkdir(parents=True)
            (store / "real.md").write_text("# real\n")
            link = tmp / "link-to-store"
            self._symlink(str(store), str(link))

            db = Path(td) / "idx.sqlite"
            rc = reindex(link, db, no_embed=True)
            self.assertEqual(rc, 0)

            paths = self._record_paths(db)
            self.assertEqual(len(paths), 1)
            resolved_store = str(store.resolve())
            self.assertTrue(next(iter(paths)).startswith(resolved_store))

            buf_out = io.StringIO()
            with contextlib.redirect_stdout(buf_out):
                rc2 = reindex(store, db, no_embed=True)
            self.assertEqual(rc2, 0)
            out = buf_out.getvalue()
            self.assertIn("0 added", out)
            self.assertIn("1 unchanged", out)


class TestPrunedPathsLeaveTheIndex(unittest.TestCase):
    """Pruning the walker is only half the fix: an ALREADY-polluted index has
    to lose those rows too. reindex drops anything the walk no longer yields,
    so the first reindex after this change cleans itself. Asserting on
    walk_markdown alone would not catch a future reindex that kept walking
    correctly but stopped deleting unseen rows -- which is exactly the path
    that repairs a live database (reviewer finding 4, 2026-08-31)."""

    def test_reindex_removes_rows_for_newly_pruned_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            (root / "topics" / "area").mkdir(parents=True)
            (root / "topics" / "area" / "real.md").write_text(
                "---\ntype: topic\nid: TOP-0001\ntitle: Real\n---\n# Real\n")
            noise_dir = root / ".remember"
            noise_dir.mkdir()
            noise = noise_dir / "now.md"
            noise.write_text("# session buffer\n")

            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            # Simulate the polluted state a pre-fix index was left in.
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            conn.execute(
                "INSERT INTO records (path, project, type, id, title, body, sha256, mtime, size) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(noise), memidx.DEFAULT_PROJECT, "topic", "TOP-9999",
                 "buffer", "noise", "0" * 64, 0.0, 1),
            )
            conn.commit()
            # real.md's expected key is RESOLVED: reindex() canonicalises
            # root via .resolve() before storing any path, so the row it
            # wrote for real.md is the resolved form -- raw only matches
            # by coincidence on Linux, where /tmp is not usually symlinked
            # (macOS's TMPDIR-derived /var/folders/... is a symlink to
            # /private/var/folders/...). `noise`'s row, in contrast, was
            # inserted directly above with its own raw str(noise) key (it
            # simulates a pre-fix polluted row, never touched by reindex),
            # so it stays raw on both sides of this comparison.
            real_path = str((root / "topics" / "area" / "real.md").resolve())
            self.assertEqual(self._paths(conn), {real_path, str(noise)})
            conn.close()

            reindex(root, db, no_embed=True)

            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            self.assertEqual(self._paths(conn), {real_path})
            conn.close()
            # The file on disk is never touched -- only the derived index.
            self.assertTrue(noise.is_file())

    @staticmethod
    def _paths(conn):
        return {r[0] for r in conn.execute(
            "SELECT path FROM records WHERE project=?", (memidx.DEFAULT_PROJECT,))}


class TestCheckDrift(unittest.TestCase):
    """audit MC-P1-02 / design R3 (TOP-0123 L3) -- the inversion: a pure
    touch (mtime moves, content unchanged) is no longer drift; a same-size,
    same-mtime_ns content rewrite (invisible to a metadata-only comparison)
    is. This replaces the old test_check_detects_touched_file, which
    asserted the opposite for a touch."""

    def test_touch_is_not_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            topic_dir = root / "topics" / "processing"
            topic_dir.mkdir(parents=True)
            target = topic_dir / HIDDEN_FILES_FIXTURE.name
            shutil.copy(HIDDEN_FILES_FIXTURE, target)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            args = ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root), json=True)
            rc_clean = memidx.cmd_check(args)
            self.assertEqual(rc_clean, 0)

            # touch: change mtime without changing content
            new_time = time.time() + 5
            os.utime(target, (new_time, new_time))

            rc_dirty = memidx.cmd_check(args)
            self.assertEqual(rc_dirty, 0, "a bare touch must not read as drift (design R3)")

    def test_same_size_same_mtime_ns_rewrite_is_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            topic_dir = root / "topics" / "processing"
            topic_dir.mkdir(parents=True)
            target = topic_dir / HIDDEN_FILES_FIXTURE.name
            shutil.copy(HIDDEN_FILES_FIXTURE, target)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            args = ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root), json=True)
            self.assertEqual(memidx.cmd_check(args), 0)

            st = target.stat()
            orig_ns = st.st_mtime_ns
            text = target.read_text()
            new_text = text.replace("Dotfiles", "DOTFILES", 1)
            self.assertNotEqual(new_text, text, "fixture bug: the word to rewrite is not present")
            self.assertEqual(len(new_text), len(text), "fixture bug: rewrite must be same length")
            target.write_text(new_text)
            os.utime(target, ns=(orig_ns, orig_ns))

            rc_dirty = memidx.cmd_check(args)
            self.assertEqual(
                rc_dirty, 1, "a same-size, same-mtime_ns content rewrite must be drift"
            )


def _write_topic(path: Path, tid: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntype: topic\nid: {tid}\ntitle: T\nlinks:\n"
        f'  - link: L1\n    status: active\n    ruling: {{text: "r", authority: owner-verbatim, source: s}}\n'
        f"---\n{body}\n"
    )


def _rewrite_same_size(path: Path, old: str, new: str, *, restore_mtime_ns: bool = True) -> None:
    """Verify-report reproducer shape (audit MC-P1-02): replace `old` with
    `new` (same length -- size never moves) and, unless told otherwise,
    restore the exact `mtime_ns` afterwards -- a rewrite a metadata-only
    comparison cannot distinguish from an untouched file."""
    st = path.stat()
    orig_ns = st.st_mtime_ns
    text = path.read_text()
    new_text = text.replace(old, new)
    assert new_text != text, "fixture bug: nothing to rewrite"
    assert len(new_text) == len(text), "fixture bug: rewrite must be same length"
    path.write_text(new_text)
    if restore_mtime_ns:
        os.utime(path, ns=(orig_ns, orig_ns))


class TestContentProvenFreshness(unittest.TestCase):
    """audit MC-P1-02 / design R3 (TOP-0123 L3), decision side: content is
    hashed where a NEGATIVE claim is made (`check`, `unmapped`'s self-heal)
    -- readers (`search`/`chain`/`for-path`/`why`/`drift`, via
    `decision_index_state` with no override) keep the metadata-only
    comparison. Reproducer shape from the verify report: replace every
    `alpha` with `bravo` (same length) in a topic's body, then restore the
    exact `mtime_ns`."""

    def test_same_size_rewrite_is_drift_under_check_and_unmapped_self_heals(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            p = root / "topics" / "t.md"
            _write_topic(p, "TOP-9500", "alpha")
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                reindex(root, db, no_embed=True)

            _rewrite_same_size(p, "alpha", "bravo")

            check_buf = io.StringIO()
            with contextlib.redirect_stdout(check_buf):
                rc = memidx.cmd_check(
                    ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root), json=True)
                )
            report = json.loads(check_buf.getvalue())
            self.assertTrue(report["drift"], report)
            # Fix round 3: walk_markdown resolves `root` before it ever
            # walks it, so the stored (and reported) path is the
            # RESOLVED one -- on macOS, td (from tempfile) sits under
            # /var/folders/..., a symlink to /private/var/folders/...,
            # so `p` itself must be resolved before comparison.
            self.assertIn(str(p.resolve()), report["changed"], report)
            self.assertEqual(rc, 1)

            # unmapped's self-heal (state == "stale", content-proven) must
            # reindex the new content -- proven by a search for "bravo"
            # afterwards actually hitting.
            with contextlib.redirect_stdout(io.StringIO()):
                memidx.cmd_unmapped(ns(
                    db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root),
                    code_root=None, paths=[], json=True,
                ))

            hits = _run_search(ns(
                db=str(db), project=memidx.DEFAULT_PROJECT, query="bravo", mode="fts",
                status=[], type=[], area=None, topic=None, authority=None, limit=5, json=True,
            ))
            self.assertTrue(hits, "unmapped's self-heal must have re-indexed the new content")

    def test_pure_touch_is_not_drift_and_refreshes_bookkeeping(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            p = root / "topics" / "t.md"
            _write_topic(p, "TOP-9501", "alpha")
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                reindex(root, db, no_embed=True)

            new_time = time.time() + 5
            os.utime(p, (new_time, new_time))

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_check(
                    ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root), json=True)
                )
            self.assertEqual(rc, 0, buf.getvalue())
            report = json.loads(buf.getvalue())
            self.assertFalse(report["drift"], report)

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            # Fix round 3: records.path is stored resolved (walk_markdown
            # resolves `root` up front) -- see the matching comment in
            # test_same_size_rewrite_is_drift_under_check_and_unmapped_self_heals.
            row = conn.execute(
                "SELECT mtime FROM records WHERE path=?", (str(p.resolve()),)
            ).fetchone()
            conn.close()
            self.assertAlmostEqual(row["mtime"], new_time, delta=1.0)

            # a following reader (no --verify-content) still reads current
            # off the refreshed bookkeeping -- no residual false drift.
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "current"
            )

    def test_reader_stays_metadata_only_while_check_proves(self):
        """The documented boundary: a reader opts into --root but never
        hashes (design R3) -- `search --root` on the exact same rewrite
        that `check` catches still reads `current`. Both halves asserted
        so the boundary between "reader" and "proof" stays explicit."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            p = root / "topics" / "t.md"
            _write_topic(p, "TOP-9502", "alpha")
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stdout(io.StringIO()):
                reindex(root, db, no_embed=True)

            _rewrite_same_size(p, "alpha", "bravo")

            # the reader: metadata-only, still "current" -- a documented
            # limitation, not a bug.
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "current"
            )
            # the proof: check hashes and finds the drift.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_check(
                    ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root), json=True)
                )
            self.assertEqual(rc, 1, buf.getvalue())


class TestF1DecisionIndexState(unittest.TestCase):
    """F1 (coordinator ruling 68): the five-state model, the noncreating
    mode=rw opener, the reindex stamp/generation, and the refuse-vs-warn-
    and-proceed split every reader below it follows."""

    def test_missing_state_no_file_created(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "nope.sqlite"
            self.assertEqual(memidx.decision_index_state(db, "p"), "missing")
            self.assertFalse(db.exists())

    def test_uninitialized_state_file_exists_no_stamp_no_rows(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "empty.sqlite"
            conn = memidx.open_db(db, project="p")   # the CREATING opener -- legitimate here, this
            conn.close()                              # test builds the fixture, not exercising a reader
            self.assertEqual(memidx.decision_index_state(db, "p"), "uninitialized")

    def test_upgrade_required_rows_but_no_stamp(self):
        # a legacy db: rows exist (someone wrote them before the stamp
        # scheme existed) but last_reindexed_at was never set.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "legacy.sqlite"
            reindex(FIXTURES / "schema_filters", db, no_embed=True)
            conn = sqlite3.connect(str(db))
            conn.execute("DELETE FROM db_meta WHERE key='last_reindexed_at'")
            conn.commit(); conn.close()
            self.assertEqual(memidx.decision_index_state(db, memidx.DEFAULT_PROJECT), "upgrade-required")

    def test_upgrade_required_old_generation(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            reindex(FIXTURES / "schema_filters", db, no_embed=True)
            conn = sqlite3.connect(str(db))
            conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', '1')")
            conn.commit(); conn.close()
            self.assertEqual(memidx.decision_index_state(db, memidx.DEFAULT_PROJECT), "upgrade-required")

    def test_corrupted_generation_stamp_is_treated_as_upgrade_required_not_a_crash(self):
        # Whole-branch review item 6: int(gen_row["value"]) was unguarded
        # in decision_index_state itself -- a corrupted index_generation
        # value raised ValueError straight out of all five CLI readers
        # (search/chain/for-path/why/drift), before any of THEIR own try/
        # except got a chance to run. Same (TypeError, ValueError) guard
        # cmd_reindex's own migration probe uses; treated as older, same
        # safe direction.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            reindex(FIXTURES / "schema_filters", db, no_embed=True)
            conn = sqlite3.connect(str(db))
            conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', 'garbage')")
            conn.commit(); conn.close()
            self.assertEqual(memidx.decision_index_state(db, memidx.DEFAULT_PROJECT), "upgrade-required")

            # And through the real reader that used to crash on this: a
            # corrupted-but-otherwise-populated index still holds real,
            # trustworthy evidence (ruling 68) -- search prints the
            # upgrade-required warning and still returns its results, exit
            # 0, no traceback (never the missing/uninitialized refusal,
            # which is the only path that returns 1).
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="the",
                                           mode="fts", status=[], type=[], area=None, topic=None,
                                           authority=None, limit=10, json=True))
            self.assertEqual(rc, 0)
            self.assertIn("upgrade-required", buf_err.getvalue())
            self.assertNotIn("Traceback", buf_err.getvalue())
            json.loads(buf_out.getvalue())   # must still be valid JSON, not a crash

    def test_stamped_empty_root_reads_current(self):
        # a real, freshly-reindexed store with zero files under it must never
        # misread as anything but current.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "empty-root"; root.mkdir()
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            self.assertEqual(memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "current")

    def test_stale_state_on_disk_drift_with_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES_COPY_OF("schema_filters", td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            (root / "new-topic.md").write_text(
                "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
            )
            self.assertEqual(memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "stale")

    def test_root_less_readers_never_see_stale(self):
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES_COPY_OF("schema_filters", td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            (root / "new-topic.md").write_text(
                "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
            )
            self.assertEqual(memidx.decision_index_state(db, memidx.DEFAULT_PROJECT), "current")

    def test_search_with_root_on_a_stale_store_returns_hits_and_the_named_warning(self):
        # Final-fix-wave item 2: `--root` is now optional plumbing on
        # search/for-path/why/chain/drift -- given, a store edited since
        # the last reindex is surfaced as "stale" (a positive match still
        # returned, per ruling 68) instead of silently answering "current".
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES_COPY_OF("schema_filters", td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            (root / "new-topic.md").write_text(
                "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
            )
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                                           query="the", mode="fts", status=[], type=[], area=None,
                                           topic=None, authority=None, limit=10, json=True))
            self.assertEqual(rc, 0)
            self.assertIn(
                "search: index is stale (store changed since the last reindex); results may be outdated",
                buf_err.getvalue(),
            )
            out = json.loads(buf_out.getvalue())
            self.assertEqual(out["state"], "stale")
            self.assertGreater(len(out["results"]), 0, out)   # positive match still returned, not withheld

    def test_search_rootless_call_is_unchanged_even_when_the_store_has_drifted(self):
        # Same setup as above, but WITHOUT --root: this reader must never
        # observe "stale" (it has nothing to walk) and its --json shape
        # must stay the exact pre-existing bare list -- proving optional_
        # root really is opt-in, not a behavior change for every caller.
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES_COPY_OF("schema_filters", td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            (root / "new-topic.md").write_text(
                "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
            )
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                           query="the", mode="fts", status=[], type=[], area=None,
                                           topic=None, authority=None, limit=10, json=True))
            self.assertEqual(rc, 0)
            self.assertNotIn("stale", buf_err.getvalue())
            self.assertNotIsInstance(json.loads(buf_out.getvalue()), dict)

    def test_reindex_stamps_generation_even_with_zero_changes(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            reindex(FIXTURES / "schema_filters", db, no_embed=True)
            reindex(FIXTURES / "schema_filters", db, no_embed=True)  # second run: 0/0/0
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            self.assertIsNotNone(conn.execute("SELECT value FROM db_meta WHERE key='last_reindexed_at'").fetchone())
            gen = conn.execute("SELECT value FROM db_meta WHERE key='index_generation'").fetchone()
            self.assertEqual(int(gen["value"]), memidx.CURRENT_INDEX_GENERATION)

    def test_reindex_refuses_an_unreadable_root_before_opening_anything(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = memidx.cmd_reindex(ns(root=str(Path(td) / "does-not-exist"), db=str(db),
                                            project=memidx.DEFAULT_PROJECT, full=False,
                                            no_embed=True, auto=False))
            self.assertEqual(rc, 2)
            self.assertFalse(db.exists())

    def _ns_for(self, cmd, db):
        base = dict(project=memidx.DEFAULT_PROJECT, db=str(db), json=True)
        extra = {
            "search": dict(query="x", mode="hybrid", status=[], type=[], area=None, topic=None, authority=None, limit=10),
            "chain": dict(topic="TOP-0001"),
            "why": dict(symbol_or_path="src/x.py", code_root=None),
        }[cmd]
        base.update(extra)
        return ns(**base)

    def test_root_less_readers_refuse_missing_and_create_nothing(self):
        funcs = {"search": memidx.cmd_search, "chain": memidx.cmd_chain, "why": memidx.cmd_why}
        for name, func in funcs.items():
            with self.subTest(cmd=name), tempfile.TemporaryDirectory() as td:
                db = Path(td) / "missing.sqlite"
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    rc = func(self._ns_for(name, db))
                self.assertEqual(rc, 1, buf_err.getvalue())
                self.assertFalse(db.exists(), f"{name} must not create a db")
                self.assertIn("reindex --root", buf_err.getvalue())
                self.assertEqual(json.loads(buf_out.getvalue()), {"state": "missing", "results": []})

    def test_search_still_returns_positive_matches_under_upgrade_required(self):
        # Ruling 68's central point: an upgrade-required index still holds
        # real evidence -- refusing it entirely would throw away good
        # information the schema-generation gap does not actually taint.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            reindex(FIXTURES / "schema_filters", db, no_embed=True)
            conn = sqlite3.connect(str(db))
            conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', '1')")
            conn.commit(); conn.close()
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="the",
                                           mode="fts", status=[], type=[], area=None, topic=None,
                                           authority=None, limit=10, json=True))
            self.assertEqual(rc, 0)
            self.assertIn("upgrade-required", buf_err.getvalue())
            results = json.loads(buf_out.getvalue())
            self.assertTrue(len(results) >= 0)  # proves it queried at all, not the refusal envelope
            self.assertNotIsInstance(json.loads(buf_out.getvalue()), dict)  # not the {"state":..} refusal shape

    def test_for_path_missing_or_uninitialized_exits_3_with_named_state(self):
        # Final-fix-wave item 3: a bare `[]` under --json no longer tells a
        # direct caller WHICH non-current state it got (missing vs.
        # uninitialized) -- the envelope now names it, matching
        # _decision_reply's own {"state":..., "results": [...]} shape used
        # by every other rootless reader's missing/uninitialized refusal.
        for state_setup, label in ((lambda db: None, "missing"),
                                    (lambda db: memidx.open_db(db, project=memidx.DEFAULT_PROJECT).close(), "uninitialized")):
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                db = Path(td) / f"{label}.sqlite"
                state_setup(db)
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    rc = memidx.cmd_for_path(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                                 file_path="src/x.py", json=True))
                self.assertEqual(rc, 3)
                self.assertEqual(json.loads(buf_out.getvalue()), {"state": label, "results": []})
                self.assertIn(f"for-path: decision index {label} -- run: memidx.py reindex --root <store>",
                              buf_err.getvalue())

    def test_for_path_missing_or_uninitialized_with_chain_text_includes_chain_text_key(self):
        """Round 7 fix (Grok NIT): the rc==4 index-error branch already
        carries {"state", "results", "chain_text"} under --with-chain-text
        (test_for_path_index_error_with_chain_text_returns_object_shape);
        this rc==3 missing/uninitialized branch (_for_path_missing_reply)
        was still returning {"state", "results"} with no chain_text key at
        all -- not even the empty string every other --with-chain-text
        envelope promises. A direct --json --with-chain-text caller must
        see the same three-key object shape from every for-path refusal
        branch, not just the error one."""
        for state_setup, label in ((lambda db: None, "missing"),
                                    (lambda db: memidx.open_db(db, project=memidx.DEFAULT_PROJECT).close(), "uninitialized")):
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                db = Path(td) / f"{label}.sqlite"
                state_setup(db)
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    rc = memidx.cmd_for_path(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                                 file_path="src/x.py", json=True,
                                                 with_chain_text=True))
                self.assertEqual(rc, 3)
                self.assertEqual(json.loads(buf_out.getvalue()),
                                  {"state": label, "results": [], "chain_text": ""})

    def test_for_path_missing_non_json_stays_plain_text_with_a_named_stderr_line(self):
        # Same states, non-json mode: stdout stays exactly the pre-existing
        # plain-text line (never becomes "[]" -- item 3's "keep stdout []"
        # requirement is about the json shape, not turning the human-
        # readable mode INTO a bare list); the new stderr line is added
        # regardless of --json.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "missing.sqlite"
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_for_path(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                             file_path="src/x.py", json=False))
            self.assertEqual(rc, 3)
            self.assertEqual(buf_out.getvalue().strip(), "no topics reference this path")
            self.assertIn("for-path: decision index missing -- run: memidx.py reindex --root <store>",
                          buf_err.getvalue())

    def test_for_path_index_error_fails_open_exits_4(self):
        # A hook-facing reader must fail open on a sqlite3.OperationalError
        # even after decision_index_state already confirmed a usable state
        # -- the corrupted/broken schema is only discovered on the query
        # this function makes AFTER that check. A call-count side effect on
        # open_db_noncreating avoids needing to know cmd_for_path's exact
        # SQL: the first call (inside decision_index_state) is real and
        # succeeds; every call after that returns a connection whose
        # execute() always raises.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            reindex(FIXTURES / "schema_filters", db, no_embed=True)
            real_open = memidx.open_db_noncreating
            calls = {"n": 0}

            class _BrokenConn:
                def execute(self, *a, **kw):
                    raise sqlite3.OperationalError("no such column: link_topic_path")

                def close(self):
                    pass

            def flaky_open(*a, **kw):
                calls["n"] += 1
                return real_open(*a, **kw) if calls["n"] == 1 else _BrokenConn()

            buf = io.StringIO()
            with mock.patch.object(memidx, "open_db_noncreating", side_effect=flaky_open), \
                 contextlib.redirect_stdout(buf):
                rc = memidx.cmd_for_path(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                             file_path="src/x.py", json=True))
            self.assertEqual(rc, 4)
            self.assertEqual(json.loads(buf.getvalue()), [])

    def test_for_path_index_error_with_chain_text_returns_object_shape(self):
        """Round 6 fix: the `--json --with-chain-text` envelope is a
        promise about SHAPE ({"results": [...], "chain_text": "..."}), not
        just about the happy path -- this same
        sqlite3.OperationalError/IndexError fail-open branch must keep
        that shape when the flag is given, not fall back to the flag-less
        bare `[]`. Same flaky-open rig as
        test_for_path_index_error_fails_open_exits_4, plus
        with_chain_text=True."""
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            reindex(FIXTURES / "schema_filters", db, no_embed=True)
            real_open = memidx.open_db_noncreating
            calls = {"n": 0}

            class _BrokenConn:
                def execute(self, *a, **kw):
                    raise sqlite3.OperationalError("no such column: link_topic_path")

                def close(self):
                    pass

            def flaky_open(*a, **kw):
                calls["n"] += 1
                return real_open(*a, **kw) if calls["n"] == 1 else _BrokenConn()

            buf = io.StringIO()
            with mock.patch.object(memidx, "open_db_noncreating", side_effect=flaky_open), \
                 contextlib.redirect_stdout(buf):
                rc = memidx.cmd_for_path(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                             file_path="src/x.py", json=True,
                                             with_chain_text=True))
            self.assertEqual(rc, 4)
            self.assertEqual(json.loads(buf.getvalue()), {"results": [], "chain_text": ""})

    def test_for_path_index_error_with_chain_text_and_stale_root_includes_state(self):
        """Same failure, but with --root pointed at a store that has
        drifted since the last reindex (decision_index_state already read
        "stale" before the try block ever runs): the object shape must
        also carry "state" -- the same `state_worth_naming` gate
        (root is not None and state in (...)) the main --json branch
        uses, mirrored here rather than silently dropped in the
        exception path."""
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES_COPY_OF("schema_filters", td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            (root / "new-topic.md").write_text(
                "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
            )
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "stale"
            )

            real_open = memidx.open_db_noncreating
            calls = {"n": 0}

            class _BrokenConn:
                def execute(self, *a, **kw):
                    raise sqlite3.OperationalError("no such column: link_topic_path")

                def close(self):
                    pass

            def flaky_open(*a, **kw):
                calls["n"] += 1
                return real_open(*a, **kw) if calls["n"] == 1 else _BrokenConn()

            buf = io.StringIO()
            with mock.patch.object(memidx, "open_db_noncreating", side_effect=flaky_open), \
                 contextlib.redirect_stdout(buf):
                rc = memidx.cmd_for_path(ns(project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                                             file_path="src/x.py", json=True,
                                             with_chain_text=True))
            self.assertEqual(rc, 4)
            self.assertEqual(
                json.loads(buf.getvalue()), {"state": "stale", "results": [], "chain_text": ""}
            )

    def test_unmapped_coverage_status_per_state(self):
        cases = {
            "missing": (lambda db, root: None, "uninitialized", 1),
            "uninitialized": (lambda db, root: memidx.open_db(db, project=memidx.DEFAULT_PROJECT).close(),
                               "uninitialized", 1),
        }
        for label, (setup, expect_status, expect_rc) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                root = Path(td) / "root"; root.mkdir()
                db = Path(td) / f"{label}.sqlite"
                setup(db, root)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = memidx.cmd_unmapped(ns(project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                                                 code_root=None, json=True, paths=[]))
                self.assertEqual(rc, expect_rc)
                self.assertEqual(json.loads(buf.getvalue())["coverage_status"], expect_status)

    def test_unmapped_upgrade_required_does_not_self_heal(self):
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES_COPY_OF("schema_filters", td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = sqlite3.connect(str(db))
            conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', '1')")
            conn.commit(); conn.close()
            mtime_before = db.stat().st_mtime
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_unmapped(ns(project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                                        code_root=None, json=True, paths=[]))
            out = json.loads(buf.getvalue())
            self.assertEqual(out["coverage_status"], "upgrade-required")
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            gen = conn.execute("SELECT value FROM db_meta WHERE key='index_generation'").fetchone()
            self.assertEqual(gen["value"], "1", "self-heal must not have reindexed -- that is the rollout's job")

    def test_unmapped_index_error_fails_open_coverage_status(self):
        # NOTE (deviation from the brief's literal test text): `paths` is
        # required here -- unlike the two states above, "current" state
        # proceeds into the per-path classify loop, which is where the
        # mocked _BrokenConn.execute() actually raises. Without at least
        # one path the loop body (and therefore the fault) never runs.
        with tempfile.TemporaryDirectory() as td:
            root = FIXTURES / "schema_filters"
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            real_open = memidx.open_db_noncreating
            calls = {"n": 0}

            class _BrokenConn:
                def execute(self, *a, **kw):
                    raise sqlite3.OperationalError("no such column: link_topic_path")

                def close(self):
                    pass

            def flaky_open(*a, **kw):
                calls["n"] += 1
                return real_open(*a, **kw) if calls["n"] == 1 else _BrokenConn()

            buf = io.StringIO()
            with mock.patch.object(memidx, "open_db_noncreating", side_effect=flaky_open), \
                 contextlib.redirect_stdout(buf):
                rc = memidx.cmd_unmapped(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                             root=str(root), code_root=None, json=True,
                                             paths=["src/x.py"]))
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["coverage_status"], "index-error")
            # Design R7 (audit MC-P2-03, TOP-0123 L7): this arm is UNCHANGED --
            # no `degraded` object, only the broad `except Exception` branch
            # (a genuine programmer bug or non-operational DB failure) gets one.
            self.assertNotIn("degraded", out)


class TestUnmappedMultipleCodeRoots(unittest.TestCase):
    """Design R5 (audit MC-P1-05, TOP-0123 L5): `unmapped --code-root`
    becomes repeatable (action="append", via a `_code_roots_arg` helper
    accepting `str | list | None`); per path the LONGEST matching resolved
    root wins (nested roots)."""

    def test_unmapped_tries_every_code_root_longest_wins_and_string_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            root_a = Path(td) / "root-a"; root_a.mkdir()
            root_b = Path(td) / "root-b"; root_b.mkdir()
            nested_b = root_a / "nested-b"; nested_b.mkdir()
            for r, name in ((root_b, "b.py"), (nested_b, "n.py")):
                (r / "src").mkdir()
                (r / "src" / name).write_text(f"# {name}\n")

            store = Path(td) / "store"
            (store / "topics").mkdir(parents=True)
            (store / "topics" / "t.md").write_text(
                "---\nid: TOP-1\ntitle: T\nstatus: active\ncode_refs:\n"
                "  - src/b.py\n  - src/n.py\n---\n\nBody.\n"
            )
            db = Path(td) / "idx.sqlite"
            reindex(store, db, no_embed=True)

            fpath_b = str((root_b / "src" / "b.py").resolve())
            fpath_n = str((nested_b / "src" / "n.py").resolve())

            # append: a file physically under the SECOND --code-root is
            # still mapped (its code_refs entry is repo-relative to
            # root_b, invisible to root_a alone -- this is the concrete
            # false-gap regression the audit's acceptance list names).
            # Nested roots: nested_b is ALSO a configured --code-root,
            # sitting INSIDE root_a -- the LONGEST (most specific) resolved
            # root among the ones actually configured must win the
            # relativisation (root_a alone would give "nested-b/src/n.py",
            # which does not match code_refs' "src/n.py" -- a false gap).
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_unmapped(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), root=str(store),
                    code_root=[str(root_a), str(root_b), str(nested_b)], json=True,
                    paths=[fpath_b, fpath_n],
                ))
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 0, out)
            self.assertEqual(out["unmapped"], [], out)
            self.assertCountEqual(out["mapped_topic"], ["src/b.py", "src/n.py"], out)
            self.assertEqual(
                out["roots"],
                [str(root_a.resolve()), str(root_b.resolve()), str(nested_b.resolve())],
            )

            # SimpleNamespace(code_root=str) -- the pre-append single-value
            # shape -- still works unchanged.
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                rc2 = memidx.cmd_unmapped(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), root=str(store),
                    code_root=str(root_b), json=True, paths=[fpath_b],
                ))
            out2 = json.loads(buf2.getvalue())
            self.assertEqual(rc2, 0, out2)
            self.assertEqual(out2["mapped_topic"], ["src/b.py"], out2)
            self.assertEqual(out2["roots"], [str(root_b.resolve())])


class TestF2EmbeddingMode(unittest.TestCase):
    """F2 (coordinator ruling 69): embed_sha provenance, the freshness join
    excluding a stale vector from ranking, --auto never writing
    embedding_mode, and the none/partial/full mode derived from real
    coverage on every non-auto reindex."""

    def _topic(self, td, text="alpha decision"):
        # NOTE (deviation from the brief's literal fixture, verified
        # empirically): embed_text_for(rec) is title+body only (pre-round,
        # unchanged by this task) -- it never reads ruling_text. The
        # brief's own fixture put the varying `text` param into the
        # ruling field and left the body constant ("body text\n"), so a
        # test asserting the RE-embedded vector differs after a content
        # edit would never see any change to the actual embedded input.
        # `text` goes into the body here instead; the ruling field is a
        # fixed placeholder. The existing `.replace("alpha decision", ...)`
        # callers below still work unchanged -- they match the whole file
        # text, not a specific frontmatter field.
        root = Path(td) / "root"; (root / "topics").mkdir(parents=True)
        p = root / "topics" / "t.md"
        p.write_text(
            "---\ntype: topic\nid: TOP-1\ntitle: T\nlinks:\n"
            "  - link: L1\n    status: active\n    ruling: {text: \"r\", authority: owner-verbatim, source: s}\n"
            f"---\n{text}\n"
        )
        # Resolved, not raw: cmd_reindex canonicalises root via .resolve()
        # before it ever touches disk, so every path it stores/returns is
        # already resolved. A raw fixture root (tempfile's TMPDIR form --
        # macOS's /var/folders/... is itself a symlink to
        # /private/var/folders/...) would make every str(root / ...) built
        # from it disagree with what actually landed in the db. Comparing
        # raw against the engine's output was a Linux-only assumption: /tmp
        # is not usually symlinked there, so raw happened to equal resolved.
        return root.resolve()

    def _emb(self, db, path):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT embed_sha, vector FROM embeddings WHERE path=?", (path,)).fetchone()
        conn.close(); return row

    def _rec_sha(self, db, path):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT sha256 FROM records WHERE path=?", (path,)).fetchone()
        conn.close(); return r["sha256"]

    def _mode(self, db):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT value FROM db_meta WHERE key='embedding_mode'").fetchone()
        conn.close(); return r["value"] if r else "none"

    def test_stale_vector_is_kept_physically_but_excluded_from_ranking(self):
        # Ruling 69's central point, tested through the REAL search path
        # (not just the embeddings table) -- a stale vector must never
        # rank at all, not merely rank lower.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td, text="the widget cache invalidates on write"); db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            path = str(root / "topics" / "t.md")
            row1 = self._emb(db, path)
            self.assertIsNotNone(row1)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            fresh_ranked = memidx.vector_ranked(conn, "widget cache invalidates on write", memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertTrue(any(p == path for p, _ in fresh_ranked),
                             "a fresh vector must actually rank -- positive control for the JOIN below")
            (root / "topics" / "t.md").write_text(
                (root / "topics" / "t.md").read_text().replace(
                    "the widget cache invalidates on write", "an unrelated sentence about nothing"
                )
            )
            reindex(root, db, no_embed=True)
            row2 = self._emb(db, path)
            self.assertIsNotNone(row2, "a changed record's embedding must be kept physically, not deleted, under --no-embed")
            self.assertEqual(row2["vector"], row1["vector"])
            self.assertNotEqual(row2["embed_sha"], self._rec_sha(db, path), "the kept vector is now stale")
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            ranked = memidx.vector_ranked(conn, "widget cache invalidates on write", memidx.DEFAULT_PROJECT)
            self.assertFalse(any(p == path for p, _ in ranked),
                              "a stale vector must be invisible to ranking, not merely scored lower")
            reindex(root, db, no_embed=False)
            row3 = self._emb(db, path)
            self.assertEqual(row3["embed_sha"], self._rec_sha(db, path))
            self.assertNotEqual(row3["vector"], row1["vector"])
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            reranked = memidx.vector_ranked(conn, "unrelated sentence about nothing", memidx.DEFAULT_PROJECT)
            conn.close()
            self.assertTrue(any(p == path for p, _ in reranked),
                             "the re-embedded row must be fresh again and rank -- positive control")

    def test_mode_tracks_none_partial_full(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td); db = Path(td) / "idx.sqlite"
            second = root / "topics" / "second.md"
            second.write_text(
                "---\ntype: topic\nid: TOP-2\ntitle: T2\nlinks:\n"
                "  - link: L1\n    status: active\n    ruling: {text: \"second\", authority: owner-verbatim, source: s}\n"
                "---\nbody\n"
            )
            reindex(root, db, no_embed=False)
            self.assertEqual(self._mode(db), "full")
            second.write_text(second.read_text().replace("second", "second-edited"))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reindex(root, db, no_embed=True)   # one of two records now stale
            self.assertEqual(self._mode(db), "partial")
            self.assertIn("embedding mode set to partial", buf.getvalue())
            reindex(root, db, no_embed=False)
            self.assertEqual(self._mode(db), "full")

    def test_mode_reaches_none_when_no_record_has_a_fresh_vector(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td); db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            (root / "topics" / "t.md").write_text(
                (root / "topics" / "t.md").read_text().replace("alpha decision", "gamma decision")
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reindex(root, db, no_embed=True)
            self.assertEqual(self._mode(db), "none")
            self.assertIn("embedding mode set to none", buf.getvalue())

    def test_auto_recomputes_mode_when_it_actually_changed_a_record(self):
        # F2/Ruling 73 (binding, supersedes this test's earlier claim that
        # --auto NEVER writes embedding_mode): an --auto run that actually
        # changed a row must recompute embedding_mode from real coverage --
        # `full` must never keep standing over a vector that this very
        # --auto pass just made stale. Two topics so "some but not all
        # fresh" (partial) is reachable: edit only the second one.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td); db = Path(td) / "idx.sqlite"
            second = root / "topics" / "second.md"
            second.write_text(
                "---\ntype: topic\nid: TOP-2\ntitle: T2\nlinks:\n"
                "  - link: L1\n    status: active\n    ruling: {text: \"second\", authority: owner-verbatim, source: s}\n"
                "---\nbody\n"
            )
            reindex(root, db, no_embed=False)
            self.assertEqual(self._mode(db), "full")
            second.write_text(second.read_text().replace("second", "second-edited"))
            args = ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                       full=False, no_embed=True, auto=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_reindex(args)
            self.assertEqual(self._mode(db), "partial",
                              "an --auto pass that changed a row must recompute mode from real coverage")
            self.assertIn("embedding mode set to partial", buf.getvalue())

    def test_auto_still_never_writes_mode_when_nothing_changed(self):
        # The part of the old contract that DOES survive Ruling 73: a
        # genuine no-op --auto pass (nothing added/changed/removed) still
        # never touches embedding_mode at all.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td); db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            self.assertEqual(self._mode(db), "full")
            args = ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                       full=False, no_embed=True, auto=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_reindex(args)   # nothing changed on disk
            self.assertEqual(self._mode(db), "full")
            self.assertNotIn("embedding mode set to", buf.getvalue())

    def test_unchanged_sha_skip_still_requires_a_matching_embed_sha(self):
        # the skip predicate is sha AND embed_sha match -- not sha alone.
        # 2, not 1: the fixture topic carries one link (F5 adds a link row
        # alongside the topic row), and both need backfilling here.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td); db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)   # record exists, no embedding at all
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reindex(root, db, no_embed=False)
            self.assertIn("2 embedding(s) backfilled", buf.getvalue())
            path = str(root / "topics" / "t.md")
            self.assertIsNotNone(self._emb(db, path))


class TestRuling80EmbeddingFailureFailsOpen(unittest.TestCase):
    """Final-fix-wave item 4 (ruling 80): a broken/missing embedding
    backend must never crash reindex or search -- it fails open, exactly
    like a --no-embed run, with one named stderr line."""

    def _topic(self, td):
        root = Path(td) / "root"; (root / "topics").mkdir(parents=True)
        (root / "topics" / "t.md").write_text(
            "---\ntype: topic\nid: TOP-1\ntitle: needle\nlinks:\n"
            "  - link: L1\n    status: active\n"
            "    ruling: {text: needle term, authority: owner-verbatim, source: s}\n"
            "---\nneedle body text\n"
        )
        return root

    def test_reindex_with_a_broken_embedding_backend_exits_0_with_mode_none(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            db = Path(td) / "idx.sqlite"
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch("fastembed.TextEmbedding", side_effect=RuntimeError("no backend")), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                            full=False, no_embed=False, auto=False))
            self.assertEqual(rc, 0)
            self.assertIn(
                "reindex: embeddings unavailable (RuntimeError: no backend); "
                "continuing without embeddings",
                buf_err.getvalue(),
            )
            # rows still committed despite the embedding failure
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT COUNT(*) AS n FROM records WHERE project=?",
                                 (memidx.DEFAULT_PROJECT,)).fetchone()
            self.assertGreater(rows["n"], 0)
            mode_row = conn.execute("SELECT value FROM db_meta WHERE key='embedding_mode'").fetchone()
            conn.close()
            # A missing row means "none" (same convention cmd_reindex's own
            # mode_now default uses -- a brand-new db that has never had a
            # SUCCESSFUL embed pass never had a reason to write the key).
            self.assertEqual(mode_row["value"] if mode_row else "none", "none")

    def test_reindex_broken_backend_does_not_prevent_a_later_healthy_reindex(self):
        # the failed pass must not poison the db so a real embed run later
        # can't recover it (e.g. wrongly stamping something that blocks a
        # real embed pass from ever recomputing "full").
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            db = Path(td) / "idx.sqlite"
            with mock.patch("fastembed.TextEmbedding", side_effect=RuntimeError("no backend")):
                memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                       full=False, no_embed=False, auto=False))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                            full=True, no_embed=False, auto=False))
            self.assertEqual(rc, 0)
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            mode = conn.execute("SELECT value FROM db_meta WHERE key='embedding_mode'").fetchone()
            conn.close()
            self.assertEqual(mode["value"], "full")

    def test_search_hybrid_with_a_broken_embedding_backend_falls_back_to_fts_exits_0(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)   # a real index exists, just no vectors
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch("fastembed.TextEmbedding", side_effect=RuntimeError("no backend")), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="needle",
                                           mode="hybrid", status=[], type=[], area=None, topic=None,
                                           authority=None, limit=10, json=True))
            self.assertEqual(rc, 0)
            self.assertIn("search: embeddings unavailable; falling back to FTS-only", buf_err.getvalue())
            out = json.loads(buf_out.getvalue())
            self.assertEqual(out["embedding"], "unavailable")
            self.assertGreater(len(out["results"]), 0, out)   # the FTS-only fallback still finds the match

    def test_search_vector_mode_with_a_broken_embedding_backend_falls_back_to_fts_exits_0(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch("fastembed.TextEmbedding", side_effect=RuntimeError("no backend")), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="needle",
                                           mode="vector", status=[], type=[], area=None, topic=None,
                                           authority=None, limit=10, json=True))
            self.assertEqual(rc, 0)
            self.assertIn("search: embeddings unavailable; falling back to FTS-only", buf_err.getvalue())
            out = json.loads(buf_out.getvalue())
            self.assertEqual(out["embedding"], "unavailable")
            self.assertGreater(len(out["results"]), 0, out)


class TestF3TrustModel(unittest.TestCase):
    SYNTHETIC_TOPIC = """---
type: topic
id: TOP-9100
title: Synthetic violations
links:
  - link: L1
    status: active
    ruling: {text: "r", authority: owner-verbatim, source: s}
    rationale: {text: "why", authority: not-a-real-authority}
    alternatives:
      - {option: "o", rejected_because: "b", authority: also-not-real}
    invariant: {kind: not-a-real-kind, pattern: "("}
  - link: L1
    status: active
    ruling: {text: "dup", authority: owner-verbatim, source: s}
    reverses: L99
    reason_for_change: new-evidence
    superseded_by: L98
---
Body.
"""

    def test_synthetic_topic_produces_at_least_five_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "topics" / "reference"; root.mkdir(parents=True)
            (root / "synthetic.md").write_text(self.SYNTHETIC_TOPIC)
            errors, warnings = memlint.lint_root(Path(td))
            self.assertGreaterEqual(len(errors), 5, errors)

    def test_single_definition_zero_matches_is_a_violation(self):
        with tempfile.TemporaryDirectory() as td:
            code_root = Path(td) / "code"; code_root.mkdir()
            (code_root / "a.py").write_text("x = 1\n")
            hits = memidx.check_invariant(code_root, {"kind": "single-definition", "pattern": "def gate\\("})
            self.assertEqual(hits, ["<no definition found>"])

    def test_unknown_kind_is_skipped_not_a_traceback(self):
        with self.assertRaises(memidx.InvariantSkipped):
            memidx.check_invariant(Path("."), {"kind": "bogus-kind", "pattern": "x"})

    def test_invalid_regex_is_skipped_not_a_traceback(self):
        with self.assertRaises(memidx.InvariantSkipped):
            memidx.check_invariant(Path("."), {"kind": "no-bypass", "pattern": "("})

    def test_must_call_without_scope_is_skipped(self):
        with self.assertRaises(memidx.InvariantSkipped):
            memidx.check_invariant(Path("."), {"kind": "must-call", "pattern": "x"})

    def test_drift_never_raises_and_names_each_skip_reason(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"; (store / "topics" / "reference").mkdir(parents=True)
            topic = """---
type: topic
id: TOP-9200
title: Four skip cases
links:
  - {link: L1, status: active, ruling: {text: r, authority: owner-verbatim, source: s}, invariant: {kind: bogus, pattern: "x"}}
  - {link: L2, status: active, ruling: {text: r, authority: owner-verbatim, source: s}, invariant: {kind: no-bypass, pattern: "("}}
  - {link: L3, status: active, ruling: {text: r, authority: owner-verbatim, source: s}, invariant: {kind: must-call, pattern: "x"}}
  - {link: L4, status: active, ruling: {text: r, authority: agent-inference, source: s}, invariant: {kind: no-bypass, pattern: "unlink\\\\("}}
---
Body.
"""
            (store / "topics" / "reference" / "t.md").write_text(topic)
            db = Path(td) / "idx.sqlite"
            reindex(store, db, no_embed=True)
            code_root = Path(td) / "code"; code_root.mkdir(); (code_root / "a.py").write_text("x=1\n")
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                     code_root=str(code_root), json=True))
            out = json.loads(buf_out.getvalue())
            self.assertEqual(len(out["skipped"]), 4, out)
            reasons = {s["reason"] for s in out["skipped"]}
            self.assertTrue(any("unknown kind" in r for r in reasons), reasons)
            self.assertTrue(any("invalid regex" in r for r in reasons), reasons)
            self.assertTrue(any("missing scope" in r for r in reasons), reasons)
            self.assertIn("authority", reasons)

    def test_constraint_authority_is_always_enforced(self):
        row = {"status": "active", "ruling_authority": "owner-verbatim", "evidence": None}
        self.assertEqual(memidx.invariant_enforcement_class(row), "constraint")

    def test_evidence_bearing_reviewer_finding_is_a_hold(self):
        row = {"status": "active", "ruling_authority": "reviewer-finding",
               "evidence": json.dumps(["commit abc123"])}
        self.assertEqual(memidx.invariant_enforcement_class(row), "hold")

    def test_reviewer_finding_without_evidence_is_context_not_hold(self):
        row = {"status": "active", "ruling_authority": "reviewer-finding", "evidence": None}
        self.assertEqual(memidx.invariant_enforcement_class(row), "context")

    def test_blank_string_evidence_counts_as_empty(self):
        # Ruling 70: evidence must be VALIDATED content, not merely tested
        # for truthiness -- a list of blank strings is [] in every way
        # that matters.
        row = {"status": "active", "ruling_authority": "code-derived",
               "evidence": json.dumps(["", "   "])}
        self.assertEqual(memidx.invariant_enforcement_class(row), "context")

    def test_scalar_evidence_is_not_a_validated_list(self):
        # Same agreement point as memlint's own scalar-evidence test: a bare
        # JSON-encoded string ("evidence" stored as a scalar, not a list) is
        # not the "parsed list of non-blank strings" ruling 70 requires.
        row = {"status": "active", "ruling_authority": "reviewer-finding",
               "evidence": json.dumps("commit abc123")}
        self.assertEqual(memidx.invariant_enforcement_class(row), "context")

    def test_agent_inference_with_validated_evidence_is_a_hold(self):
        # Ruling 76 (overrides this task's original agent-inference
        # exclusion): agent-inference is HOLD-eligible exactly like
        # reviewer-finding/code-derived -- validated evidence makes it a
        # HOLD, same as any other HOLD-eligible authority.
        row = {"status": "active", "ruling_authority": "agent-inference",
               "evidence": json.dumps(["commit abc123 -- an agent-inference link with real evidence"])}
        self.assertEqual(memidx.invariant_enforcement_class(row), "hold")

    def test_agent_inference_without_evidence_is_still_context(self):
        # Ruling 76 does not make agent-inference unconditionally
        # enforceable -- eligibility ALWAYS requires validated evidence,
        # same as reviewer-finding/code-derived.
        row = {"status": "active", "ruling_authority": "agent-inference", "evidence": None}
        self.assertEqual(memidx.invariant_enforcement_class(row), "context")

    def test_non_active_status_is_always_context(self):
        row = {"status": "superseded", "ruling_authority": "owner-verbatim", "evidence": None}
        self.assertEqual(memidx.invariant_enforcement_class(row), "context")

    def test_drift_strict_holds_fails_only_under_the_flag(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"; (store / "topics" / "reference").mkdir(parents=True)
            topic = """---
type: topic
id: TOP-9300
title: Hold case
links:
  - link: L1
    status: active
    ruling: {text: r, authority: reviewer-finding, source: s}
    evidence: ["commit abc123 -- verified the bypass exists"]
    invariant: {kind: no-bypass, pattern: "unsafe_call\\\\("}
---
Body.
"""
            (store / "topics" / "reference" / "t.md").write_text(topic)
            db = Path(td) / "idx.sqlite"
            reindex(store, db, no_embed=True)
            code_root = Path(td) / "code"; code_root.mkdir()
            (code_root / "a.py").write_text("unsafe_call(1)\n")   # trips the invariant

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=True, strict_holds=False))
            out = json.loads(buf.getvalue())
            self.assertEqual(len(out["hold_violations"]), 1, out)
            self.assertEqual(out["violations"], [])
            self.assertEqual(rc, 0, "a hold-violation must not fail the exit without --strict-holds")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=True, strict_holds=True))
            self.assertEqual(rc, 1, "the same hold-violation must fail the exit under --strict-holds")

    def test_drift_agent_inference_with_evidence_is_a_hold_violation(self):
        # Ruling 76, end-to-end through cmd_drift: an agent-inference link
        # WITH validated evidence trips the invariant like any other
        # HOLD-eligible authority -- hold_violations, not skipped; rc 0
        # without --strict-holds, rc 1 with it.
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"; (store / "topics" / "reference").mkdir(parents=True)
            topic = """---
type: topic
id: TOP-9400
title: Agent-inference hold case
links:
  - link: L1
    status: active
    ruling: {text: r, authority: agent-inference, source: s}
    evidence: ["commit def456 -- an agent-inference link with real evidence"]
    invariant: {kind: no-bypass, pattern: "unsafe_call\\\\("}
---
Body.
"""
            (store / "topics" / "reference" / "t.md").write_text(topic)
            db = Path(td) / "idx.sqlite"
            reindex(store, db, no_embed=True)
            code_root = Path(td) / "code"; code_root.mkdir()
            (code_root / "a.py").write_text("unsafe_call(1)\n")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=True, strict_holds=False))
            out = json.loads(buf.getvalue())
            self.assertEqual(len(out["hold_violations"]), 1, out)
            self.assertEqual(out["violations"], [])
            self.assertEqual(out["skipped"], [])
            self.assertEqual(rc, 0)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=True, strict_holds=True))
            self.assertEqual(rc, 1)

    def test_drift_agent_inference_without_evidence_is_skipped_authority(self):
        # Same topic shape, evidence: [] -- must be skipped (authority), a
        # named CONTEXT skip, never enforced under any flag.
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"; (store / "topics" / "reference").mkdir(parents=True)
            topic = """---
type: topic
id: TOP-9401
title: Agent-inference no-evidence case
links:
  - link: L1
    status: active
    ruling: {text: r, authority: agent-inference, source: s}
    evidence: []
    invariant: {kind: no-bypass, pattern: "unsafe_call\\\\("}
---
Body.
"""
            (store / "topics" / "reference" / "t.md").write_text(topic)
            db = Path(td) / "idx.sqlite"
            reindex(store, db, no_embed=True)
            code_root = Path(td) / "code"; code_root.mkdir()
            (code_root / "a.py").write_text("unsafe_call(1)\n")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=True, strict_holds=True))
            out = json.loads(buf.getvalue())
            self.assertEqual(out["violations"], [])
            self.assertEqual(out["hold_violations"], [])
            self.assertEqual(len(out["skipped"]), 1, out)
            self.assertEqual(out["skipped"][0]["reason"], "authority")
            self.assertEqual(rc, 0)

    def test_drift_provisional_invariant_is_reported_for_revalidation(self):
        # Ruling 74: a provisional invariant is never checked and never a
        # failure -- text AND JSON both.
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"; (store / "topics" / "reference").mkdir(parents=True)
            topic = """---
type: topic
id: TOP-9500
title: Provisional invariant case
links:
  - link: L1
    status: provisional
    ruling: {text: r, authority: owner-verbatim, source: s}
    invariant: {kind: no-bypass, pattern: "unsafe_call\\\\("}
---
Body.
"""
            (store / "topics" / "reference" / "t.md").write_text(topic)
            db = Path(td) / "idx.sqlite"
            reindex(store, db, no_embed=True)
            code_root = Path(td) / "code"; code_root.mkdir()
            (code_root / "a.py").write_text("unsafe_call(1)\n")   # would trip if ever checked

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=True, strict_holds=True))
            out = json.loads(buf.getvalue())
            self.assertEqual(out["violations"], [])
            self.assertEqual(out["hold_violations"], [])
            self.assertEqual(out["skipped"], [])
            self.assertEqual(out["revalidate"],
                              [{"topic": "TOP-9500", "link": "L1", "kind": "no-bypass"}])
            self.assertEqual(rc, 0, "a provisional invariant must never fail the exit")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=False, strict_holds=True))
            text = buf.getvalue()
            self.assertIn("revalidate (provisional): TOP-9500/L1 no-bypass", text)
            self.assertEqual(rc, 0)

    def test_must_call_scope_matching_no_files_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            code_root = Path(td) / "code"; code_root.mkdir()
            (code_root / "a.py").write_text("x = 1\n")
            with self.assertRaises(memidx.InvariantSkipped) as ctx:
                memidx.check_invariant(
                    code_root,
                    {"kind": "must-call", "pattern": "x", "scope": "nowhere/*.py"},
                )
            self.assertIn("must-call scope matches no files", str(ctx.exception))
            self.assertIn("nowhere/*.py", str(ctx.exception))

    def test_drift_must_call_empty_scope_is_a_named_skip_not_a_pass(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"; (store / "topics" / "reference").mkdir(parents=True)
            topic = """---
type: topic
id: TOP-9600
title: must-call empty scope case
links:
  - link: L1
    status: active
    ruling: {text: r, authority: owner-verbatim, source: s}
    invariant: {kind: must-call, pattern: "x", scope: "nowhere/*.py"}
---
Body.
"""
            (store / "topics" / "reference" / "t.md").write_text(topic)
            db = Path(td) / "idx.sqlite"
            reindex(store, db, no_embed=True)
            code_root = Path(td) / "code"; code_root.mkdir(); (code_root / "a.py").write_text("x=1\n")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_drift(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                          code_root=str(code_root), json=True))
            out = json.loads(buf.getvalue())
            self.assertEqual(out["violations"], [])
            self.assertEqual(len(out["skipped"]), 1, out)
            self.assertIn("must-call scope matches no files", out["skipped"][0]["reason"])
            self.assertEqual(rc, 0)


class TestSharedEvidenceValidator(unittest.TestCase):
    """The "list of non-blank strings" core is one function
    (memidx.validated_evidence_list), used by both drift's classifier
    (_validated_evidence) and memlint's own check -- these five shapes
    must produce identical verdicts everywhere it's consulted."""

    SHAPES = [
        ("nonblank_list", ["real citation"], True),
        ("scalar_string", "a bare scalar, not a list", False),
        ("none", None, False),
        ("dict", {"key": "value"}, False),
        ("mixed_list", ["", "   ", "real"], True),
    ]

    def test_validated_evidence_list_core_shapes(self):
        for name, raw, expect_nonempty in self.SHAPES:
            with self.subTest(shape=name):
                result = memidx.validated_evidence_list(raw)
                self.assertEqual(bool(result), expect_nonempty, result)
                self.assertTrue(all(isinstance(e, str) and e.strip() for e in result), result)

    def test_drift_side_agrees_with_the_shared_core_via_the_stored_json_round_trip(self):
        # _validated_evidence consumes the JSON-encoded `links.evidence`
        # column value -- simulate exactly what insert_record_rows stores
        # (json.dumps(raw) if raw else None) and confirm _validated_evidence
        # matches validated_evidence_list's own verdict on the raw value.
        for name, raw, expect_nonempty in self.SHAPES:
            with self.subTest(shape=name):
                stored = json.dumps(raw) if raw else None
                row = {"evidence": stored}
                self.assertEqual(
                    bool(memidx._validated_evidence(row)), expect_nonempty, (name, stored)
                )

    def test_memlint_and_drift_agree_on_the_same_five_shapes(self):
        # Routes through the REAL checks (memlint.lint_root -> lint_topic,
        # and drift's own invariant_enforcement_class via the same
        # JSON-stored-column round trip insert_record_rows produces) --
        # not just two direct calls to the same shared function object,
        # which would pass even if lint_topic's own call site drifted from
        # validated_evidence_list tomorrow.
        for name, raw, expect_nonempty in self.SHAPES:
            with self.subTest(shape=name):
                evidence_yaml = json.dumps(raw)  # JSON is valid YAML flow syntax
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td) / "topics" / "reference"; root.mkdir(parents=True)
                    (root / "t.md").write_text(
                        "---\ntype: topic\nid: TOP-1\ntitle: T\nlinks:\n"
                        "  - link: L1\n    status: active\n"
                        "    ruling: {text: r, authority: reviewer-finding, source: s}\n"
                        f"    evidence: {evidence_yaml}\n"
                        "    invariant: {kind: no-bypass, pattern: x}\n"
                        "---\nBody.\n"
                    )
                    errors, _warnings = memlint.lint_root(Path(td))
                memlint_clean = not any("evidence" in e for e in errors)

                stored = json.dumps(raw) if raw else None
                row = {"status": "active", "ruling_authority": "reviewer-finding", "evidence": stored}
                drift_is_hold = memidx.invariant_enforcement_class(row) == "hold"

                self.assertEqual(memlint_clean, expect_nonempty, (name, errors))
                self.assertEqual(drift_is_hold, expect_nonempty, (name, row))
                self.assertEqual(memlint_clean, drift_is_hold, (name, errors, row))


class TestF4CodeRefMatches(unittest.TestCase):
    def test_suffix_collision_is_not_a_match(self):
        self.assertFalse(memidx.code_ref_matches("src/foo.py.bak", "src/foo.py"))

    def test_sibling_prefix_directory_is_not_a_match(self):
        self.assertFalse(memidx.code_ref_matches("src/core2/x.py", "src/core"))

    def test_exact_directory_containment_still_matches(self):
        self.assertTrue(memidx.code_ref_matches("src/core/x.py", "src/core"))

    def test_trailing_slash_ref_still_matches(self):
        self.assertTrue(memidx.code_ref_matches("src/core/x.py", "src/core/"))

    def test_exact_file_match_unchanged(self):
        self.assertTrue(memidx.code_ref_matches("src/x.py", "src/x.py"))

    def test_symbol_fragment_unaffected(self):
        self.assertTrue(memidx.code_ref_matches("src/x.py", "src/x.py#Foo.bar"))

    def test_topic_matches_for_path_no_longer_false_positives_on_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "topics"; root.mkdir()
            (root / "t.md").write_text(
                "---\ntype: topic\nid: TOP-1\ntitle: T\ncode_refs: [src/foo.py]\nlinks: []\n---\nBody.\n"
            )
            db = Path(td) / "idx.sqlite"
            reindex(Path(td), db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            self.assertEqual(memidx.topic_matches_for_path(conn, memidx.DEFAULT_PROJECT, "src/foo.py.bak"), [])

    def test_concept_matches_for_path_no_longer_false_positives_on_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "concepts"; root.mkdir()
            (root / "c.md").write_text(
                "---\ntype: concept\nid: CON-1\ntitle: C\n"
                "implemented_by: [src/foo.py]\ntested_by: []\ngoverned_by: []\ninvolved_in: []\n"
                "---\nBody.\n"
            )
            db = Path(td) / "idx.sqlite"
            reindex(Path(td), db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            self.assertEqual(memidx.concept_matches_for_path(conn, memidx.DEFAULT_PROJECT, "src/foo.py.bak"), [])

    def test_concept_matches_for_chunk_no_longer_false_positives_on_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "concepts"; root.mkdir()
            (root / "c.md").write_text(
                "---\ntype: concept\nid: CON-1\ntitle: C\n"
                "implemented_by: [src/foo.py]\ntested_by: []\ngoverned_by: []\ninvolved_in: []\n"
                "---\nBody.\n"
            )
            db = Path(td) / "idx.sqlite"
            reindex(Path(td), db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            self.assertEqual(
                memidx.concept_matches_for_chunk(
                    conn, memidx.DEFAULT_PROJECT, "src/foo.py.bak", "sym", "sym"
                ),
                [],
            )

    def test_check_invariant_allowed_exemption_no_longer_false_positives_on_suffix(self):
        with tempfile.TemporaryDirectory() as td:
            code_root = Path(td)
            (code_root / "src").mkdir()
            (code_root / "src" / "foo.py.bak").write_text("BADCALL should not appear\n")
            hits = memidx.check_invariant(
                code_root,
                {"kind": "no-bypass", "pattern": "BADCALL", "allowed": ["src/foo.py"]},
            )
            self.assertEqual(hits, ["src/foo.py.bak:1"])

    def test_empty_code_ref_matches_no_path(self):
        # Critical fix: an empty code_refs entry ("") used to degenerate the
        # directory check to fp.startswith(ref_path + "/") == fp.startswith("/"),
        # matching every ABSOLUTE path -- reachable because for-path receives
        # absolute hook-payload paths and memlint did not reject this shape.
        self.assertFalse(memidx.code_ref_matches("/home/x/src/foo.py", ""))

    def test_fragment_only_code_ref_matches_no_path(self):
        self.assertFalse(memidx.code_ref_matches("/home/x/src/foo.py", "#Foo"))

    def test_double_hash_code_ref_splits_on_the_first_hash_only(self):
        # "widget.js##m" is a real shape: a JavaScript private member keeps
        # its own "#" in its symbol (chunkers/treesitter.py), so a code_ref
        # naming one is "<path>#<#symbol>" -- two hashes, not one. Every
        # code_ref splitter in this codebase (memlint.py:252/420,
        # code_ref_is_named/code_ref_matches/concept_matches_for_chunk here)
        # splits on the FIRST "#" only (str.partition / str.split(..., 1)),
        # so this resolves to path "widget.js" and fragment "#m" -- the
        # private member's own symbol, hash included -- never an empty
        # fragment and never a path of "widget.js#".
        ref_str = "widget.js##m"
        ref_path, _, frag = ref_str.partition("#")
        self.assertEqual((ref_path, frag), ("widget.js", "#m"))
        self.assertTrue(memidx.code_ref_matches("widget.js", ref_str))
        self.assertFalse(memidx.code_ref_matches("widget.js.bak", ref_str))
        self.assertTrue(memidx.fragment_matches_symbol(frag, "#m", "Widget.#m"))


class TestF5LinkRows(unittest.TestCase):
    def _topic_with_two_links(self, td):
        root = Path(td) / "store"; (root / "topics").mkdir(parents=True)
        (root / "topics" / "t.md").write_text(
            "---\ntype: topic\nid: TOP-1\ntitle: Widget cache\nlinks:\n"
            "  - link: L1\n    status: active\n"
            "    ruling: {text: \"the widget cache is invalidated on every write\", authority: owner-verbatim, source: s}\n"
            "  - link: L2\n    status: declined\n"
            "    ruling: {text: \"a background sweeper thread was rejected\", authority: owner-verbatim, source: s}\n"
            "---\nBody never mentions invalidation or sweepers.\n"
        )
        # Resolved, not raw -- see TestF2EmbeddingMode._topic's comment: every
        # path this fixture's callers build from `root` must agree with what
        # cmd_reindex actually stored (it resolves internally).
        return root.resolve()

    def test_ruling_paraphrase_not_in_body_is_found_via_vector_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            ranked = memidx.vector_ranked(conn, "cache invalidated on write", memidx.DEFAULT_PROJECT)
            top3_rows = [memidx.record_row_by_path(conn, p) for p, _ in ranked[:3]]
            self.assertTrue(any(r is not None and r["link_id"] == "L1" for r in top3_rows), ranked)

    def test_link_record_key_never_collides_with_a_real_path(self):
        # Codex's specific rejection of Revision 3's f"{topic_path}#{link_id}"
        # -- # is legal in a real filename, so that scheme was NOT
        # collision-proof. A hashed surrogate can never collide with
        # anything walk_markdown yields.
        key = memidx.link_record_key("/store/topics/t.md", "L1")
        self.assertNotIn("#", key)
        self.assertTrue(key.startswith("link:"))
        self.assertNotEqual(key, "/store/topics/t.md#L1")

    def test_a_real_filename_containing_hash_never_collides_with_a_link_row(self):
        # Codex's rejection made concrete on disk (not just at the helper
        # level, per test_link_record_key_never_collides_with_a_real_path
        # above): '#' is legal in a real POSIX filename -- a topic that
        # happens to be named that way must still index as an ordinary
        # topic row, distinct from every one of its own link rows.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"; (root / "topics").mkdir(parents=True)
            weird = root / "topics" / "weird#name.md"
            weird.write_text(
                "---\ntype: topic\nid: TOP-HASH\ntitle: Weird\nlinks:\n"
                "  - link: L1\n    status: active\n"
                "    ruling: {text: some ruling text, authority: owner-verbatim, source: s}\n"
                "---\nBody.\n"
            )
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            rows = conn.execute(
                "SELECT path, type FROM records WHERE project=?", (memidx.DEFAULT_PROJECT,)
            ).fetchall()
            by_path = {r["path"]: r["type"] for r in rows}
            # resolved, not raw: reindex() canonicalises root via .resolve()
            # before storing any path (see TestF2EmbeddingMode._topic).
            self.assertEqual(by_path.get(str(weird.resolve())), "topic")
            link_paths = [p for p, t in by_path.items() if t == "link"]
            self.assertEqual(len(link_paths), 1)
            self.assertNotEqual(link_paths[0], str(weird.resolve()))
            args = ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root), json=True)
            self.assertEqual(memidx.cmd_check(args), 0)

    def test_an_active_topic_beyond_the_old_200_cap_is_still_returned(self):
        # F7, folded into F5 (ruling 71): filtering must happen INSIDE the
        # FTS query, before its own cap -- not after a 200-row pre-filter
        # cap that a flood of non-matching-status rows (now multiplied by
        # their own link rows) can push a real match behind.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "topics"; root.mkdir()
            for i in range(250):
                (root / f"decoy-{i:03}.md").write_text(
                    f"---\ntype: topic\nid: TOP-D{i}\ntitle: Decoy {i}\nstatus: superseded\nlinks:\n"
                    "  - {link: L1, status: superseded, ruling: {text: needle term, authority: owner-verbatim, source: s}}\n"
                    "---\nBody.\n"
                )
            (root / "target.md").write_text(
                "---\ntype: topic\nid: TOP-TARGET\ntitle: Target\nlinks:\n"
                "  - {link: L1, status: active, ruling: {text: needle term, authority: owner-verbatim, source: s}}\n"
                "---\nBody.\n"
            )
            db = Path(td) / "idx.sqlite"
            reindex(Path(td), db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            where, params = memidx.build_filter_clause(
                ns(project=memidx.DEFAULT_PROJECT, status=["active"], type=[], area=None, topic=None, authority=None),
                include_project=False,
            )
            ranked = memidx.fts_ranked(conn, "needle term", memidx.DEFAULT_PROJECT, where, params)
            # Assert on the FAMILY (topic path), not the raw winning
            # representative: the target topic and its own single active
            # link share near-identical ruling text here, and bm25's length
            # normalization deterministically favors the shorter link
            # document -- collapse legitimately keeps the LINK row as that
            # family's representative (records.path is a synthetic
            # "link:<hash>" key, containing no "target.md" substring at
            # all). What F7's fix actually promises is that the TARGET
            # FAMILY is not starved out by the 250-decoy flood; which
            # member represents it is an incidental tie-break, not the
            # property under test.
            families = {memidx._record_family(conn, memidx.DEFAULT_PROJECT, p) for p in ranked}
            self.assertTrue(any("target.md" in fam for fam in families), (ranked, families))

            # Fix-round item 5 (coordinator review): the families assertion
            # above proves the FAMILY survives the cap, but not that the
            # real cmd_search caller-facing path actually resolves that
            # survivor back to a topic hit ending in "target.md" -- prove
            # that end to end too.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="needle term",
                                      mode="fts", status=["active"], type=[], area=None, topic=None,
                                      authority=None, limit=10, json=True))
            out = json.loads(buf.getvalue())
            self.assertTrue(any(h["path"].endswith("target.md") for h in out), out)

    def test_a_family_beyond_the_old_raw_1000_row_cap_is_not_starved_by_a_flooding_family(self):
        # Final-fix-wave item 1 (Codex probe): the OLD `fts_ranked` fetched
        # `ORDER BY bm25(fts) LIMIT 1000` RAW rows, THEN collapsed to
        # parent-topic families, THEN capped at 200 -- so a single family
        # that alone contributes more than 1000 matching, equally-ranked-
        # ahead rows can push a second family's own single matching row
        # past the raw 1000-row line before collapse ever runs, starving it
        # out even though it matches and the true family count (2) is far
        # under the real 200-family cap. Binding order is filter -> parent
        # collapse -> cap, with no raw cap in between. Both families are
        # built here as direct records/fts rows (not via reindex over 1001
        # markdown link entries) purely so the test runs fast -- the shape
        # matches exactly what insert_record_rows would have produced for
        # a topic with 1001 links.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            conn = sqlite3.connect(str(db))
            conn.executescript(memidx.SCHEMA_SQL)
            conn.commit(); conn.close()
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)   # runs the link-column migration guards

            def _topic_row(path, tid, title):
                conn.execute(
                    "INSERT INTO records (path, sha256, mtime, size, project, type, id, "
                    "title, area, topic, status, authority, tags, code_refs, body, "
                    "ruling_text, source_path, link_topic_path, link_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path, "sha-" + tid, 0.0, 0, memidx.DEFAULT_PROJECT, "topic", tid, title,
                     None, None, "active", "owner-verbatim", "", "[]", "Body.", "",
                     path, None, None),
                )

            _topic_row("/topics/a.md", "TOP-A", "Topic A")
            _topic_row("/topics/b.md", "TOP-B", "Topic B")

            link_rows = []
            fts_rows = []
            for i in range(1001):   # family A: 1001 matching link rows, all ranking ahead of B
                p = memidx.link_record_key("/topics/a.md", f"La{i}")
                link_rows.append((p, "sha-a", 0.0, 0, memidx.DEFAULT_PROJECT, "link", "TOP-A", "Topic A",
                                   None, None, "active", "owner-verbatim", "", "[]", "", "needle",
                                   "/topics/a.md", "/topics/a.md", f"La{i}"))
                fts_rows.append((p, memidx.DEFAULT_PROJECT, "Topic A", "", "needle"))
            b_path = memidx.link_record_key("/topics/b.md", "Lb")
            # Diluted with filler terms so bm25 ranks it strictly behind
            # every one of A's exact-match rows -- deterministic, not a tie.
            b_text = "needle plus several extra filler words diluting the match score"
            link_rows.append((b_path, "sha-b", 0.0, 0, memidx.DEFAULT_PROJECT, "link", "TOP-B", "Topic B",
                               None, None, "active", "owner-verbatim", "", "[]", "", b_text,
                               "/topics/b.md", "/topics/b.md", "Lb"))
            fts_rows.append((b_path, memidx.DEFAULT_PROJECT, "Topic B", "", b_text))

            conn.executemany(
                "INSERT INTO records (path, sha256, mtime, size, project, type, id, "
                "title, area, topic, status, authority, tags, code_refs, body, "
                "ruling_text, source_path, link_topic_path, link_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                link_rows,
            )
            conn.executemany(
                "INSERT INTO fts (path, project, title, body, ruling_text) VALUES (?,?,?,?,?)",
                fts_rows,
            )
            conn.commit()

            # Sanity: confirm the intended bm25 ordering (A rows ahead of B)
            # actually holds before trusting the rest of the assertion.
            raw = conn.execute(
                "SELECT fts.path AS path FROM fts JOIN records ON records.path=fts.path "
                "WHERE fts MATCH ? AND records.project=? ORDER BY bm25(fts)",
                (memidx.fts_escape("needle"), memidx.DEFAULT_PROJECT),
            ).fetchall()
            self.assertEqual(len(raw), 1002)
            self.assertEqual(raw[-1]["path"], b_path, "test setup bug: B must rank strictly last")

            ranked = memidx.fts_ranked(conn, "needle", memidx.DEFAULT_PROJECT)
            families = {memidx._record_family(conn, memidx.DEFAULT_PROJECT, p) for p in ranked}
            self.assertIn("/topics/a.md", families, (ranked, families))
            self.assertIn("/topics/b.md", families, (ranked, families))

    def test_unfiltered_search_returns_both_topic_and_link_hits_with_no_type_given(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            rows = conn.execute("SELECT type FROM records WHERE project=?", (memidx.DEFAULT_PROJECT,)).fetchall()
            types = {r["type"] for r in rows}
            self.assertIn("topic", types); self.assertIn("link", types)

    def test_type_topic_excludes_link_rows_type_link_selects_only_them(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            only_topic = memidx.filtered_paths(conn, ns(project=memidx.DEFAULT_PROJECT, status=[], type=["topic"],
                                                          area=None, topic=None, authority=None))
            only_link = memidx.filtered_paths(conn, ns(project=memidx.DEFAULT_PROJECT, status=[], type=["link"],
                                                         area=None, topic=None, authority=None))
            self.assertEqual(len(only_topic), 1)
            self.assertEqual(len(only_link), 2)

    def test_status_active_drops_a_declined_only_match(self):
        # Fix-round item 3 (coordinator review): exercise the IN-QUERY
        # filter itself (the actual F7 mechanism -- filter_clause applied
        # inside fts_ranked's own WHERE), not a Python-side post-filter
        # reimplemented in the test that would stay green even if the
        # in-query filter regressed to a no-op.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            where, params = memidx.build_filter_clause(
                ns(project=memidx.DEFAULT_PROJECT, status=["active"], type=[], area=None, topic=None, authority=None),
                include_project=False,
            )
            ranked = memidx.fts_ranked(conn, "sweeper", memidx.DEFAULT_PROJECT, where, params)
            self.assertEqual(ranked, [], "a declined-only match must be dropped by the in-query "
                                          "status filter, never surfaced")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="sweeper",
                                      mode="fts", status=["active"], type=[], area=None, topic=None,
                                      authority=None, limit=10, json=True))
            out = json.loads(buf.getvalue())
            self.assertEqual(out, [], "cmd_search --status active must return no hits for a "
                                       "declined-only match")

    def test_search_defaults_to_status_active_and_any_widens(self):
        # search-default-active: no --status given at all (a bare Namespace
        # with status=[], exactly what argparse's own default produces)
        # must behave exactly like an explicit --status active -- a
        # declined-only match is dropped by default, with no caller ever
        # having to say so. --status any is the one way to widen back to
        # every status; any OTHER explicit value keeps meaning exactly what
        # it already means (test_status_active_drops_a_declined_only_match,
        # just above, pins that unchanged half).
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="sweeper",
                                      mode="fts", status=[], type=[], area=None, topic=None,
                                      authority=None, limit=10, json=True))
            out = json.loads(buf.getvalue())
            self.assertEqual(out, [], "no --status given must default to active-only, dropping "
                                       "a declined-only match")

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="sweeper",
                                      mode="fts", status=["any"], type=[], area=None, topic=None,
                                      authority=None, limit=10, json=True))
            out = json.loads(buf.getvalue())
            self.assertEqual(len(out), 1, "--status any must widen back to every status, "
                                           "surfacing the declined-only match")
            self.assertEqual(out[0]["status"], "declined")

    def test_inbox_records_are_typed_inbox_and_excluded_from_search_unless_included(self):
        # search-inbox-downrank: a record under inbox/ indexes as
        # type: inbox (unconditionally -- even one carrying an explicit,
        # conflicting frontmatter `type:`, since an inbox drop is never a
        # first-class record whatever it claims to be) and is excluded
        # from `search` results unless --include-inbox is given. status=
        # ["any"] throughout isolates this from search-default-active
        # (item 1): both assertions below use the SAME status filter, so
        # only include_inbox varies.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"
            (root / "topics").mkdir(parents=True)
            (root / "inbox").mkdir(parents=True)
            (root / "topics" / "t.md").write_text(
                "---\ntype: topic\nid: TOP-1\ntitle: Topic\nlinks:\n"
                "  - link: L1\n    status: active\n"
                "    ruling: {text: \"zephyr shows up in a real active decision\", "
                "authority: owner-verbatim, source: s}\n"
                "---\nBody.\n"
            )
            # No frontmatter at all -- the real shape of a consult drop
            # (inbox/grok/*.md, inbox/codex/*.md in this store's own tree).
            (root / "inbox" / "freeform.md").write_text(
                "# A consult note\nzephyr also shows up here, freeform, no frontmatter.\n"
            )
            # An inbox file that DOES carry frontmatter, deliberately
            # claiming a different type -- still must index as inbox.
            (root / "inbox" / "claims-topic.md").write_text(
                "---\ntype: topic\nid: TOP-9\ntitle: A note masquerading as a topic\n"
                "---\nzephyr, a third time, in a file that claims type: topic.\n"
            )
            root = root.resolve()
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)

            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            freeform_row = memidx.record_row_by_path(conn, str(root / "inbox" / "freeform.md"))
            claims_row = memidx.record_row_by_path(conn, str(root / "inbox" / "claims-topic.md"))
            self.assertEqual(freeform_row["type"], "inbox")
            self.assertEqual(claims_row["type"], "inbox",
                              "a path under inbox/ must index as type: inbox even when its own "
                              "frontmatter claims a different type")
            conn.close()

            def run(include_inbox):
                buf = io.StringIO()
                kwargs = dict(project=memidx.DEFAULT_PROJECT, db=str(db), query="zephyr",
                               mode="fts", status=["any"], type=[], area=None, topic=None,
                               authority=None, limit=10, json=True)
                if include_inbox:
                    kwargs["include_inbox"] = True
                with contextlib.redirect_stdout(buf):
                    memidx.cmd_search(ns(**kwargs))
                return json.loads(buf.getvalue())

            default_out = run(include_inbox=False)
            paths = [r["path"] for r in default_out]
            self.assertTrue(any(p.endswith("t.md") for p in paths), paths)
            self.assertFalse(any(p.endswith("freeform.md") or p.endswith("claims-topic.md")
                                  for p in paths), paths)

            included_out = run(include_inbox=True)
            paths = [r["path"] for r in included_out]
            self.assertTrue(any(p.endswith("freeform.md") for p in paths), paths)
            self.assertTrue(any(p.endswith("claims-topic.md") for p in paths), paths)

    def test_reindex_check_unmapped_run_twice_report_zero_second_time(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                       full=False, no_embed=True, auto=False))
            self.assertIn("0 changed", buf.getvalue()); self.assertIn("0 removed", buf.getvalue())
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            self.assertFalse(memidx._index_has_drift(conn, root, memidx.DEFAULT_PROJECT))

    def test_dedup_keeps_one_hit_per_family_in_every_mode(self):
        # Collapse now happens INSIDE fts_ranked/vector_ranked themselves
        # (ruling 71) -- their returned lists are already collapsed, no
        # separate _collapse_link_duplicates call needed by the caller.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            fts_list = memidx.fts_ranked(conn, "widget", memidx.DEFAULT_PROJECT)
            vec_list = [p for p, _ in memidx.vector_ranked(conn, "widget cache behavior", memidx.DEFAULT_PROJECT)]
            for label, lst in (("fts", fts_list), ("vector", vec_list)):
                with self.subTest(label):
                    families = [memidx._record_family(conn, memidx.DEFAULT_PROJECT, p) for p in lst]
                    self.assertEqual(len(families), len(set(families)), families)

    def test_hybrid_reports_contributing_link_ids_when_channels_disagree(self):
        # Codex's addition: a single matched_link_id is ambiguous when FTS
        # and vector retrieval matched different links of the same topic.
        # Deterministic: patch fts_ranked/vector_ranked directly so the
        # fusion logic's own disagreement-detection is under test, not
        # real embedding behavior (which this repo's other tests already
        # cover for realism).
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            topic_path = str(root / "topics" / "t.md")
            l1_path = conn.execute("SELECT path FROM records WHERE type='link' AND link_id='L1'").fetchone()["path"]
            l2_path = conn.execute("SELECT path FROM records WHERE type='link' AND link_id='L2'").fetchone()["path"]
            conn.close()
            with mock.patch.object(memidx, "fts_ranked", return_value=[l1_path]), \
                 mock.patch.object(memidx, "vector_ranked", return_value=[(l2_path, 0.9)]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="x",
                                          mode="hybrid", status=[], type=[], area=None, topic=None,
                                          authority=None, limit=10, json=True))
                out = json.loads(buf.getvalue())
            hit = next(h for h in out if h["path"].endswith("t.md"))
            self.assertEqual(hit.get("contributing_link_ids"), {"fts": "L1", "vector": "L2"})

    def test_contributing_link_ids_emitted_when_fts_picks_a_link_and_vector_picks_the_topic(self):
        # Fix-round item 4 (coordinator review): contributing_link_ids used
        # to require BOTH channels to have a link representative that
        # DIFFER -- so the mixed case (one channel's representative is the
        # plain topic row, the other's is a link row) carried neither
        # contributing_link_ids nor matched_link_id, silently dropping real
        # per-channel link information. Fixed: emit whenever AT LEAST ONE
        # channel's representative is a link row.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            topic_path = str(root / "topics" / "t.md")
            l1_path = conn.execute("SELECT path FROM records WHERE type='link' AND link_id='L1'").fetchone()["path"]
            conn.close()
            # fts's representative for the family is the LINK row; vector's
            # is the plain TOPIC row. fts is processed first, so it also
            # wins the RRF fusion -- the fused winner IS the link, so
            # matched_link_id is set too (from the winner's own row).
            with mock.patch.object(memidx, "fts_ranked", return_value=[l1_path]), \
                 mock.patch.object(memidx, "vector_ranked", return_value=[(topic_path, 0.9)]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="x",
                                          mode="hybrid", status=[], type=[], area=None, topic=None,
                                          authority=None, limit=10, json=True))
                out = json.loads(buf.getvalue())
            hit = next(h for h in out if h["path"].endswith("t.md"))
            self.assertEqual(hit.get("contributing_link_ids"), {"fts": "L1"})
            self.assertEqual(hit.get("matched_link_id"), "L1")

    def test_contributing_link_ids_emitted_when_vector_picks_a_link_and_fts_picks_the_topic(self):
        # The mirror image of the test above -- fts's representative is the
        # plain topic row (and wins the fusion, being processed first), so
        # the fused winner has no link id of its own (matched_link_id is
        # absent), but contributing_link_ids must still surface the
        # vector channel's real link-level match.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            topic_path = str(root / "topics" / "t.md")
            l1_path = conn.execute("SELECT path FROM records WHERE type='link' AND link_id='L1'").fetchone()["path"]
            conn.close()
            with mock.patch.object(memidx, "fts_ranked", return_value=[topic_path]), \
                 mock.patch.object(memidx, "vector_ranked", return_value=[(l1_path, 0.9)]):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="x",
                                          mode="hybrid", status=[], type=[], area=None, topic=None,
                                          authority=None, limit=10, json=True))
                out = json.loads(buf.getvalue())
            hit = next(h for h in out if h["path"].endswith("t.md"))
            self.assertEqual(hit.get("contributing_link_ids"), {"vector": "L1"})
            self.assertIsNone(hit.get("matched_link_id"))

    def test_link_row_hit_reports_real_topic_path_and_link_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                # search-default-active: this test's own intent is the
                # link-row field shape (path/link_status/matched_link_id),
                # not status filtering -- its query only ever matches L2,
                # which is declined, so it must widen explicitly now that
                # an empty status list defaults to active-only.
                memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="sweeper",
                                      mode="fts", status=["any"], type=[], area=None, topic=None,
                                      authority=None, limit=10, json=True))
            out = json.loads(buf.getvalue())
            hit = next(h for h in out if h.get("matched_link_id") == "L2")
            self.assertTrue(hit["path"].endswith("t.md"))
            self.assertEqual(hit["link_status"], "declined")

    def test_link_row_snippet_is_never_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            row = conn.execute("SELECT * FROM records WHERE type='link' AND link_id='L1'").fetchone()
            self.assertTrue(memidx.snippet_for(row))

    def test_migration_from_a_pre_link_column_db_triggers_one_full_reindex(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = sqlite3.connect(str(db))
            conn.execute("ALTER TABLE records RENAME TO records_v1")
            conn.execute("""CREATE TABLE records (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, mtime REAL NOT NULL,
                size INTEGER NOT NULL, project TEXT NOT NULL, type TEXT, id TEXT, title TEXT, area TEXT, topic TEXT,
                status TEXT, authority TEXT, tags TEXT, code_refs TEXT, body TEXT, ruling_text TEXT)""")
            conn.execute("""INSERT INTO records SELECT path, sha256, mtime, size, project, type, id, title, area,
                topic, status, authority, tags, code_refs, body, ruling_text FROM records_v1 WHERE type='topic'""")
            conn.execute("DROP TABLE records_v1")
            conn.execute("DELETE FROM db_meta WHERE key='last_reindexed_at'")
            conn.commit(); conn.close()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reindex(root, db, no_embed=True)
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            types = {r["type"] for r in conn.execute("SELECT type FROM records")}
            self.assertIn("link", types, "an upgrading db must grow its link rows in one pass, not wait for the next sha change")

    def test_hybrid_dedup_runs_through_the_real_fusion_site_not_just_the_helper(self):
        # A test that only calls _collapse_link_duplicates directly (as
        # test_dedup_keeps_one_hit_per_family_in_every_mode above does) would
        # stay green even if a future refactor moved the collapse call to
        # AFTER RRF fusion instead of before it (Grok's "a new filter lie"
        # failure mode) -- this one goes through the real cmd_search hybrid
        # path end to end.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                      query="widget cache invalidated", mode="hybrid",
                                      status=[], type=[], area=None, topic=None,
                                      authority=None, limit=10, json=True))
            out = json.loads(buf.getvalue())
            paths = [hit["path"] for hit in out]
            self.assertEqual(len(paths), len(set(paths)),
                              f"a topic and its own link row must never both appear in hybrid results: {out}")

    def test_migration_does_not_force_needless_reembedding_of_unchanged_topics(self):
        # Revision-1 preamble point 4 promises a migration-triggered full
        # content pass "never forces a needless re-embed" -- prove it: an
        # already-embedded topic's vector must be byte-identical after the
        # migration pass, while its brand-new link row still gets embedded.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            topic_path = str(root / "topics" / "t.md")
            original_vector = conn.execute(
                "SELECT vector FROM embeddings WHERE path=?", (topic_path,)
            ).fetchone()["vector"]
            self.assertIsNotNone(original_vector)
            # Simulate a pre-F5 schema: the topic's own record/embedding are
            # left exactly as they are; only the records table itself loses
            # the link-row shape (no link_topic_path/link_id columns, and no
            # link rows -- both removed by rebuilding the table).
            conn.execute("ALTER TABLE records RENAME TO records_v1")
            conn.execute("""CREATE TABLE records (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, mtime REAL NOT NULL,
                size INTEGER NOT NULL, project TEXT NOT NULL, type TEXT, id TEXT, title TEXT, area TEXT, topic TEXT,
                status TEXT, authority TEXT, tags TEXT, code_refs TEXT, body TEXT, ruling_text TEXT)""")
            conn.execute("""INSERT INTO records SELECT path, sha256, mtime, size, project, type, id, title, area,
                topic, status, authority, tags, code_refs, body, ruling_text FROM records_v1 WHERE type='topic'""")
            conn.execute("DROP TABLE records_v1")
            conn.execute("DELETE FROM db_meta WHERE key='last_reindexed_at'")
            conn.commit(); conn.close()
            reindex(root, db, no_embed=False)   # plain run -- migration-triggered full content pass
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            refreshed_vector = conn.execute(
                "SELECT vector FROM embeddings WHERE path=?", (topic_path,)
            ).fetchone()["vector"]
            self.assertEqual(refreshed_vector, original_vector,
                              "a migration-triggered full content pass must not re-embed an unchanged topic")
            link_row = conn.execute(
                "SELECT path FROM records WHERE type='link' AND link_id='L1'"
            ).fetchone()
            self.assertIsNotNone(link_row)
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM embeddings WHERE path=?", (link_row["path"],)).fetchone(),
                "the newly-created link row must still get its own embedding",
            )

    def test_generation_only_trigger_backfills_evidence_and_link_rows(self):
        # Ruling 75: cmd_reindex compares the stored index_generation with
        # the engine's own CURRENT_INDEX_GENERATION independently of the
        # column-presence probe above -- a db that already has the new
        # columns/link rows (this test builds one via a real reindex) but
        # whose generation stamp is behind must still get one full content
        # pass, closing Task 3's own documented gap (evidence backfill was
        # promised by that generation bump but never wired until this task).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"; (root / "topics").mkdir(parents=True)
            (root / "topics" / "t.md").write_text(
                "---\ntype: topic\nid: TOP-1\ntitle: T\nlinks:\n"
                "  - link: L1\n    status: active\n"
                "    ruling: {text: \"r\", authority: reviewer-finding, source: s}\n"
                "    evidence: [\"a real citation\"]\n"
                "---\nBody.\n"
            )
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            # resolved, not raw: reindex() canonicalises root via .resolve()
            # before storing any path (see TestF2EmbeddingMode._topic).
            topic_path = str((root / "topics" / "t.md").resolve())
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            # Columns/link rows are left completely intact -- ONLY the
            # generation stamp regresses and the evidence value is cleared,
            # isolating the generation-comparison trigger from the
            # column-presence one tested above.
            conn.execute("UPDATE links SET evidence=NULL WHERE topic_path=? AND link='L1'", (topic_path,))
            conn.execute(
                "UPDATE db_meta SET value=? WHERE key='index_generation'",
                (str(memidx.CURRENT_INDEX_GENERATION - 1),),
            )
            conn.commit(); conn.close()

            reindex(root, db, no_embed=True)   # plain run -- generation-only migration trigger

            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            ev = conn.execute(
                "SELECT evidence FROM links WHERE topic_path=? AND link='L1'", (topic_path,)
            ).fetchone()["evidence"]
            self.assertIsNotNone(ev, "the generation trigger alone must force a full pass that backfills evidence")
            self.assertIn("a real citation", ev)
            link_row = conn.execute(
                "SELECT path FROM records WHERE type='link' AND link_id='L1'"
            ).fetchone()
            self.assertIsNotNone(link_row)

    def test_check_reports_up_to_date_and_unmapped_does_not_self_heal_with_link_rows_present(self):
        # Coordinator ruling 66: _index_has_drift/cmd_check must not count
        # type='link' rows as missing source files (they have no file on
        # disk -- walk_markdown never sees them, only real topic files do).
        # A regression here would make every reindex containing link rows
        # look permanently "drifted," and unmapped's self-heal would fire
        # on every single call, silently reindexing on every hook
        # invocation. This exercises the PUBLIC cmd_check/cmd_unmapped
        # entry points directly, not just the internal _index_has_drift
        # helper (test_reindex_check_unmapped_run_twice_report_zero_second_time
        # above already covers _index_has_drift and cmd_reindex's own
        # "0 changed"/"0 removed" line; this test is the public-command-level
        # companion ruling 66 asks for).
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)   # produces 2 link rows alongside the 1 topic row
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            link_count = conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE type='link'"
            ).fetchone()["n"]
            self.assertEqual(link_count, 2, "fixture must actually produce link rows for this test to mean anything")

            check_buf = io.StringIO()
            with contextlib.redirect_stdout(check_buf):
                check_rc = memidx.cmd_check(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                                root=str(root), json=True))
            self.assertEqual(check_rc, 0)
            check_out = json.loads(check_buf.getvalue())
            self.assertEqual(check_out.get("changed", []), [])
            self.assertEqual(check_out.get("removed", []), [])

            unmapped_buf = io.StringIO()
            with contextlib.redirect_stdout(unmapped_buf):
                memidx.cmd_unmapped(ns(project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                                        code_root=None, json=True, paths=[]))
            unmapped_out = json.loads(unmapped_buf.getvalue())
            self.assertEqual(unmapped_out["coverage_status"], "ok",
                              "unmapped must not fall into its stale-index self-heal path when the "
                              "only 'drift' would have been link rows being miscounted as missing files")

    def test_link_topic_path_index_created_on_a_legacy_shaped_db(self):
        # Fix-round item 1 (coordinator review, IMPORTANT): the index
        # cannot live in SCHEMA_SQL -- executescript(SCHEMA_SQL) runs
        # BEFORE ensure_records_link_columns adds the column it would
        # index, so CREATE INDEX there would fail (or, with IF NOT EXISTS
        # racing the ALTER, silently never happen) on a legacy-shaped db
        # that predates link_topic_path. Build a db with the OLD schema
        # only (no source_path/link_topic_path/link_id at all), then open
        # it through the real migration path.
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "idx.sqlite"
            conn = sqlite3.connect(str(db))
            conn.executescript(memidx.SCHEMA_SQL)
            conn.commit(); conn.close()
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            idx_names = {r[1] for r in conn.execute("PRAGMA index_list(records)").fetchall()}
            self.assertIn("idx_records_link_topic_path", idx_names, idx_names)
            conn.close()

    def test_batched_collapse_matches_a_naive_per_row_reference(self):
        # Fix-round item 2 (coordinator review, IMPORTANT): the batched
        # family lookup inside fts_ranked/vector_ranked must produce
        # EXACTLY the same collapsed output as a naive per-row reference
        # (built here directly against the db, independent of memidx's own
        # internal helpers) -- proving the batching is a pure performance
        # change, not a behavior change.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"; (root / "topics").mkdir(parents=True)
            for i in range(30):
                (root / "topics" / f"t{i:02}.md").write_text(
                    f"---\ntype: topic\nid: TOP-{i}\ntitle: Topic {i}\nlinks:\n"
                    f"  - {{link: L1, status: active, ruling: {{text: shared needle term {i}, "
                    f"authority: owner-verbatim, source: s}}}}\n"
                    f"  - {{link: L2, status: active, ruling: {{text: shared needle term {i} again, "
                    f"authority: owner-verbatim, source: s}}}}\n"
                    "---\nBody.\n"
                )
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)

            # Final-fix-wave item 1: the oracle itself must be the
            # family-first definition (filter -> collapse -> cap, no raw
            # row cap in between) -- NOT a copy of the old buggy raw-LIMIT-
            # 1000-then-collapse ordering, or this "reference" would just
            # re-assert the bug it's meant to catch.
            raw = conn.execute(
                "SELECT fts.path AS path FROM fts JOIN records ON records.path=fts.path "
                "WHERE fts MATCH ? AND records.project=? ORDER BY bm25(fts)",
                (memidx.fts_escape("shared needle term"), memidx.DEFAULT_PROJECT),
            ).fetchall()
            seen = set(); naive = []
            for r in raw:
                p = r["path"]
                row = conn.execute("SELECT link_topic_path FROM records WHERE path=?", (p,)).fetchone()
                fam = row["link_topic_path"] or p
                if fam in seen:
                    continue
                seen.add(fam); naive.append(p)
                if len(naive) >= 200:
                    break

            actual = memidx.fts_ranked(conn, "shared needle term", memidx.DEFAULT_PROJECT)
            self.assertEqual(actual, naive)

    def test_fts_and_vector_ranked_issue_at_most_three_sql_statements_per_channel(self):
        # Fix-round item 2 (coordinator review, IMPORTANT): _collapse_link_
        # duplicates/_record_family used to issue one SELECT per ranked
        # row (up to 1000 in fts_ranked, the whole fresh corpus in
        # vector_ranked) -- must now be O(1) queries (batched), not O(rows).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "store"; (root / "topics").mkdir(parents=True)
            for i in range(30):
                (root / "topics" / f"t{i:02}.md").write_text(
                    f"---\ntype: topic\nid: TOP-{i}\ntitle: Topic {i}\nlinks:\n"
                    f"  - {{link: L1, status: active, ruling: {{text: shared needle term {i}, "
                    f"authority: owner-verbatim, source: s}}}}\n"
                    f"  - {{link: L2, status: active, ruling: {{text: shared needle term {i} again, "
                    f"authority: owner-verbatim, source: s}}}}\n"
                    "---\nBody.\n"
                )
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            # Two kinds of statement traffic this count must NOT charge to
            # fts_ranked/vector_ranked themselves: (1) FTS5's own virtual-
            # table implementation issues internal shadow-table statements
            # per MATCH query (segment b-tree traversal etc.), always
            # prefixed "--" in the trace -- filtered out below; (2) SQLite/
            # FTS5 issue a one-time PRAGMA + fts_config bootstrap the FIRST
            # time the fts5 module is touched on a connection -- a warm-up
            # call (untraced) absorbs that before the real, traced call, so
            # the count reflects each function's own PER-CALL marginal SQL,
            # which is what "batched, not one-query-per-row" actually means.
            memidx.fts_ranked(conn, "shared needle term", memidx.DEFAULT_PROJECT)
            memidx.vector_ranked(conn, "shared needle term", memidx.DEFAULT_PROJECT)
            stmts: list[str] = []
            conn.set_trace_callback(lambda sql: stmts.append(sql))
            try:
                stmts.clear()
                memidx.fts_ranked(conn, "shared needle term", memidx.DEFAULT_PROJECT)
                top = [s for s in stmts if not s.strip().startswith("--")]
                self.assertLessEqual(len(top), 3, f"fts_ranked issued {top}")
                stmts.clear()
                memidx.vector_ranked(conn, "shared needle term", memidx.DEFAULT_PROJECT)
                top = [s for s in stmts if not s.strip().startswith("--")]
                self.assertLessEqual(len(top), 3, f"vector_ranked issued {top}")
            finally:
                conn.set_trace_callback(None)

    def test_migration_probe_treats_a_corrupted_generation_stamp_as_older(self):
        # Fix-round item 6 (coordinator review): int(gen_row[0]) on a
        # corrupted stamp used to raise ValueError, escaping the probe's
        # own `except sqlite3.OperationalError` and crashing the whole
        # reindex. A non-integer stamp must be treated as older than
        # current (force the content rewrite) with a warning, not crash.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic_with_two_links(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            conn = sqlite3.connect(str(db))
            conn.execute("UPDATE db_meta SET value='garbage' WHERE key='index_generation'")
            conn.commit(); conn.close()

            err_buf = io.StringIO()
            out_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(out_buf):
                rc = memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                            full=False, no_embed=True, auto=False))
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertIn("garbage", err_buf.getvalue())

            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            gen = conn.execute("SELECT value FROM db_meta WHERE key='index_generation'").fetchone()
            self.assertEqual(gen["value"], str(memidx.CURRENT_INDEX_GENERATION))
            link_row = conn.execute("SELECT 1 FROM records WHERE type='link' AND link_id='L1'").fetchone()
            self.assertIsNotNone(link_row, "the corrupted-stamp pass must still be treated as a "
                                            "migration -- link rows must still be present")


def _write_record(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _valid_topic_text(tid: str, title: str = "Good", body: str = "Body text.") -> str:
    return (
        f"---\ntype: topic\nid: {tid}\ntitle: {title}\nlinks:\n"
        f'  - link: L1\n    status: active\n    ruling: {{text: "r", authority: owner-verbatim, source: s}}\n'
        f"---\n{body}\n"
    )


class TestMalformedRecordQuarantine(unittest.TestCase):
    """audit MC-P1-03 / design R2 (TOP-0123 L2): a malformed record must
    never crash reindex/memlint -- it is quarantined into `index_errors`,
    its neighbours stay indexed, and the run exits 0."""

    def _index_errors_rows(self, db):
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM index_errors ORDER BY path").fetchall()]
        finally:
            conn.close()

    # -- scenario 1: the audit's own reproducer, `links: [`, beside two good topics

    def test_links_flow_open_beside_two_valid_topics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            _write_record(root / "topics" / "good1.md", _valid_topic_text("TOP-9001"))
            _write_record(root / "topics" / "good2.md", _valid_topic_text("TOP-9002"))
            bad_path = root / "topics" / "bad.md"
            _write_record(bad_path, "---\ntype: topic\nid: TOP-9003\ntitle: Bad\nlinks: [\n---\nBody.\n")
            db = Path(td) / "idx.sqlite"

            out_buf, err_buf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertNotIn("Traceback", err_buf.getvalue())

            rows = self._index_errors_rows(db)
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["path"], str(bad_path.resolve()))
            diagnostics = json.loads(rows[0]["diagnostics"])
            fields = [d[0] for d in diagnostics]
            self.assertIn("links", fields, diagnostics)

            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "quarantined"
            )

            search_buf = io.StringIO()
            with contextlib.redirect_stdout(search_buf):
                rc_s = memidx.cmd_search(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                    query="Body text", mode="fts", status=[], type=[], area=None,
                    topic=None, authority=None, limit=10, json=True,
                ))
            self.assertEqual(rc_s, 0)
            out = json.loads(search_buf.getvalue())
            self.assertEqual(out.get("state"), "quarantined")
            found_paths = {r["path"] for r in out["results"]}
            self.assertIn(str((root / "topics" / "good1.md").resolve()), found_paths)
            self.assertIn(str((root / "topics" / "good2.md").resolve()), found_paths)

            check_buf = io.StringIO()
            with contextlib.redirect_stdout(check_buf):
                rc_c = memidx.cmd_check(ns(db=str(db), project=memidx.DEFAULT_PROJECT, root=str(root), json=True))
            self.assertEqual(rc_c, 0)
            report = json.loads(check_buf.getvalue())
            self.assertEqual(report["state"], "quarantined")
            self.assertEqual(len(report["quarantined"]), 1, report)
            self.assertEqual(report["quarantined"][0]["path"], str(bad_path.resolve()))

    # -- scenario 2: valid YAML, wrong shapes -- each names the offending field

    def test_valid_yaml_wrong_shapes_are_quarantined_with_field_named(self):
        cases = [
            ("links_not_a_list", "id: TOP-9101\ntype: topic\nlinks: some text\n", "links"),
            ("tags_not_a_list", "id: TOP-9102\ntype: topic\ntags: a-string\n", "tags"),
            ("code_refs_scalar", "id: TOP-9103\ntype: topic\ncode_refs: src/x.py\n", "code_refs"),
            (
                "ruling_plain_text",
                "id: TOP-9104\ntype: topic\nlinks:\n  - link: L1\n    status: active\n    ruling: plain text\n",
                "links[0].ruling",
            ),
            (
                "links_bad_elements",
                "id: TOP-9105\ntype: topic\nlinks:\n  - not-a-mapping\n  - 42\n",
                "links[0]",
            ),
        ]
        for name, fm_body, expected_field in cases:
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td) / "root"
                    _write_record(root / "topics" / "bad.md", f"---\n{fm_body}title: Bad\n---\nBody.\n")
                    db = Path(td) / "idx.sqlite"
                    err_buf = io.StringIO()
                    with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                        rc = reindex(root, db, no_embed=True)
                    self.assertEqual(rc, 0, err_buf.getvalue())
                    self.assertNotIn("Traceback", err_buf.getvalue())
                    rows = self._index_errors_rows(db)
                    self.assertEqual(len(rows), 1, rows)
                    diagnostics = json.loads(rows[0]["diagnostics"])
                    fields = [d[0] for d in diagnostics]
                    self.assertIn(expected_field, fields, diagnostics)

    # -- scenario 3: a link with no `link:` id -- must quarantine, not IntegrityError

    def test_link_with_no_id_is_quarantined_not_integrity_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            _write_record(
                root / "topics" / "bad.md",
                "---\nid: TOP-9106\ntype: topic\ntitle: Bad\nlinks:\n"
                '  - status: active\n    ruling: {text: "r", authority: owner-verbatim, source: s}\n'
                "---\nBody.\n",
            )
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertNotIn("IntegrityError", err_buf.getvalue())
            self.assertNotIn("Traceback", err_buf.getvalue())
            rows = self._index_errors_rows(db)
            self.assertEqual(len(rows), 1, rows)
            diagnostics = json.loads(rows[0]["diagnostics"])
            fields = [d[0] for d in diagnostics]
            self.assertIn("links[0].link", fields, diagnostics)

    # -- scenario 4: unreadable file / non-UTF-8 file -- quarantined naming "file"

    @unittest.skipIf(
        os.name != "posix" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        "chmod 000 is meaningless on Windows or as root",
    )
    def test_unreadable_file_is_quarantined_with_file_named(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad = root / "topics" / "bad.md"
            _write_record(bad, _valid_topic_text("TOP-9107"))
            os.chmod(bad, 0o000)
            db = Path(td) / "idx.sqlite"
            try:
                err_buf = io.StringIO()
                with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                    rc = reindex(root, db, no_embed=True)
                self.assertEqual(rc, 0, err_buf.getvalue())
                self.assertNotIn("Traceback", err_buf.getvalue())
                rows = self._index_errors_rows(db)
                self.assertEqual(len(rows), 1, rows)
                diagnostics = json.loads(rows[0]["diagnostics"])
                fields = [d[0] for d in diagnostics]
                self.assertIn("file", fields, diagnostics)
            finally:
                os.chmod(bad, 0o644)

    def test_non_utf8_file_is_quarantined_with_file_named(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad = root / "topics" / "bad.md"
            bad.parent.mkdir(parents=True, exist_ok=True)
            bad.write_bytes(b"---\ntitle: Bad\n---\n\xff\xfe broken bytes\n")
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertNotIn("Traceback", err_buf.getvalue())
            rows = self._index_errors_rows(db)
            self.assertEqual(len(rows), 1, rows)
            diagnostics = json.loads(rows[0]["diagnostics"])
            fields = [d[0] for d in diagnostics]
            self.assertIn("file", fields, diagnostics)

    # -- scenario 5: fixing the record clears the quarantine on the next reindex

    def test_fixing_the_record_clears_quarantine_next_reindex(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad_path = root / "topics" / "bad.md"
            _write_record(bad_path, "---\ntype: topic\nid: TOP-9108\ntitle: Bad\nlinks: [\n---\nBody.\n")
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                reindex(root, db, no_embed=True)
            self.assertEqual(len(self._index_errors_rows(db)), 1)

            _write_record(bad_path, _valid_topic_text("TOP-9108"))
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)
            self.assertEqual(self._index_errors_rows(db), [])
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "current"
            )
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT 1 FROM records WHERE id='TOP-9108'").fetchone()
            conn.close()
            self.assertIsNotNone(row)

    # -- scenario 6: deleting the bad file clears the quarantine on the next reindex

    def test_deleting_the_bad_file_clears_quarantine_next_reindex(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad_path = root / "topics" / "bad.md"
            _write_record(bad_path, "---\ntype: topic\nid: TOP-9109\ntitle: Bad\nlinks: [\n---\nBody.\n")
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                reindex(root, db, no_embed=True)
            self.assertEqual(len(self._index_errors_rows(db)), 1)

            bad_path.unlink()
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0)
            self.assertEqual(self._index_errors_rows(db), [])
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "current"
            )

    # -- scenario 7: `unmapped` on a quarantined store refuses the negative claim, no self-heal

    def test_unmapped_on_quarantined_store_refuses_negative_claim(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            _write_record(root / "topics" / "good.md", _valid_topic_text("TOP-9110"))
            _write_record(
                root / "topics" / "bad.md",
                "---\ntype: topic\nid: TOP-9111\ntitle: Bad\nlinks: [\n---\nBody.\n",
            )
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                reindex(root, db, no_embed=True)
            rows_before = self._index_errors_rows(db)
            self.assertEqual(len(rows_before), 1)
            seen_at_before = rows_before[0]["seen_at"]

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = memidx.cmd_unmapped(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                    code_root=None, paths=["some/file.py"], json=True,
                ))
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["coverage_status"], "quarantined")
            self.assertEqual(out["unmapped"], [])

            rows_after = self._index_errors_rows(db)
            self.assertEqual(len(rows_after), 1)
            self.assertEqual(
                rows_after[0]["seen_at"], seen_at_before,
                "unmapped must not self-heal (reindex) on a quarantined store",
            )

    # -- scenario 7b: fix wave 1, G2 (Grok MAJOR 2 / whole-branch-review
    # BLOCKING-1) -- a self-heal reindex that PURGES the covering record
    # into quarantine must never answer coverage_status: "ok" with the
    # file it used to cover listed as an uncovered gap.

    def test_unmapped_self_heal_that_creates_quarantine_maps_to_quarantined_not_gap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            covering = root / "topics" / "covering.md"
            _write_record(
                covering,
                "---\ntype: topic\nid: TOP-9112\ntitle: Covering\ncode_refs: [src/mapped.py]\nlinks:\n"
                '  - link: L1\n    status: active\n    ruling: {text: "r", authority: owner-verbatim, source: s}\n'
                "---\nBody text.\n",
            )
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc0 = reindex(root, db, no_embed=True)
            self.assertEqual(rc0, 0)
            self.assertEqual(self._index_errors_rows(db), [])

            # On-disk drift: rewrite the SAME file into a malformed shape.
            # decision_index_state must read "stale" (real content drift)
            # -- nothing has been reindexed since the edit, so it is not
            # "quarantined" yet.
            _write_record(
                covering,
                "---\ntype: topic\nid: TOP-9112\ntitle: Covering\nlinks: [\n---\nBody text.\n",
            )
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root, verify_content=True),
                "stale",
            )

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = memidx.cmd_unmapped(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                    code_root=None, paths=["src/mapped.py"], json=True,
                ))
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 1, out)
            self.assertEqual(out["coverage_status"], "quarantined", out)
            self.assertEqual(
                out["unmapped"], [],
                "the self-heal that just quarantined the covering record must never assert a "
                "false gap for the path it used to cover",
            )

            rows = self._index_errors_rows(db)
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["path"], str(covering.resolve()))

    # -- scenario 7c: codex re-gate BLOCKING 1 (ruling 134) -- canonicity
    # must be read from the block's OWN top-level indent, not column zero
    # and not a global minimum over the whole scanned text. A valid topic
    # whose entire frontmatter mapping is uniformly indented, later
    # stripped of only its closing `---`, leaves an unindented body line
    # ("Body text.") merged into the raw text handed to
    # `_raw_frontmatter_is_canonical` (no end delimiter means no separate
    # body slice at all). A global-minimum rule would anchor "top level"
    # to that stray column-zero body line and never see the real
    # (indented) id:/type: keys -- silently demoting a still fully
    # schema-conformant record to a note.

    def test_unmapped_self_heal_with_uniformly_indented_frontmatter_stays_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            covering = root / "topics" / "covering.md"
            _write_record(
                covering,
                "---\n  type: topic\n  id: TOP-9113\n  title: Covering\n  code_refs: [src/mapped.py]\n"
                "  links:\n"
                '    - link: L1\n      status: active\n      ruling: {text: "r", authority: owner-verbatim, source: s}\n'
                "---\nBody text.\n",
            )
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc0 = reindex(root, db, no_embed=True)
            self.assertEqual(rc0, 0)
            self.assertEqual(self._index_errors_rows(db), [])

            # On-disk drift: remove ONLY the closing delimiter -- the
            # frontmatter mapping itself is untouched and still fully
            # schema-conformant (id/type/links/code_refs all present).
            _write_record(
                covering,
                "---\n  type: topic\n  id: TOP-9113\n  title: Covering\n  code_refs: [src/mapped.py]\n"
                "  links:\n"
                '    - link: L1\n      status: active\n      ruling: {text: "r", authority: owner-verbatim, source: s}\n'
                "Body text.\n",
            )
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root, verify_content=True),
                "stale",
            )

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = memidx.cmd_unmapped(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                    code_root=None, paths=["src/mapped.py"], json=True,
                ))
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 1, out)
            self.assertEqual(out["coverage_status"], "quarantined", out)
            self.assertEqual(
                out["unmapped"], [],
                "a uniformly-indented canonical mapping missing only its closing delimiter "
                "must still be quarantined, never silently demoted to a note that leaves a "
                "false coverage gap",
            )

            rows = self._index_errors_rows(db)
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["path"], str(covering.resolve()))

    # -- scenario 7d: codex-final.md BLOCKING -- a column-zero YAML comment
    # must never itself become the structural indentation anchor.
    # `_raw_frontmatter_is_canonical` anchored on the block's first
    # NON-BLANK line, which the comment-carrying variant makes a `#
    # leading comment` sitting at column zero -- every real key of the
    # uniformly 4-space-indented mapping below it then reads as "deeper
    # than top" and is skipped, so a fully schema-conformant topic loses
    # its closing delimiter and is silently demoted to a note instead of
    # quarantined. Comment-only lines must never anchor and must never be
    # matched themselves; the anchor is the first line that is neither
    # blank nor a comment.

    def test_unmapped_self_heal_with_leading_comment_before_uniformly_indented_frontmatter_stays_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            covering = root / "topics" / "covering.md"
            _write_record(
                covering,
                "---\n# leading YAML comment\n    type: topic\n    id: TOP-9114\n    title: Covering\n"
                "    code_refs: [src/mapped.py]\n"
                "    links:\n"
                '        - link: L1\n          status: active\n'
                '          ruling: {text: "r", authority: owner-verbatim, source: s}\n'
                "---\nBody text.\n",
            )
            db = Path(td) / "idx.sqlite"
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                rc0 = reindex(root, db, no_embed=True)
            self.assertEqual(rc0, 0)
            self.assertEqual(self._index_errors_rows(db), [])

            # On-disk drift: remove ONLY the closing delimiter -- the
            # comment and the fully schema-conformant, uniformly-indented
            # mapping are both untouched.
            _write_record(
                covering,
                "---\n# leading YAML comment\n    type: topic\n    id: TOP-9114\n    title: Covering\n"
                "    code_refs: [src/mapped.py]\n"
                "    links:\n"
                '        - link: L1\n          status: active\n'
                '          ruling: {text: "r", authority: owner-verbatim, source: s}\n'
                "Body text.\n",
            )
            self.assertEqual(
                memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root, verify_content=True),
                "stale",
            )

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = memidx.cmd_unmapped(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                    code_root=None, paths=["src/mapped.py"], json=True,
                ))
            out = json.loads(buf.getvalue())
            self.assertEqual(rc, 1, out)
            self.assertEqual(out["coverage_status"], "quarantined", out)
            self.assertEqual(
                out["unmapped"], [],
                "a leading column-zero comment must never become the indentation anchor and "
                "must never make a uniformly-indented canonical mapping read as merely a note, "
                "leaving a false coverage gap",
            )

            rows = self._index_errors_rows(db)
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0]["path"], str(covering.resolve()))

    def test_indented_comment_before_nested_metadata_links_does_not_make_unterminated_note_canonical(self):
        """codex-final.md BLOCKING, opposite transition: a 4-space-indented
        comment sitting above an unterminated note must not become the
        indentation anchor either. The note's only frontmatter marker
        that could ever match a canonical regex is a `links:` key nested
        TWO levels deep, under `metadata:` -- never at the block's real
        top-level indent (column zero, where `title:`/`metadata:` sit).
        A buggy anchor taken from the indented comment (4 spaces) would
        make that nested `links:` (2 spaces) read as "not deeper than
        top" and wrongly flip the note to canonical/quarantined."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            note_path = root / "notes" / "note.md"
            _write_record(
                note_path,
                "---\n    # indented comment\ntitle: my note\nmetadata:\n  links:\n"
                "    - link: TOP-1\nBody, no closing delimiter.\n",
            )
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertEqual(
                self._index_errors_rows(db), [],
                "an indented comment must never become the anchor and must never let a nested "
                "metadata.links marker masquerade as top-level canonical",
            )

            result = memidx.parse_record(note_path)
            self.assertTrue(result.valid)

    # -- scenario 8: a note (no id/links/type) with malformed YAML stays indexed

    def test_note_with_malformed_yaml_stays_indexed_not_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            note_path = root / "notes" / "note.md"
            _write_record(
                note_path,
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
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertEqual(self._index_errors_rows(db), [], "a note must never be quarantined")

            result = memidx.parse_record(note_path)
            self.assertTrue(result.valid)
            self.assertTrue(result.fallback)
            self.assertEqual(result.frontmatter.get("title"), "Unterminated quote note")
            self.assertEqual(len(result.diagnostics), 1, result.diagnostics)

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT 1 FROM records WHERE title=?", ("Unterminated quote note",)).fetchone()
            conn.close()
            self.assertIsNotNone(row, "the note must still be indexed")

    # -- scenario 8b/8c/8d: fix wave 1, G1 (Grok BLOCKING 1, MINOR 6-7;
    # design R2 as amended, ruling 133) -- a note (no id/type/links-list)
    # whose OWN complex field parses to VALID YAML but the WRONG shape must
    # be indexed with that field DROPPED (never quarantined, never a
    # traceback in build_record/infer_type/memlint).

    def test_note_with_scalar_links_is_indexed_with_links_dropped_and_warned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            _write_record(root / "topics" / "good.md", _valid_topic_text("TOP-9401"))
            note_path = root / "notes" / "note.md"
            _write_record(note_path, "---\ntitle: my note\nlinks: see TOP-1\n---\nBody text.\n")
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertNotIn("Traceback", err_buf.getvalue())
            warning_lines = [l for l in err_buf.getvalue().splitlines() if "WARNING" in l]
            self.assertEqual(len(warning_lines), 1, err_buf.getvalue())
            self.assertIn("links: not a list of mappings; ignored", warning_lines[0])
            self.assertEqual(self._index_errors_rows(db), [], "a note must never be quarantined")

            result = memidx.parse_record(note_path)
            self.assertTrue(result.valid)
            self.assertNotIn("links", result.frontmatter)

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            good_row = conn.execute("SELECT 1 FROM records WHERE id='TOP-9401'").fetchone()
            note_row = conn.execute("SELECT 1 FROM records WHERE title='my note'").fetchone()
            conn.close()
            self.assertIsNotNone(good_row, "the neighbouring topic must still be indexed")
            self.assertIsNotNone(note_row, "the note itself must still be indexed")

    def test_note_with_scalar_metadata_is_indexed_with_metadata_dropped_and_warned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            _write_record(root / "topics" / "good.md", _valid_topic_text("TOP-9402"))
            note_path = root / "notes" / "note.md"
            _write_record(note_path, "---\ntitle: my note\nmetadata: foo\n---\nBody text.\n")
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertNotIn("Traceback", err_buf.getvalue())
            warning_lines = [l for l in err_buf.getvalue().splitlines() if "WARNING" in l]
            self.assertEqual(len(warning_lines), 1, err_buf.getvalue())
            self.assertIn("metadata: not a mapping; ignored", warning_lines[0])
            self.assertEqual(self._index_errors_rows(db), [])

            result = memidx.parse_record(note_path)
            self.assertTrue(result.valid)
            self.assertNotIn("metadata", result.frontmatter)

            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            note_row = conn.execute("SELECT 1 FROM records WHERE title='my note'").fetchone()
            conn.close()
            self.assertIsNotNone(note_row, "the note itself must still be indexed")

    def test_note_with_list_metadata_is_indexed_with_metadata_dropped_and_warned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            _write_record(root / "topics" / "good.md", _valid_topic_text("TOP-9403"))
            note_path = root / "notes" / "note.md"
            _write_record(note_path, "---\ntitle: my note\nmetadata: [a, b]\n---\nBody text.\n")
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertNotIn("Traceback", err_buf.getvalue())
            warning_lines = [l for l in err_buf.getvalue().splitlines() if "WARNING" in l]
            self.assertEqual(len(warning_lines), 1, err_buf.getvalue())
            self.assertIn("metadata: not a mapping; ignored", warning_lines[0])
            self.assertEqual(self._index_errors_rows(db), [])

            result = memidx.parse_record(note_path)
            self.assertTrue(result.valid)
            self.assertNotIn("metadata", result.frontmatter)

    def test_indented_links_marker_does_not_make_unterminated_note_canonical(self):
        """Grok MINOR 6: `_raw_frontmatter_is_canonical` must match only
        lines at the block's own top-level indent (its first non-blank
        line's indent -- here column zero) -- an indented `- links:` line
        inside an otherwise note-shaped, unterminated frontmatter block
        must never flip the record to canonical (and therefore to
        quarantine)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            note_path = root / "notes" / "note.md"
            _write_record(
                note_path,
                "---\ntitle: my note\n  - links:\n    - link: TOP-1\nBody, no closing delimiter.\n",
            )
            db = Path(td) / "idx.sqlite"
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 0, err_buf.getvalue())
            self.assertEqual(self._index_errors_rows(db), [], "must stay a note, never quarantined")

            result = memidx.parse_record(note_path)
            self.assertTrue(result.valid)

    # -- Fix round 1, finding A1 (BLOCKING): canonicity must be decided from
    # the RAW frontmatter text, not the post-failure {} dict, in every
    # parse-failure branch. Each of the three shapes below is a fully
    # schema-conformant topic (id, type, block-style links) undone by only
    # ONE unrelated defect -- it must still be quarantined, not silently
    # reduced to a filename-derived "note".

    def _assert_quarantined_and_unsearchable(self, root, db, bad_path):
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf), contextlib.redirect_stdout(io.StringIO()):
            rc = reindex(root, db, no_embed=True)
        self.assertEqual(rc, 0, err_buf.getvalue())
        rows = self._index_errors_rows(db)
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["path"], str(bad_path.resolve()))
        self.assertEqual(
            memidx.decision_index_state(db, memidx.DEFAULT_PROJECT, root=root), "quarantined"
        )
        search_buf = io.StringIO()
        with contextlib.redirect_stdout(search_buf):
            memidx.cmd_search(ns(
                project=memidx.DEFAULT_PROJECT, db=str(db), root=str(root),
                query="Body", mode="fts", status=[], type=[], area=None,
                topic=None, authority=None, limit=10, json=True,
            ))
        out = json.loads(search_buf.getvalue())
        self.assertEqual(out["results"], [], "a quarantined record must never appear in search")
        return rows[0]

    def test_unterminated_frontmatter_on_a_canonical_topic_is_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad_path = root / "topics" / "bad.md"
            _write_record(
                bad_path,
                "---\ntype: topic\nid: TOP-9301\ntitle: T\nlinks:\n"
                '  - link: L1\n    status: active\n    ruling: {authority: owner-verbatim, text: t, source: s}\n'
                "Body.\n",   # deliberately no closing ---
            )
            db = Path(td) / "idx.sqlite"
            self._assert_quarantined_and_unsearchable(root, db, bad_path)

    def test_yaml_error_on_a_canonical_topic_is_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad_path = root / "topics" / "bad.md"
            _write_record(
                bad_path,
                "---\ntype: topic\nid: TOP-9302\ntitle: a: b\nlinks:\n"
                '  - link: L1\n    status: active\n    ruling: {authority: owner-verbatim, text: t, source: s}\n'
                "---\nBody mentioning xyzzy123.\n",
            )
            db = Path(td) / "idx.sqlite"
            self._assert_quarantined_and_unsearchable(root, db, bad_path)

    def test_frontmatter_parsing_to_a_list_on_a_canonical_topic_is_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad_path = root / "topics" / "bad.md"
            _write_record(
                bad_path,
                "---\n- type: topic\n- id: TOP-9303\n- links:\n"
                "    - link: L1\n      status: active\n"
                '      ruling: {authority: owner-verbatim, text: t, source: s}\n'
                "---\nBody.\n",
            )
            db = Path(td) / "idx.sqlite"
            self._assert_quarantined_and_unsearchable(root, db, bad_path)

    def test_links_only_canonical_marker_under_fallback_is_quarantined(self):
        """No id:/type: at all -- `links:` alone must still make this
        canonical (finding A1 instance (a)); the fallback must also still
        name `links` in its diagnostics even though its own header line
        (`links:`) is blank (finding A2 -- the blank carve-out is for
        notes only)."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            bad_path = root / "topics" / "bad.md"
            _write_record(
                bad_path,
                "---\ntitle: a: b\nlinks:\n"
                '  - link: TOP-0001\n    status: active\n    ruling: {authority: owner-verbatim, text: something, source: s}\n'
                "---\nBody text mentioning searchable phrase xyzzy123.\n",
            )
            db = Path(td) / "idx.sqlite"
            row = self._assert_quarantined_and_unsearchable(root, db, bad_path)
            diagnostics = json.loads(row["diagnostics"])
            fields = [d[0] for d in diagnostics]
            self.assertIn("links", fields, diagnostics)

    def test_memlint_on_a1_regressions_reports_error_never_traceback(self):
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
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                with tempfile.TemporaryDirectory() as td:
                    root = Path(td)
                    _write_record(root / "topics" / "bad.md", text)
                    buf_out, buf_err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                        rc = memlint.main([str(root)])
                    self.assertEqual(rc, 1, buf_out.getvalue())
                    self.assertIn("ERROR:", buf_out.getvalue())
                    self.assertNotIn("Traceback", buf_out.getvalue())
                    self.assertNotIn("Traceback", buf_err.getvalue())

    # -- Fix round 1, finding B2 (MODERATE): a quarantine-only transition
    # (nothing added/changed/removed, only a record's own quarantine
    # status flipping) must still recompute embedding_mode under --auto.

    def test_mode_relevant_change_includes_quarantine_transitions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            topic_path = root / "topics" / "t1.md"
            _write_record(topic_path, _valid_topic_text("TOP-9601"))
            db = Path(td) / "idx.sqlite"

            # Design R4 (audit MC-P1-06): compute_embeddings gained an
            # optional `model=` param (cmd_reindex now passes the model it
            # already loaded for the fingerprint check) -- widened here,
            # same precedent as Task 3's write_file_status signature change.
            def fake_embed(texts, model=None):
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

            with mock.patch.object(memidx, "compute_embeddings", side_effect=fake_embed):
                with contextlib.redirect_stdout(io.StringIO()):
                    reindex(root, db, no_embed=False)
            self.assertEqual(self._mode(db), "full")

            # The only record becomes quarantined; nothing else changes.
            # Under --auto (no_embed forced True), mode must still drop.
            _write_record(topic_path, "---\ntype: topic\nid: TOP-9601\ntitle: Bad\nlinks: [\n---\nBody.\n")
            args = ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                      full=False, no_embed=True, auto=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                rc = memidx.cmd_reindex(args)
            self.assertEqual(rc, 0)
            self.assertIn("1 quarantined", buf.getvalue())
            self.assertEqual(
                self._mode(db), "none",
                "quarantining the only record must drop embedding_mode to none even under --auto",
            )

            # Un-quarantine it and restore full coverage with a real
            # (mocked) embedding pass.
            _write_record(topic_path, _valid_topic_text("TOP-9601"))
            with mock.patch.object(memidx, "compute_embeddings", side_effect=fake_embed):
                with contextlib.redirect_stdout(io.StringIO()):
                    rc2 = reindex(root, db, no_embed=False)
            self.assertEqual(rc2, 0)
            self.assertEqual(self._mode(db), "full")

    def _mode(self, db):
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT value FROM db_meta WHERE key='embedding_mode'").fetchone()
        conn.close()
        return row["value"] if row else None

    # -- Fix round 1, finding B5 (NIT): build_record's defensive ValueError
    # guard, directly.

    def test_build_record_raises_on_links_not_a_list(self):
        with self.assertRaises(ValueError) as ctx:
            memidx.build_record(Path("/root"), Path("/root/topics/t.md"), {"links": "not-a-list"}, "body")
        self.assertIn("links must be a list of mappings", str(ctx.exception))

    def test_build_record_raises_on_links_list_with_non_mapping_element(self):
        with self.assertRaises(ValueError):
            memidx.build_record(Path("/root"), Path("/root/topics/t.md"), {"links": ["not-a-mapping"]}, "body")


class TestEmbeddingFingerprint(unittest.TestCase):
    """Design R4 (audit MC-P1-06, TOP-0123 L4): embedding_fingerprint /
    fingerprints_match, cosine's typed dimension/non-finite errors, the
    batch-length check before the reindex zip, the freshness join's
    embed_fp/dim gate, fingerprint-mismatch handling at query time, and
    check --json's vector_index_state. Deterministic fake vectors/
    fingerprints throughout except the two tests named explicitly as
    real-model (kept few, per the brief)."""

    FP1 = "model=fake;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=rev1"
    FP2 = "model=fake;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=rev2"
    FOREIGN_FP = "model=other-model;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=revX"

    def _topic(self, td, text="alpha decision", tid="TOP-1", name="t.md"):
        root = Path(td) / "root"
        (root / "topics").mkdir(parents=True, exist_ok=True)
        p = root / "topics" / name
        p.write_text(
            f"---\ntype: topic\nid: {tid}\ntitle: T\nlinks:\n"
            "  - link: L1\n    status: active\n"
            "    ruling: {text: \"r\", authority: owner-verbatim, source: s}\n"
            f"---\n{text}\n"
        )
        return root.resolve()

    @staticmethod
    def _fake_embed(texts, model=None):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    @staticmethod
    def _fake_loader(fp):
        return lambda: (object(), fp)

    def _mode(self, db):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT value FROM db_meta WHERE key='embedding_mode'").fetchone()
        conn.close(); return r["value"] if r else "none"

    def _stored_fp(self, db):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT value FROM db_meta WHERE key='embedding_fingerprint'").fetchone()
        conn.close(); return r["value"] if r else None

    def _emb_rows(self, db):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT path, embed_fp, dim FROM embeddings ORDER BY path").fetchall()
        conn.close(); return [dict(r) for r in rows]

    # -- Red 1: cosine's typed dimension/non-finite errors --

    def test_cosine_raises_vector_dimension_mismatch_on_unequal_lengths(self):
        with self.assertRaises(memidx.VectorDimensionMismatch):
            memidx.cosine([1.0, 0.0], [1.0])

    def test_vector_dimension_mismatch_is_a_value_error(self):
        self.assertTrue(issubclass(memidx.VectorDimensionMismatch, ValueError))

    def test_cosine_raises_value_error_on_non_finite_component(self):
        with self.assertRaises(ValueError):
            memidx.cosine([1.0, float("nan")], [1.0, 0.0])

    # -- Red 2: same text, new fingerprint -> reindex re-embeds every row;
    # a second run at the SAME fingerprint re-embeds nothing.

    def test_new_fingerprint_forces_full_reembed_then_settles(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            db = Path(td) / "idx.sqlite"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                reindex(root, db, no_embed=False)
            rows1 = self._emb_rows(db)
            self.assertEqual(len(rows1), 2, rows1)   # topic + its one link row
            self.assertTrue(all(r["embed_fp"] == self.FP1 for r in rows1), rows1)
            self.assertEqual(self._stored_fp(db), self.FP1)

            def _boom(*a, **kw):
                raise AssertionError("compute_embeddings must not run when nothing changed and the fingerprint matches")

            with mock.patch.object(memidx, "compute_embeddings", side_effect=_boom), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                reindex(root, db, no_embed=False)
            self.assertEqual({r["embed_fp"] for r in self._emb_rows(db)}, {self.FP1})

            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP2)):
                reindex(root, db, no_embed=False)
            rows3 = self._emb_rows(db)
            self.assertEqual(len(rows3), 2, rows3)
            self.assertTrue(all(r["embed_fp"] == self.FP2 for r in rows3), rows3)
            self.assertEqual(self._stored_fp(db), self.FP2)

    # -- Red 3: a dimension-mismatched row is skipped, not ranked, and
    # counted; other rows still rank.

    def test_dimension_mismatch_row_is_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td, text="widget cache invalidates on write")
            second = self._topic(td, text="second body about caching widgets", tid="TOP-2", name="second.md")
            db = Path(td) / "idx.sqlite"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                reindex(root, db, no_embed=False)

            path = str(root / "topics" / "t.md")
            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE embeddings SET vector=?, dim=4 WHERE path=?",
                (memidx.pack_vector([0.1, 0.2, 0.3]), path),
            )
            conn.commit(); conn.close()

            def fake_qe(text, model=None):
                return [1.0, 0.0, 0.0, 0.0]

            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            stats = {}
            with mock.patch.object(memidx, "compute_query_embedding", side_effect=fake_qe), \
                 mock.patch.object(memidx, "embedding_fingerprint", return_value=self.FP1):
                ranked = memidx.vector_ranked(conn, "second", memidx.DEFAULT_PROJECT, model=object(), stats=stats)
            conn.close()
            self.assertFalse(any(p == path for p, _ in ranked), ranked)
            self.assertEqual(stats.get("dimension_mismatch_rows"), 1, stats)
            self.assertTrue(any(p == str(root / "topics" / "second.md") for p, _ in ranked), ranked)

    # -- Red 4: short/long batch -> nothing written, stderr names N and M,
    # embedding_mode recompute reads none/partial, never full.

    def test_short_batch_writes_nothing_and_names_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            self._topic(td, text="second body", tid="TOP-2", name="second.md")
            db = Path(td) / "idx.sqlite"

            def short_embed(texts, model=None):
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts[:-1]]

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "compute_embeddings", side_effect=short_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                            full=False, no_embed=False, auto=False))
            self.assertEqual(rc, 0)
            self.assertIn("backend returned 3 vectors for 4 texts", buf_err.getvalue())
            self.assertEqual(len(self._emb_rows(db)), 0)
            self.assertEqual(self._mode(db), "none")

    def test_long_batch_also_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            self._topic(td, text="second body", tid="TOP-2", name="second.md")
            db = Path(td) / "idx.sqlite"

            def long_embed(texts, model=None):
                return [[0.1, 0.2, 0.3, 0.4] for _ in texts] + [[0.9, 0.9, 0.9, 0.9]]

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "compute_embeddings", side_effect=long_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                            full=False, no_embed=False, auto=False))
            self.assertEqual(rc, 0)
            self.assertIn("backend returned 5 vectors for 4 texts", buf_err.getvalue())
            self.assertEqual(len(self._emb_rows(db)), 0)
            self.assertEqual(self._mode(db), "none")

    # -- Red 5: hybrid/vector under a fingerprint mismatch -> FTS list,
    # "embedding": "fingerprint-mismatch", stderr line, exit 0.

    def test_hybrid_and_vector_under_fingerprint_mismatch_fall_back_to_fts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td, text="needle term for search")
            db = Path(td) / "idx.sqlite"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                reindex(root, db, no_embed=False)

            conn = sqlite3.connect(str(db))
            conn.execute(
                "UPDATE db_meta SET value=? WHERE key='embedding_fingerprint'", (self.FOREIGN_FP,)
            )
            conn.commit(); conn.close()

            def _boom(*a, **kw):
                raise AssertionError("compute_query_embedding must not run under a fingerprint mismatch")

            for mode in ("hybrid", "vector"):
                buf_out, buf_err = io.StringIO(), io.StringIO()
                with mock.patch.object(memidx, "compute_query_embedding", side_effect=_boom), \
                     mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                     contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                    rc = memidx.cmd_search(ns(
                        project=memidx.DEFAULT_PROJECT, db=str(db), query="needle term for search",
                        mode=mode, status=[], type=[], area=None, topic=None, authority=None,
                        limit=10, json=True,
                    ))
                self.assertEqual(rc, 0, mode)
                out = json.loads(buf_out.getvalue())
                self.assertEqual(out["embedding"], "fingerprint-mismatch", (mode, out))
                self.assertIn("different model", buf_err.getvalue(), mode)
                self.assertGreater(len(out["results"]), 0, (mode, out))

    # -- Task 4 review finding M1 (carried into Task 5's commit per
    # brief): zero embeddings rows but a FOREIGN stored fingerprint used
    # to be silent (no "embedding" key, no stderr) because the old gate
    # was `has_vectors` alone; the disjunction now catches it too.

    def test_zero_rows_but_foreign_fingerprint_still_reports_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td, text="needle term for search")
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)  # zero embeddings rows

            conn = sqlite3.connect(str(db))
            conn.execute(
                "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('embedding_fingerprint', ?)",
                (self.FOREIGN_FP,),
            )
            conn.commit(); conn.close()

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_search(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), query="needle term for search",
                    mode="vector", status=[], type=[], area=None, topic=None, authority=None,
                    limit=10, json=True,
                ))
            self.assertEqual(rc, 0)
            out = json.loads(buf_out.getvalue())
            self.assertEqual(out["embedding"], "fingerprint-mismatch", out)
            self.assertIn("different model", buf_err.getvalue())

    # -- Red 6/11: an old DB (vectors with NULL embed_fp) invalidates only
    # the vector layer; check --json never reads "mismatch" for it; one
    # embedding reindex refills every row and reads "full".

    def test_old_db_with_null_embed_fp_invalidates_vector_layer_not_fts(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td, text="legacy vector needle phrase")
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)   # rows exist, never embedded

            path = str(root / "topics" / "t.md")
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            sha = conn.execute("SELECT sha256 FROM records WHERE path=?", (path,)).fetchone()["sha256"]
            conn.execute(
                "INSERT INTO embeddings (path, project, dim, embed_sha, embed_fp, vector) VALUES (?,?,?,?,?,?)",
                (path, memidx.DEFAULT_PROJECT, 4, sha, None, memidx.pack_vector([0.1, 0.2, 0.3, 0.4])),
            )
            conn.commit(); conn.close()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_check(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT, json=True))
            report = json.loads(buf.getvalue())
            self.assertIn(report["vector_index_state"], ("none", "partial"), report)

            fts_out = io.StringIO()
            with contextlib.redirect_stdout(fts_out):
                memidx.cmd_search(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), query="legacy vector needle phrase",
                    mode="fts", status=[], type=[], area=None, topic=None, authority=None,
                    limit=10, json=True,
                ))
            fts_results = json.loads(fts_out.getvalue())
            self.assertGreater(len(fts_results), 0, fts_results)

            vec_out, vec_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)), \
                 contextlib.redirect_stdout(vec_out), contextlib.redirect_stderr(vec_err):
                rc = memidx.cmd_search(ns(
                    project=memidx.DEFAULT_PROJECT, db=str(db), query="legacy vector needle phrase",
                    mode="vector", status=[], type=[], area=None, topic=None, authority=None,
                    limit=10, json=True,
                ))
            self.assertEqual(rc, 0)
            vec_json = json.loads(vec_out.getvalue())
            self.assertGreater(len(vec_json["results"]), 0, vec_json)

            # check's own vector_index_state compares against the REAL
            # static fingerprint (it never loads a model) -- so the refill
            # here must carry a fingerprint that shares that static prefix
            # (same model/dim/pipeline/prefix/norm/fastembed version),
            # differing only in revision, to read back as "full" rather
            # than "mismatch" against a fake model name.
            real_static_prefix = memidx._fingerprint_static_prefix(memidx.embedding_fingerprint())
            fp_real_revision = real_static_prefix + "rev-test"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(fp_real_revision)):
                reindex(root, db, no_embed=False)
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2):
                memidx.cmd_check(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT, json=True))
            report2 = json.loads(buf2.getvalue())
            self.assertEqual(report2["vector_index_state"], "full", report2)
            rows = self._emb_rows(db)
            self.assertTrue(all(r["embed_fp"] for r in rows), rows)

    # -- Fix wave 1, G3 (whole-branch-review MODERATE-1): `check --json`'s
    # `searchable_vector_count` must use the SAME "fresh" definition
    # (embed_sha AND embed_fp match) as `vector_index_state` and
    # `embedding_backlog` in the same envelope -- a migrated DB with a
    # NULL-fp row must report 0, not 1.

    def test_searchable_vector_count_matches_vector_index_state_on_a_migrated_db(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td, text="migrated vector needle phrase")
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)   # rows exist, never embedded

            path = str(root / "topics" / "t.md")
            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            sha = conn.execute("SELECT sha256 FROM records WHERE path=?", (path,)).fetchone()["sha256"]
            conn.execute(
                "INSERT INTO embeddings (path, project, dim, embed_sha, embed_fp, vector) VALUES (?,?,?,?,?,?)",
                (path, memidx.DEFAULT_PROJECT, 4, sha, None, memidx.pack_vector([0.1, 0.2, 0.3, 0.4])),
            )
            conn.commit(); conn.close()

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_check(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT, json=True))
            report = json.loads(buf.getvalue())
            self.assertEqual(report["vector_index_state"], "none", report)
            self.assertEqual(report["embedding_backlog"]["rows_without_fresh_vector"], 2, report)
            self.assertEqual(
                report["searchable_vector_count"], 0,
                "a NULL-embed_fp row is not a FRESH vector -- searchable_vector_count must agree "
                "with vector_index_state and embedding_backlog in the same envelope: " + str(report),
            )

    # -- Red 7: check reports "full" on a healthy embedded DB, without
    # importing fastembed (real model -- kept to this one case).

    def test_check_reports_full_vector_index_state_without_importing_fastembed(self):
        import subprocess

        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)   # real model -- one of the few real-model cases here

            script = (
                "import sys, io, contextlib, json; sys.path.insert(0, %r); import memidx\n"
                "buf = io.StringIO()\n"
                "with contextlib.redirect_stdout(buf):\n"
                "    rc = memidx.main(['check', '--root', %r, '--db', %r, '--json'])\n"
                "assert 'fastembed' not in sys.modules, 'fastembed was imported'\n"
                "report = json.loads(buf.getvalue())\n"
                "assert report['vector_index_state'] == 'full', report\n"
                "print('OK')\n"
            ) % (str(TOOLS_DIR), str(root), str(db))
            result = subprocess.run(
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("OK", result.stdout)

    # -- Red 9: --no-embed/--auto on a mismatched DB reports and does not repair.

    def test_no_embed_auto_reports_mismatch_without_repairing(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td)
            db = Path(td) / "idx.sqlite"
            with mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed), \
                 mock.patch.object(memidx, "load_embedding_model", side_effect=self._fake_loader(self.FP1)):
                reindex(root, db, no_embed=False)
            rows_before = self._emb_rows(db)

            conn = sqlite3.connect(str(db))
            conn.execute("UPDATE db_meta SET value=? WHERE key='embedding_fingerprint'", (self.FOREIGN_FP,))
            conn.commit(); conn.close()

            buf_err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_reindex(ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                                            full=False, no_embed=True, auto=True))
            self.assertEqual(rc, 0)
            self.assertIn("different model", buf_err.getvalue())
            self.assertEqual(self._emb_rows(db), rows_before,
                              "a --no-embed/--auto pass must not repair a standing mismatch")


class TestFingerprintsMatchContract(unittest.TestCase):
    """Task 4 review finding L4 (carried into Task 5's commit per brief):
    `fingerprints_match`'s own contract had no direct unit test -- every
    existing test exercised it only indirectly, through a full DB
    mismatch/no-mismatch scenario. These pin the 10 edge cases directly."""

    REAL_A = "model=fake;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=rev1"
    REAL_A_DIFFERENT_REVISION = "model=fake;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=rev2"
    REAL_A_DIFFERENT_DIM = "model=fake;dim=8;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=rev1"
    REAL_A_UNKNOWN_REVISION = "model=fake;dim=4;pipeline=1;prefix=none;norm=l2;fastembed=0.0.0;revision=unknown"

    def test_unknown_on_stored_side_is_a_wildcard(self):
        self.assertTrue(memidx.fingerprints_match(self.REAL_A_UNKNOWN_REVISION, self.REAL_A))

    def test_unknown_on_current_side_is_a_wildcard(self):
        self.assertTrue(memidx.fingerprints_match(self.REAL_A, self.REAL_A_UNKNOWN_REVISION))

    def test_both_unknown_matches(self):
        self.assertTrue(memidx.fingerprints_match(self.REAL_A_UNKNOWN_REVISION, self.REAL_A_UNKNOWN_REVISION))

    def test_both_real_and_identical_matches(self):
        self.assertTrue(memidx.fingerprints_match(self.REAL_A, self.REAL_A))

    def test_both_real_differing_only_in_revision_does_not_match(self):
        self.assertFalse(memidx.fingerprints_match(self.REAL_A, self.REAL_A_DIFFERENT_REVISION))

    def test_differing_in_a_single_static_key_does_not_match(self):
        self.assertFalse(memidx.fingerprints_match(self.REAL_A, self.REAL_A_DIFFERENT_DIM))

    def test_stored_none_never_matches(self):
        self.assertFalse(memidx.fingerprints_match(None, self.REAL_A))

    def test_stored_empty_string_never_matches(self):
        self.assertFalse(memidx.fingerprints_match("", self.REAL_A))

    def test_stored_malformed_never_matches(self):
        self.assertFalse(memidx.fingerprints_match("garbage", self.REAL_A))

    def test_current_none_never_matches(self):
        self.assertFalse(memidx.fingerprints_match(self.REAL_A, None))


class TestTypedDegradation(unittest.TestCase):
    """Design R7 (audit MC-P2-03, TOP-0123 L7): `_degraded`/`_debug_log`,
    `cmd_unmapped`'s programmer-bug branch, `--debug`, per-file/per-record
    savepoints on the decision side, and `cmd_stats`'s typed fail-open
    line."""

    def _store_with_one_topic(self, td):
        root = Path(td) / "root"; (root / "topics").mkdir(parents=True)
        (root / "topics" / "t.md").write_text(
            "---\ntype: topic\nid: TOP-9000\ntitle: T\nlinks:\n"
            "  - link: L1\n    status: active\n    ruling: {text: \"r\", authority: owner-verbatim, source: s}\n"
            "---\nbody\n"
        )
        return root.resolve()

    # -- Red 1: a genuine programmer bug (AttributeError, nothing to do
    # with sqlite) inside cmd_unmapped's per-path classify loop is named,
    # not silently folded into a bare "unknown".

    def test_programmer_error_in_unmapped_is_named_and_debug_logged(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._store_with_one_topic(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            debug_home = Path(td) / "debughome"; debug_home.mkdir()

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "topic_matches_for_path", side_effect=AttributeError("boom")), \
                 mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(debug_home)}), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = memidx.cmd_unmapped(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                             root=str(root), code_root=None, json=True,
                                             paths=["topics/t.md"]))
            out = json.loads(buf_out.getvalue())
            self.assertEqual(out["coverage_status"], "unknown")
            self.assertIn("degraded", out)
            self.assertEqual(out["degraded"]["reason_code"], "internal-error")
            self.assertEqual(out["degraded"]["exception_type"], "AttributeError")
            self.assertIn("boom", out["degraded"]["safe_message"])
            self.assertIn("unmapped: degraded reason=internal-error type=AttributeError",
                           buf_err.getvalue())
            # Ruling 132: _debug_log lands beside the database this call
            # served (db.parent), never under MEMCONTINUUM_HOME -- prove
            # both directions: the file exists at db.parent, and the
            # patched (but now-irrelevant) MEMCONTINUUM_HOME stays empty.
            log_path = db.parent / "memidx-debug.log"
            self.assertTrue(log_path.is_file(), "memidx-debug.log must exist beside the database")
            log_text = log_path.read_text()
            self.assertIn("AttributeError", log_text)
            self.assertIn("boom", log_text)
            self.assertIn("Traceback", log_text)
            self.assertEqual(
                list(debug_home.iterdir()), [],
                "MEMCONTINUUM_HOME must not receive the debug log when a db_path is in scope",
            )

    def test_debug_flag_reraises_unmapped_internal_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._store_with_one_topic(td)
            db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)
            with mock.patch.object(memidx, "topic_matches_for_path", side_effect=AttributeError("boom")), \
                 mock.patch.object(memidx, "DEBUG", True):
                with self.assertRaises(AttributeError):
                    memidx.cmd_unmapped(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                            root=str(root), code_root=None, json=True,
                                            paths=["topics/t.md"]))

    # -- Red 5 (decision side): a DB-level write failure on one record
    # never touches its neighbours, and the run reports it honestly
    # (a hard error, rc != 0) rather than silently mislabeling a DB
    # failure as a parse/shape quarantine -- see report section on this
    # deviation for the reasoning.

    def test_decision_savepoint_isolates_write_failure_to_one_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"; (root / "topics").mkdir(parents=True)
            for name, tid in (("a.md", "TOP-9001"), ("b.md", "TOP-9002"), ("c.md", "TOP-9003")):
                (root / "topics" / name).write_text(
                    "---\ntype: topic\nid: {}\ntitle: T\nlinks:\n"
                    "  - link: L1\n    status: active\n    ruling: {{text: \"r\", authority: owner-verbatim, source: s}}\n"
                    "---\nbody\n".format(tid)
                )
            root = root.resolve()
            db = Path(td) / "idx.sqlite"

            real_insert = memidx.insert_record_rows

            def flaky_insert(conn, project, rec, sha, mtime, size):
                if rec["path"].endswith("b.md"):
                    raise sqlite3.OperationalError("disk I/O error (injected)")
                return real_insert(conn, project, rec, sha, mtime, size)

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with mock.patch.object(memidx, "insert_record_rows", side_effect=flaky_insert), \
                 contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = reindex(root, db, no_embed=True)
            self.assertEqual(rc, 5)
            self.assertIn("integrity failure", buf_out.getvalue())
            self.assertIn("index integrity not guaranteed", buf_err.getvalue())

            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            paths = {r["path"] for r in conn.execute("SELECT path FROM records")}
            b_embeddings = conn.execute(
                "SELECT COUNT(*) AS n FROM embeddings WHERE path LIKE '%b.md'"
            ).fetchone()["n"]
            conn.close()
            self.assertTrue(any(p.endswith("a.md") for p in paths), paths)
            self.assertTrue(any(p.endswith("c.md") for p in paths), paths)
            self.assertFalse(any(p.endswith("b.md") for p in paths), paths)
            self.assertEqual(b_embeddings, 0, "b.md's embedding upsert must have rolled back too")

    # -- Regression (advisor review, post-fix): the per-record SAVEPOINT
    # loop must nest inside ONE transaction that commits only once, at the
    # very end of the function -- not one micro-commit per SAVEPOINT/
    # RELEASE pair. Proven black-box (no connection tracing needed): force
    # the mode-recompute step that runs AFTER the loop but BEFORE the
    # final conn.commit() to raise, and confirm NOTHING landed. Before the
    # `if not conn.in_transaction: conn.execute("BEGIN")` guard this read
    # 3 (each RELEASE had already committed its own record); it must read
    # 0 now that the whole loop shares one transaction closed only by the
    # function's own final commit.

    def test_decision_single_commit_nothing_lands_if_post_loop_step_fails(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"; (root / "topics").mkdir(parents=True)
            for name, tid in (("a.md", "TOP-9001"), ("b.md", "TOP-9002"), ("c.md", "TOP-9003")):
                (root / "topics" / name).write_text(
                    "---\ntype: topic\nid: {}\ntitle: T\nlinks:\n"
                    "  - link: L1\n    status: active\n    ruling: {{text: \"r\", authority: owner-verbatim, source: s}}\n"
                    "---\nbody\n".format(tid)
                )
            root = root.resolve()
            db = Path(td) / "idx.sqlite"

            with mock.patch.object(memidx, "_fingerprint_static_prefix", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    reindex(root, db, no_embed=True)

            conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
            n = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
            conn.close()
            self.assertEqual(
                n, 0,
                "a failure AFTER the per-record loop but before the final commit must roll back "
                "every record the loop touched -- proves the SAVEPOINTs share one transaction",
            )

    # -- Red 7 (cmd_stats): the fail-open catch-all is retyped.

    def test_stats_internal_error_prints_typed_degraded_line(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"; home.mkdir()
            (home / "hook.log").write_text("2026-09-04T00:00:00+00:00 outcome=ok project=p\n")
            buf_out = io.StringIO()
            with mock.patch.object(memidx, "_stats_report", side_effect=AttributeError("boom")), \
                 contextlib.redirect_stdout(buf_out):
                rc = memidx.cmd_stats(ns(project=memidx.DEFAULT_PROJECT, days=7, home=str(home),
                                          store=None, json=False, now=None))
            self.assertEqual(rc, 0)
            self.assertIn("stats: degraded reason=internal-error type=AttributeError: boom -- exit 0 (fail-open)",
                           buf_out.getvalue())

    def test_debug_flag_reraises_stats_internal_error(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"; home.mkdir()
            (home / "hook.log").write_text("2026-09-04T00:00:00+00:00 outcome=ok project=p\n")
            with mock.patch.object(memidx, "_stats_report", side_effect=AttributeError("boom")), \
                 mock.patch.object(memidx, "DEBUG", True):
                with self.assertRaises(AttributeError):
                    memidx.cmd_stats(ns(project=memidx.DEFAULT_PROJECT, days=7, home=str(home),
                                         store=None, json=False, now=None))

    # -- Direct unit coverage of the new helpers themselves.

    def test_degraded_helper_shape_and_truncation(self):
        exc = ValueError("x" * 300 + "\nsecond line")
        d = memidx._degraded("internal-error", exc)
        self.assertEqual(d["reason_code"], "internal-error")
        self.assertEqual(d["exception_type"], "ValueError")
        self.assertLessEqual(len(d["safe_message"]), 200)
        self.assertNotIn("\n", d["safe_message"])

    def test_degraded_helper_no_exception(self):
        d = memidx._degraded("index-missing")
        self.assertEqual(d, {"reason_code": "index-missing", "exception_type": None, "safe_message": None})

    def test_debug_log_writes_traceback_when_home_exists(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"; home.mkdir()
            try:
                raise RuntimeError("kaboom")
            except RuntimeError as exc:
                with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(home)}):
                    memidx._debug_log(exc, "unit-test")
            text = (home / "memidx-debug.log").read_text()
            self.assertIn("kaboom", text)
            self.assertIn("unit-test", text)
            self.assertIn("Traceback", text)

    def test_debug_log_never_creates_the_home_directory(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "does-not-exist"
            try:
                raise RuntimeError("kaboom")
            except RuntimeError as exc:
                with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(home)}):
                    memidx._debug_log(exc, "unit-test")  # must not raise, must not create home
            self.assertFalse(home.exists())

    def test_debug_log_writes_beside_db_path_ignoring_home(self):
        """Ruling 132 (coordinator, TOP-0123 L6 review): `_debug_log` must
        place its file BESIDE THE DATABASE it serves (`Path(db_path).parent`)
        when a caller has one -- never resolved from a default home,
        independently of whatever `--db` the caller was actually given.
        Proven here with MEMCONTINUUM_HOME UNSET and `Path.home()` patched
        to a temp dir that must stay completely untouched."""
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "fake-home"
            fake_home.mkdir()
            db_dir = Path(td) / "custom-db-dir"
            db_dir.mkdir()
            db_path = db_dir / "project.sqlite"
            try:
                raise RuntimeError("kaboom")
            except RuntimeError as exc:
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("MEMCONTINUUM_HOME", None)
                    with mock.patch.object(memidx.Path, "home", return_value=fake_home):
                        memidx._debug_log(exc, "unit-test", db_path)
            log_path = db_dir / "memidx-debug.log"
            self.assertTrue(log_path.is_file(), "the log must land beside the db path")
            text = log_path.read_text()
            self.assertIn("kaboom", text)
            self.assertIn("unit-test", text)
            self.assertFalse((fake_home / "memidx-debug.log").exists())
            self.assertEqual(list(fake_home.iterdir()), [], "nothing may land in the patched home")

    def test_index_integrity_error_carries_rel_and_cause(self):
        cause = sqlite3.OperationalError("disk I/O error")
        err = memidx.IndexIntegrityError("src/x.py", cause)
        self.assertEqual(err.rel, "src/x.py")
        self.assertIs(err.cause, cause)
        self.assertIn("src/x.py", str(err))
        self.assertIn("OperationalError", str(err))


class TestEmbedWorker(unittest.TestCase):
    """Design R8 (audit MC-P2-02, TOP-0123 L7): the coalescing background
    embed-worker -- `cmd_embed_worker`, the marker/lock file contract, and
    `embedding_backlog`. Every test sets a temp MEMCONTINUUM_HOME via
    mock.patch.dict(os.environ, ...) (never touches the real one) and
    never leaves a worker holding its lock (tearDown asserts the lock is
    free)."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-embedworker-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.home = Path(self.td) / "home"
        self.home.mkdir()
        self.project = "ew-test"

    def tearDown(self):
        # Ruling 132: marker/lock/log land beside the database
        # (db.parent), never under the home the test happens to patch --
        # this must track cmd_embed_worker's own resolution or a held
        # lock at the WRONG path would silently never be detected.
        lock_path = self._db().parent / f"{self.project}.embed.lock"
        if lock_path.exists():
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                self.fail(f"{lock_path} is still held after the test")
            finally:
                os.close(fd)

    def _topic(self, text="the widget cache invalidates on write"):
        root = Path(self.td) / "root"
        (root / "topics").mkdir(parents=True, exist_ok=True)
        p = root / "topics" / "t.md"
        p.write_text(f"---\nid: T-1\ntitle: T\nstatus: active\n---\n{text}\n")
        return root.resolve()

    def _db(self):
        return Path(self.td) / f"{self.project}.sqlite"

    def _marker(self):
        return self._db().parent / f"{self.project}.embed-pending"

    def _log(self):
        return self._db().parent / f"{self.project}.embed.log"

    def _args(self, root, db):
        return SimpleNamespace(root=str(root), project=self.project, db=str(db))

    @staticmethod
    def _fake_embed(texts, model=None):
        return [[0.01] * memidx.EMBED_DIM for _ in texts]

    def _mode(self, db):
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT value FROM db_meta WHERE key='embedding_mode'").fetchone()
        conn.close()
        return r["value"] if r else "none"

    # -- 3: worker embeds everything and clears the marker ------------------

    def test_worker_embeds_everything_and_clears_marker(self):
        root = self._topic()
        db = self._db()
        reindex(root, db, project=self.project, no_embed=True)  # content only, no vector yet
        marker = self._marker()
        marker.touch()
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}), \
             mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed):
            rc = memidx.cmd_embed_worker(self._args(root, db))
        self.assertEqual(rc, 0)
        self.assertFalse(marker.exists(), "a clean pass must remove the marker")
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT vector FROM embeddings WHERE path=?", (str(root / "topics" / "t.md"),)).fetchone()
        conn.close()
        self.assertIsNotNone(row, "every record must be embedded")
        self.assertEqual(self._mode(db), "full")
        # Ruling 132: marker/lock/log land beside the database, never
        # under the patched MEMCONTINUUM_HOME -- prove the OLD location
        # stays empty, not merely that the new one has the right files.
        self.assertEqual(
            list(self.home.iterdir()), [],
            "embed-worker must not write any companion file under home",
        )

    # -- 3: two commits during one pass coalesce into one job ---------------

    def test_worker_coalesces_a_marker_retouched_mid_pass(self):
        root = self._topic()
        db = self._db()
        reindex(root, db, project=self.project, no_embed=True)
        marker = self._marker()
        marker.touch()

        calls = []

        def fake_backfill(args):
            calls.append(args)
            if len(calls) == 1:
                # Simulate a second commit landing WHILE this pass runs:
                # retouch the marker mid-way (a later mtime_ns than the
                # worker captured before this call).
                time.sleep(0.01)
                marker.touch()
            return 0

        with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}), \
             mock.patch.object(memidx, "cmd_reindex", side_effect=fake_backfill):
            rc = memidx.cmd_embed_worker(self._args(root, db))
        self.assertEqual(rc, 0)
        self.assertFalse(marker.exists(), "the loop must run again and clear the marker on the untouched pass")
        self.assertGreaterEqual(len(calls), 2, "a mid-pass retouch must make the loop run at least twice")

    # -- 4: a crash leaves a retriable marker --------------------------------

    def test_worker_crash_leaves_marker_and_logs_exception(self):
        root = self._topic()
        db = self._db()
        reindex(root, db, project=self.project, no_embed=True)
        marker = self._marker()
        marker.touch()

        with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}), \
             mock.patch.object(memidx, "cmd_reindex", side_effect=RuntimeError("kaboom")):
            rc = memidx.cmd_embed_worker(self._args(root, db))
        self.assertEqual(rc, 3)
        self.assertTrue(marker.exists(), "a crash must leave the marker for a later retry")
        log_text = self._log().read_text()
        self.assertIn("kaboom", log_text)
        self.assertIn("RuntimeError", log_text)
        self.assertIn("Traceback", log_text)

        # A second, healthy run clears it.
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}), \
             mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed):
            rc2 = memidx.cmd_embed_worker(self._args(root, db))
        self.assertEqual(rc2, 0)
        self.assertFalse(marker.exists(), "a subsequent healthy run must clear the retriable marker")

    # -- 5: a second worker exits 0 immediately while the first holds the lock

    def test_second_worker_exits_0_while_first_holds_lock(self):
        root = self._topic()
        db = self._db()
        reindex(root, db, project=self.project, no_embed=True)
        marker = self._marker()
        marker.touch()
        lock_path = self._db().parent / f"{self.project}.embed.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}), \
                 mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed):
                rc = memidx.cmd_embed_worker(self._args(root, db))
            self.assertEqual(rc, 0, "the second worker must exit 0 immediately, not block or error")
            self.assertTrue(marker.exists(), "the SECOND worker (locked out) must not touch the marker's state")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        # Now that the first "worker" released, drain the marker for real
        # so tearDown's lock-free assertion has nothing outstanding.
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}), \
             mock.patch.object(memidx, "compute_embeddings", side_effect=self._fake_embed):
            memidx.cmd_embed_worker(self._args(root, db))

    # -- 6: --help exits 0 and is id-free ------------------------------------

    def test_embed_worker_help_exits_0_and_is_id_free(self):
        env = dict(os.environ); env["PYTHONPATH"] = ""
        proc = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "memidx.py"), "embed-worker", "--help"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())
        for token in ("TOP-0123", "MC-P2-02", "R8"):
            self.assertNotIn(token, proc.stdout)

    # -- 6: check --json reports embedding_backlog ---------------------------

    def test_check_json_reports_embedding_backlog(self):
        root = self._topic()
        db = self._db()
        reindex(root, db, project=self.project, no_embed=True)
        marker = self._marker()
        marker.touch()
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_check(ns(project=self.project, db=str(db), root=str(root), json=True))
        out = json.loads(buf.getvalue())
        self.assertIn("embedding_backlog", out)
        backlog = out["embedding_backlog"]
        self.assertTrue(backlog["pending_marker"])
        self.assertEqual(backlog["rows_without_fresh_vector"], 1)
        self.assertFalse(backlog["worker_lock_held"])
        marker.unlink()

    # -- MOD-1 (task-6-review.md): a fail-open embedding-backend failure
    # (not a crash -- cmd_reindex catches it internally and returns 0 by
    # R4/R7's own fail-open contract) must still leave the marker and
    # signal failure to the worker's own caller, via an explicit
    # module-level signal cmd_reindex sets (never stdout-parsed, never the
    # awaiting-embedding count -- a legitimately un-embeddable row also
    # leaves that count > 0 and must not make the worker spin on it) ------

    def test_broken_backend_leaves_the_marker_and_exits_3(self):
        root = self._topic()
        db = self._db()
        reindex(root, db, project=self.project, no_embed=True)
        marker = self._marker()
        marker.touch()
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_HOME": str(self.home)}), \
             mock.patch.object(memidx, "load_embedding_model",
                                side_effect=RuntimeError("fastembed not installed")):
            rc = memidx.cmd_embed_worker(self._args(root, db))
        self.assertEqual(rc, 3, "a fail-open embedding-backend failure must signal failure, not success")
        self.assertTrue(marker.exists(), "a broken backend must leave the marker for a later retry")

    # -- LOW-1 (task-6-review.md): worker_lock_held reads True while a
    # worker holds the lock (previously untested; the "held" branch was
    # correct but never exercised) -----------------------------------------

    def test_worker_lock_held_true_while_a_worker_holds_the_lock(self):
        root = self._topic()
        db = self._db()
        reindex(root, db, project=self.project, no_embed=True)
        lock_path = self._db().parent / f"{self.project}.embed.lock"
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            backlog = memidx.embedding_backlog(db, self.project)
            self.assertTrue(backlog["worker_lock_held"])
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        backlog2 = memidx.embedding_backlog(db, self.project)
        self.assertFalse(backlog2["worker_lock_held"])


if __name__ == "__main__":
    unittest.main()
