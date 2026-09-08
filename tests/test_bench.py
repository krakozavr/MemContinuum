"""tests/test_bench.py -- verifies the public retrieval benchmark harness
under bench/: the corpus lints, every query's expected ids are real
records, the scorer's metrics match a hand-checked fixture, every runner
obeys bench/README.md's interface contract, the path-kind answer key
matches an independent recomputation from the corpus's own frontmatter,
and the paraphrase queries genuinely share no vocabulary with their
targets.

Never touches ~/.memcontinuum, ~/.claude, memory/, fixtures/records, or
.claude/: every index this file builds lives under a TemporaryDirectory,
and bench/runners/memcontinuum.py's own default --db already lives under
the system temp directory, never $MEMCONTINUUM_HOME.
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402

BENCH_DIR = TOOLS_DIR / "bench"
CORPUS = BENCH_DIR / "corpus"
CODEBASE = BENCH_DIR / "codebase"
QUERIES_PATH = BENCH_DIR / "queries.jsonl"
RUNNERS_DIR = BENCH_DIR / "runners"

VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
_SKIP_NO_VENV = (
    "set $MEMCONTINUUM_PYTHON to a venv python with fastembed/PyYAML installed "
    "to run the memcontinuum-runner-specific checks (see bench/README.md)"
)

# Load bench/score.py by file path -- bench/ is not a package (no
# __init__.py, deliberately: everything under bench/ is meant to be read
# and run as loose scripts, matching the runner-interface contract), so an
# ordinary `import` would not find it.
import importlib.util

_spec = importlib.util.spec_from_file_location("bench_score", str(BENCH_DIR / "score.py"))
score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score)


def load_queries() -> list[dict]:
    return score.load_queries(QUERIES_PATH)


# ---------------------------------------------------------------------------
# 1. the corpus lints
# ---------------------------------------------------------------------------

class TestCorpusLint(unittest.TestCase):
    def test_bare_lint_clean(self):
        errors, warnings = memlint.lint_root(CORPUS)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [], f"expected zero warnings, got: {warnings}")

    def test_lint_clean_with_code_root_bound(self):
        errors, warnings = memlint.lint_root(CORPUS, code_roots=[CODEBASE])
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [], f"expected zero warnings, got: {warnings}")

    def test_corpus_size_in_brief_s_25_to_40_range(self):
        n = sum(1 for _ in memidx.walk_markdown(CORPUS))
        self.assertGreaterEqual(n, 25)
        self.assertLessEqual(n, 40)


# ---------------------------------------------------------------------------
# 2. every query's expected ids exist in the corpus
# ---------------------------------------------------------------------------

def _all_corpus_ids() -> set[str]:
    ids = set()
    for f in memidx.walk_markdown(CORPUS):
        result = memidx.parse_record(f)
        if not result.valid:
            continue
        fm = result.frontmatter
        ids.add(str(fm.get("id") or f.stem))
    return ids


class TestQueriesShapeAndExpectIds(unittest.TestCase):
    def test_at_least_30_queries_and_10_paraphrase(self):
        queries = load_queries()
        self.assertGreaterEqual(len(queries), 30)
        para = [q for q in queries if q["id"].startswith("para-")]
        self.assertGreaterEqual(len(para), 10)

    def test_both_kinds_present(self):
        kinds = {q["kind"] for q in load_queries()}
        self.assertEqual(kinds, {"path", "question"})

    def test_ids_are_unique(self):
        queries = load_queries()
        ids = [q["id"] for q in queries]
        self.assertEqual(len(ids), len(set(ids)), "duplicate query id(s)")

    def test_every_expect_id_exists_in_corpus(self):
        corpus_ids = _all_corpus_ids()
        queries = load_queries()
        for q in queries:
            for rid in q["expect"]:
                self.assertIn(
                    rid, corpus_ids,
                    f"query {q['id']!r} expects {rid!r}, which is not a record id in {CORPUS}",
                )

    def test_notes_nonempty(self):
        for q in load_queries():
            self.assertTrue(q["notes"].strip(), f"query {q['id']!r} has an empty notes field")


# ---------------------------------------------------------------------------
# 3. the scorer's metrics, hand-checked
# ---------------------------------------------------------------------------

class TestScorerMetrics(unittest.TestCase):
    def test_single_expect_perfect_rank1(self):
        m = score.evaluate_query(["A"], ["A", "B", "C"])
        self.assertEqual(m["recall@1"], 1.0)
        self.assertEqual(m["recall@3"], 1.0)
        self.assertEqual(m["recall@10"], 1.0)
        self.assertEqual(m["mrr"], 1.0)

    def test_single_expect_found_at_rank_3(self):
        m = score.evaluate_query(["C"], ["A", "B", "C"])
        self.assertEqual(m["recall@1"], 0.0)
        self.assertEqual(m["recall@3"], 1.0)
        self.assertEqual(m["recall@10"], 1.0)
        self.assertAlmostEqual(m["mrr"], 1.0 / 3.0)

    def test_not_found_at_all(self):
        m = score.evaluate_query(["Z"], ["A", "B", "C"])
        self.assertEqual(m["recall@1"], 0.0)
        self.assertEqual(m["recall@3"], 0.0)
        self.assertEqual(m["recall@10"], 0.0)
        self.assertEqual(m["mrr"], 0.0)

    def test_empty_ranked_list(self):
        m = score.evaluate_query(["A"], [])
        self.assertEqual(m["recall@1"], 0.0)
        self.assertEqual(m["recall@10"], 0.0)
        self.assertEqual(m["mrr"], 0.0)

    def test_multi_expect_partial_recall_at_1(self):
        # Two correct ids, only one can occupy rank 1 -- this is the exact
        # shape several path-kind queries take (see bench/README.md's
        # Recall@k note); hand-computed: top-1 = {"B"}, expect = {"A","B"},
        # intersection size 1 of 2 expected -> recall@1 = 0.5.
        m = score.evaluate_query(["A", "B"], ["B", "X", "A"])
        self.assertAlmostEqual(m["recall@1"], 0.5)
        self.assertAlmostEqual(m["recall@3"], 1.0)  # both A and B are in the top 3
        self.assertEqual(m["mrr"], 1.0)  # B (an expected id) is at rank 1

    def test_multi_expect_neither_found(self):
        m = score.evaluate_query(["A", "B"], ["X", "Y", "Z"])
        self.assertEqual(m["recall@1"], 0.0)
        self.assertEqual(m["recall@3"], 0.0)
        self.assertEqual(m["mrr"], 0.0)

    def test_expect_must_be_nonempty(self):
        with self.assertRaises(ValueError):
            score.evaluate_query([], ["A"])

    def test_aggregate_averages_across_queries(self):
        # Query 1: perfect rank-1 hit (recall@1=1.0, mrr=1.0).
        # Query 2: found only at rank 3 (recall@1=0.0, mrr=1/3).
        m1 = score.evaluate_query(["A"], ["A", "B", "C"])
        m2 = score.evaluate_query(["C"], ["A", "B", "C"])
        agg = score.aggregate([m1, m2])
        self.assertEqual(agg["n"], 2)
        self.assertAlmostEqual(agg["recall@1"], 0.5)  # (1.0 + 0.0) / 2
        self.assertAlmostEqual(agg["recall@3"], 1.0)  # (1.0 + 1.0) / 2
        self.assertAlmostEqual(agg["mrr"], (1.0 + 1.0 / 3.0) / 2)

    def test_aggregate_of_zero_queries_is_zero_not_a_crash(self):
        agg = score.aggregate([])
        self.assertEqual(agg["n"], 0)
        self.assertEqual(agg["recall@1"], 0.0)
        self.assertEqual(agg["mrr"], 0.0)


# ---------------------------------------------------------------------------
# 4. every runner obeys the interface
# ---------------------------------------------------------------------------

def _run_runner(script: Path, kind: str, query: str, mode: str | None = None, corpus: Path = CORPUS) -> subprocess.CompletedProcess:
    argv = [sys.executable, str(script), "--corpus", str(corpus), "--kind", kind, "--query", query, "--limit", "5"]
    if mode:
        argv += ["--mode", mode]
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)


class TestRunnerInterface(unittest.TestCase):
    def test_nomemory_always_empty_rc0(self):
        for kind, query in (("path", "src/storage/dedup/store.py"), ("question", "why?")):
            proc = _run_runner(RUNNERS_DIR / "nomemory.py", kind, query)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), "")

    def test_keyword_rc0_one_id_per_line(self):
        for kind, query in (
            ("path", "src/storage/dedup/store.py"),
            ("question", "why does the dedup store key chunks by content hash"),
        ):
            proc = _run_runner(RUNNERS_DIR / "keyword.py", kind, query)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [ln for ln in proc.stdout.splitlines()]
            self.assertTrue(all(ln.strip() == ln and ln for ln in lines), lines)
            self.assertEqual(len(lines), len(set(lines)), "keyword runner must not repeat an id")

    def test_keyword_empty_query_is_empty_not_a_crash(self):
        proc = _run_runner(RUNNERS_DIR / "keyword.py", "question", "   ")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_keyword_respects_limit(self):
        argv = [sys.executable, str(RUNNERS_DIR / "keyword.py"), "--corpus", str(CORPUS),
                "--kind", "question", "--query", "the a of and to", "--limit", "2"]
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertLessEqual(len(lines), 2)

    def test_bad_corpus_dir_is_a_clean_nonzero_exit_not_a_traceback(self):
        for script in ("nomemory.py", "keyword.py"):
            proc = _run_runner(RUNNERS_DIR / script, "path", "x.py", corpus=Path("/no/such/dir"))
            if script == "nomemory.py":
                continue  # nomemory never reads --corpus at all
            self.assertNotEqual(proc.returncode, 0)
            self.assertNotIn("Traceback", proc.stderr)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_memcontinuum_rc0_one_id_per_line_both_kinds(self):
        env_python = {"MEMCONTINUUM_PYTHON": VENV_PYTHON}
        for kind, query, mode in (
            ("path", "src/storage/dedup/store.py", None),
            ("question", "why does the dedup store key chunks by content hash", "fts"),
        ):
            argv = [sys.executable, str(RUNNERS_DIR / "memcontinuum.py"), "--corpus", str(CORPUS),
                    "--kind", kind, "--query", query, "--limit", "5"]
            if mode:
                argv += ["--mode", mode]
            env = dict(os.environ)
            env["PYTHONPATH"] = ""
            env.update(env_python)
            proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [ln for ln in proc.stdout.splitlines()]
            self.assertTrue(all(ln.strip() == ln and ln for ln in lines), lines)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_memcontinuum_path_query_matches_hand_authored_key(self):
        """One live end-to-end check (not just the oracle below): store.py
        must come back exactly as bench/queries.jsonl's path-01 says."""
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        env["MEMCONTINUUM_PYTHON"] = VENV_PYTHON
        argv = [sys.executable, str(RUNNERS_DIR / "memcontinuum.py"), "--corpus", str(CORPUS),
                "--kind", "path", "--query", "src/storage/dedup/store.py", "--limit", "10"]
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ids = set(ln.strip() for ln in proc.stdout.splitlines() if ln.strip())
        self.assertEqual(ids, {"TOP-101", "TOP-104", "CON-301"})

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_memcontinuum_reports_bad_index_as_error_not_empty_results(self):
        """A --db pointed at a file that can never become a valid
        MemContinuum index must fail loudly, not be mistaken for a clean
        'no results' answer (bench/README.md's Runner interface contract)."""
        with tempfile.TemporaryDirectory() as td:
            bad_db = Path(td) / "not-a-db" / "sub" / "x.sqlite"
            # A directory in place of the db file: reindex cannot open it as
            # a database no matter what, which is exactly the "genuinely
            # broken index" case the runner must surface as an error.
            bad_db.parent.mkdir(parents=True)
            bad_db.mkdir()
            argv = [sys.executable, str(RUNNERS_DIR / "memcontinuum.py"), "--corpus", str(CORPUS),
                    "--kind", "path", "--query", "src/storage/dedup/store.py",
                    "--db", str(bad_db)]
            env = dict(os.environ)
            env["PYTHONPATH"] = ""
            env["MEMCONTINUUM_PYTHON"] = VENV_PYTHON
            proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
            self.assertNotEqual(proc.returncode, 0)


