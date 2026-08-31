import os
import shutil
import sys
import tempfile
import time
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402

FIXTURES = TOOLS_DIR / "fixtures"
# The 14 incident records are local copies (fixtures/records/incidents/) of the
# real-world notes originally sourced from an external sandbox directory --
# copied in once, verbatim, never edited, so the test tree is self-contained.
LOCAL_INCIDENTS = FIXTURES / "records" / "incidents"
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


def _run_search(args):
    # cmd_search prints; pull the underlying data via the same code path it uses.
    conn = memidx.open_db(memidx.resolve_db_path(args))
    allowed = memidx.filtered_paths(conn, args)
    if args.mode == "fts":
        ranked = memidx.fts_ranked(conn, args.query, args.project)
        results = [(p, float(len(ranked) - i)) for i, p in enumerate(ranked) if p in allowed]
    elif args.mode == "vector":
        ranked = memidx.vector_ranked(conn, args.query, args.project)
        results = [(p, s) for p, s in ranked if p in allowed]
    else:
        fts_list = [p for p in memidx.fts_ranked(conn, args.query, args.project) if p in allowed]
        vec_list = [p for p, _ in memidx.vector_ranked(conn, args.query, args.project) if p in allowed]
        k = 60
        scores = {}
        for i, p in enumerate(fts_list):
            scores[p] = scores.get(p, 0.0) + 1.0 / (k + i + 1)
        for i, p in enumerate(vec_list):
            scores[p] = scores.get(p, 0.0) + 1.0 / (k + i + 1)
        results = sorted(scores.items(), key=lambda t: t[1], reverse=True)
    results = results[: args.limit]
    out = []
    for path, score in results:
        row = memidx.record_row_by_path(conn, path)
        if row is None:
            continue
        out.append({"path": row["path"], "score": score})
    conn.close()
    return out


class TestD1RebuildStable(unittest.TestCase):
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
            self.assertEqual(self._paths(conn), {str(root / "topics" / "area" / "real.md"), str(noise)})
            conn.close()

            reindex(root, db, no_embed=True)

            conn = memidx.open_db(db, project=memidx.DEFAULT_PROJECT)
            self.assertEqual(self._paths(conn), {str(root / "topics" / "area" / "real.md")})
            conn.close()
            # The file on disk is never touched -- only the derived index.
            self.assertTrue(noise.is_file())

    @staticmethod
    def _paths(conn):
        return {r[0] for r in conn.execute(
            "SELECT path FROM records WHERE project=?", (memidx.DEFAULT_PROJECT,))}


class TestCheckDrift(unittest.TestCase):
    def test_check_detects_touched_file(self):
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
            import os
            os.utime(target, (new_time, new_time))

            rc_dirty = memidx.cmd_check(args)
            self.assertEqual(rc_dirty, 1)


if __name__ == "__main__":
    unittest.main()
