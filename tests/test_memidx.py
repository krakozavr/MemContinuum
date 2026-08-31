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
    def test_project_a_query_never_returns_project_b(self):
        with tempfile.TemporaryDirectory() as td:
            root_a = Path(td) / "a"
            root_b = Path(td) / "b"
            build_real_corpus(root_a)
            build_real_corpus(root_b)
            db = Path(td) / "shared.sqlite"  # same db file, different --project

            reindex(root_a, db, project="proj-a", no_embed=True)
            reindex(root_b, db, project="proj-b", no_embed=True)

            results = _run_search(ns(
                db=str(db), project="proj-a", query="hidden files count", mode="fts",
                status=[], type=[], area=None, topic=None, authority=None, limit=50, json=True,
            ))
            self.assertTrue(results)
            for r in results:
                self.assertTrue(r["path"].startswith(str(root_a)), r["path"])
                self.assertFalse(r["path"].startswith(str(root_b)), r["path"])


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
