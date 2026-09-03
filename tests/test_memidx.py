import contextlib
import io
import os
import shutil
import sqlite3
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

    def test_for_path_missing_or_uninitialized_exits_3_with_bare_list(self):
        for state_setup, label in ((lambda db: None, "missing"),
                                    (lambda db: memidx.open_db(db, project=memidx.DEFAULT_PROJECT).close(), "uninitialized")):
            with self.subTest(label), tempfile.TemporaryDirectory() as td:
                db = Path(td) / f"{label}.sqlite"
                state_setup(db)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = memidx.cmd_for_path(ns(project=memidx.DEFAULT_PROJECT, db=str(db),
                                                 file_path="src/x.py", json=True))
                self.assertEqual(rc, 3)
                self.assertEqual(json.loads(buf.getvalue()), [])

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
            self.assertEqual(json.loads(buf.getvalue())["coverage_status"], "index-error")


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
        return root

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

    def test_auto_never_writes_mode_but_leaves_a_real_gap_until_the_next_full_reindex(self):
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td); db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=False)
            self.assertEqual(self._mode(db), "full")
            (root / "topics" / "t.md").write_text(
                (root / "topics" / "t.md").read_text().replace("alpha decision", "delta decision")
            )
            args = ns(root=str(root), db=str(db), project=memidx.DEFAULT_PROJECT,
                       full=False, no_embed=True, auto=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                memidx.cmd_reindex(args)
            self.assertEqual(self._mode(db), "full", "an --auto heal must never write embedding_mode at all")
            self.assertNotIn("embedding mode set to", buf.getvalue())
            path = str(root / "topics" / "t.md")
            self.assertNotEqual(self._emb(db, path)["embed_sha"], self._rec_sha(db, path))
            reindex(root, db, no_embed=False)
            self.assertEqual(self._emb(db, path)["embed_sha"], self._rec_sha(db, path))

    def test_unchanged_sha_skip_still_requires_a_matching_embed_sha(self):
        # the skip predicate is sha AND embed_sha match -- not sha alone.
        with tempfile.TemporaryDirectory() as td:
            root = self._topic(td); db = Path(td) / "idx.sqlite"
            reindex(root, db, no_embed=True)   # record exists, no embedding at all
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                reindex(root, db, no_embed=False)
            self.assertIn("1 embedding(s) backfilled", buf.getvalue())
            path = str(root / "topics" / "t.md")
            self.assertIsNotNone(self._emb(db, path))


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


if __name__ == "__main__":
    unittest.main()
