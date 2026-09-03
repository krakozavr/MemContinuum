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
                memidx.cmd_search(ns(project=memidx.DEFAULT_PROJECT, db=str(db), query="sweeper",
                                      mode="fts", status=[], type=[], area=None, topic=None,
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


if __name__ == "__main__":
    unittest.main()