# ---------------------------------------------------------------------------
# 5. the path-kind answer key, independently recomputed from the corpus's
#    own frontmatter -- never calls memidx.py, never opens a database.
# ---------------------------------------------------------------------------

def _load_corpus_frontmatter() -> tuple[dict[str, dict], dict[str, dict]]:
    """(topics: id -> frontmatter, concepts: id -> frontmatter)."""
    topics: dict[str, dict] = {}
    concepts: dict[str, dict] = {}
    for f in memidx.walk_markdown(CORPUS):
        result = memidx.parse_record(f)
        if not result.valid:
            continue
        fm = result.frontmatter
        rid = str(fm.get("id") or f.stem)
        if fm.get("type") == "concept":
            concepts[rid] = fm
        elif fm.get("links") or fm.get("type") == "topic":
            topics[rid] = fm
    return topics, concepts


def _oracle_expect_for_path(file_path: str, topics: dict, concepts: dict) -> set[str]:
    """Independent reimplementation of bench/README.md's "path-kind answer
    key" rule, using memidx.code_ref_matches (the one shared matching
    primitive for-path itself, drift, and memlint's own marker checks all
    trust) but none of memidx's database/indexing machinery -- this reads
    frontmatter dicts directly."""
    direct = [
        tid for tid, fm in topics.items()
        if any(memidx.code_ref_matches(file_path, ref) for ref in (fm.get("code_refs") or []))
    ]
    matched_concepts = [
        cid for cid, fm in concepts.items()
        if any(
            memidx.code_ref_matches(file_path, ref)
            for ref in list(fm.get("implemented_by") or []) + list(fm.get("tested_by") or [])
        )
    ]
    flat: set[str] = set(direct)
    for cid in matched_concepts:
        flat.add(cid)
        for gtid in concepts[cid].get("governed_by") or []:
            flat.add(str(gtid))
    return flat


class TestPathOracle(unittest.TestCase):
    def test_every_path_query_matches_independent_recomputation(self):
        topics, concepts = _load_corpus_frontmatter()
        self.assertTrue(topics, "no topics found in corpus -- fixture loading is broken")
        self.assertTrue(concepts, "no concepts found in corpus -- fixture loading is broken")
        for q in load_queries():
            if q["kind"] != "path":
                continue
            oracle = _oracle_expect_for_path(q["query"], topics, concepts)
            self.assertEqual(
                oracle, set(q["expect"]),
                f"{q['id']} ({q['query']!r}): authored expect {sorted(q['expect'])} != "
                f"oracle {sorted(oracle)} recomputed from the corpus's own code_refs/"
                f"implemented_by/tested_by/governed_by",
            )


# ---------------------------------------------------------------------------
# 6. paraphrase queries genuinely share no vocabulary with their target
# ---------------------------------------------------------------------------

# INC-0115: this stopword list/tokenizer used to be defined here only; it
# now also backs memidx._fts_top_hit_is_confident's own content-term
# coverage gate (hybrid's FTS-step-aside threshold), so it lives in
# memidx.py and is imported, not duplicated, here.
_tokens = memidx._content_terms


def _indexed_text_for(record_id: str, topics: dict, incidents: dict) -> str:
    """title + body + (for a topic) its CURRENT active link's ruling.text +
    rationale.text -- exactly what memidx.py actually puts into FTS5 and
    the embedding input (build_record/insert_record_rows/embed_text_for;
    see bench/README.md "The paraphrase queries"), never a superseded
    link's own text, which is a separate, unindexed-by-default row."""
    if record_id in topics:
        fm, body = topics[record_id]
        parts = [fm.get("title", ""), body]
        current = memidx.newest_active_link(fm.get("links") or [])
        if current:
            ruling = current.get("ruling") or {}
            rationale = current.get("rationale") or {}
            parts.append(ruling.get("text") or "")
            parts.append(rationale.get("text") or "")
        return " ".join(parts)
    fm, body = incidents[record_id]
    return " ".join([fm.get("title", ""), body])


def _load_topics_and_incidents_with_body() -> tuple[dict, dict]:
    topics: dict[str, tuple] = {}
    incidents: dict[str, tuple] = {}
    for f in memidx.walk_markdown(CORPUS):
        result = memidx.parse_record(f)
        if not result.valid:
            continue
        fm, body = result.frontmatter, result.body
        rid = str(fm.get("id") or f.stem)
        if fm.get("links") or fm.get("type") == "topic":
            topics[rid] = (fm, body)
        elif fm.get("type") == "incident":
            incidents[rid] = (fm, body)
    return topics, incidents


class TestParaphraseIndependence(unittest.TestCase):
    def test_paraphrase_queries_share_no_vocabulary_with_their_target(self):
        topics, incidents = _load_topics_and_incidents_with_body()
        para = [q for q in load_queries() if q["id"].startswith("para-")]
        self.assertGreaterEqual(len(para), 10)
        for q in para:
            self.assertEqual(len(q["expect"]), 1, f"{q['id']}: paraphrase queries target exactly one record")
            target = q["expect"][0]
            indexed = _indexed_text_for(target, topics, incidents)
            overlap = _tokens(q["query"]) & _tokens(indexed)
            self.assertEqual(
                overlap, set(),
                f"{q['id']} -> {target}: shares vocabulary {sorted(overlap)} with its target's "
                f"indexed text -- this is no longer a genuine paraphrase",
            )

    def test_kw_queries_are_not_accidentally_zero_overlap(self):
        """Sanity check on the *other* half of the question set: the kw-
        queries are supposed to have real overlap (that's what makes them
        the easy case the paraphrase set is contrasted against). If one
        accidentally has none, the corpus/query pairing has drifted."""
        topics, incidents = _load_topics_and_incidents_with_body()
        kw = [q for q in load_queries() if q["id"].startswith("kw-")]
        self.assertGreaterEqual(len(kw), 5)
        for q in kw:
            target = q["expect"][0]
            indexed = _indexed_text_for(target, topics, incidents)
            overlap = _tokens(q["query"]) & _tokens(indexed)
            self.assertNotEqual(overlap, set(), f"{q['id']} -> {target}: expected some shared vocabulary")


# ---------------------------------------------------------------------------
# 7. INC-0115's FTS-step-aside threshold: pin the coverage separation the
#    code comment next to FTS_STEP_ASIDE_COVERAGE claims, against the REAL
#    gate memidx._fts_top_hit_is_confident and the REAL, status-filtered,
#    family-collapsed fts_ranked() top-1 -- not a reimplementation of
#    either, which is exactly the kind of drift that produced a wrong
#    number in that comment's first draft (a raw, unfiltered `fts MATCH`
#    query can surface a superseded link row real search never returns).
# ---------------------------------------------------------------------------

class TestFtsStepAsideCoverage(unittest.TestCase):
    # para-10 is the one paraphrase query whose FTS top-1 pick is a
    # genuinely different, wrong record that happens to share real
    # vocabulary with the query -- paraphrase queries are constructed to
    # share no vocabulary with their OWN target (see
    # TestParaphraseIndependence above), not with every other record in
    # the corpus. The gate correctly leaves it to fuse normally.
    EXPECTED_NOT_CONFIDENT = {
        "para-01", "para-02", "para-03", "para-04", "para-05",
        "para-06", "para-07", "para-08", "para-09", "para-11",
    }
    EXPECTED_CONFIDENT_EXCEPTION = "para-10"

    def test_kw_and_et_always_confident_para_mostly_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            db = Path(td) / "idx.sqlite"
            rc = memidx.cmd_reindex(type("NS", (), {
                "root": str(CORPUS), "db": str(db), "project": memidx.DEFAULT_PROJECT,
                "full": False, "no_embed": False,
            })())
            self.assertEqual(rc, 0)

            orig_gate = memidx._fts_top_hit_is_confident
            questions = [q for q in load_queries() if q["kind"] == "question"]
            verdicts: dict[str, bool] = {}
            try:
                for q in questions:
                    captured = {}

                    def wrapped(conn, project, query, top_path, _cap=captured):
                        v = orig_gate(conn, project, query, top_path)
                        _cap["verdict"] = v
                        return v

                    memidx._fts_top_hit_is_confident = wrapped
                    buf = io.StringIO()
                    with contextlib.redirect_stdout(buf):
                        rc = memidx.main([
                            "search", "--project", memidx.DEFAULT_PROJECT, "--db", str(db),
                            q["query"], "--mode", "hybrid", "--json", "--limit", "1",
                        ])
                    self.assertEqual(rc, 0, q["id"])
                    # The gate is only called when both channels return at
                    # least one candidate (see _search_hits); every query
                    # in this corpus does, so it must have been captured.
                    self.assertIn("verdict", captured, f"{q['id']}: gate was not invoked")
                    verdicts[q["id"]] = captured["verdict"]
            finally:
                memidx._fts_top_hit_is_confident = orig_gate

            for q in questions:
                qid = q["id"]
                if qid.startswith("kw-") or qid.startswith("et-"):
                    self.assertTrue(verdicts[qid], f"{qid}: expected FTS to be confident (plain/exact-term query)")
                elif qid == self.EXPECTED_CONFIDENT_EXCEPTION:
                    self.assertTrue(verdicts[qid], f"{qid}: expected the documented confident-but-wrong exception")
                elif qid in self.EXPECTED_NOT_CONFIDENT:
                    self.assertFalse(verdicts[qid], f"{qid}: expected FTS to step aside (paraphrase noise)")


if __name__ == "__main__":
    unittest.main()
