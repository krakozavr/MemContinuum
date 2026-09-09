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

import json
import math
import os
import re
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

    def test_at_least_10_exact_term_queries(self):
        """Grok 13: the et- slice had no floor -- it could silently vanish
        (or shrink to nothing meaningful) without failing the suite. This
        does not check that each query's claimed phrase is actually
        present in its target (that is still verified by hand -- see
        bench/README.md's "The exact-term queries"), only that the slice
        itself cannot quietly disappear."""
        et = [q for q in load_queries() if q["id"].startswith("et-")]
        self.assertGreaterEqual(len(et), 10)

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
# 3b. --json's envelope shape (Codex 9): {"runners", "negative_control"},
#     an untested breaking change vs. main's old flat-dict-of-runner-names
#     shape when this was introduced -- pinned here so a future change to
#     it is a deliberate, visible diff, not a silent break.
# ---------------------------------------------------------------------------

class TestJSONOutputShape(unittest.TestCase):
    def test_json_envelope_is_runners_and_negative_control(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = score.main(["--json", "--runner", "nomemory", "--runner", "keyword"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(set(payload.keys()), {"runners", "negative_control"})
        self.assertEqual(set(payload["runners"].keys()), {"nomemory", "keyword"})
        for slices in payload["runners"].values():
            self.assertEqual(
                set(slices.keys()),
                {"overall", "path", "question", "paraphrase", "exact-term", "plain", "errors"},
            )
        nc = payload["negative_control"]
        self.assertEqual(set(nc.keys()) - {"nomemory", "keyword"}, {"verdict", "failed_runners"})
        for runner_result in (nc["nomemory"], nc["keyword"]):
            self.assertEqual(
                set(runner_result.keys()),
                {"real_mrr", "shuffled_mrr", "gain", "spread", "se", "returns_nothing",
                 "is_exempt_baseline", "separates"},
            )
        # nomemory (declared, empty, zero errors, full coverage) always
        # separates by exemption; the real corpus + keyword always
        # separates too (TestNegativeControl's own live-corpus test covers
        # this properly) -- both true here means the run is "ok", so this
        # doubles as a check that --json's exit code matches its own
        # printed verdict (M3).
        self.assertEqual(nc["verdict"], "ok")
        self.assertEqual(rc, 0)


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
            proc = _run_runner(RUNNERS_DIR / "keyword_baseline.py", kind, query)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            lines = [ln for ln in proc.stdout.splitlines()]
            self.assertTrue(all(ln.strip() == ln and ln for ln in lines), lines)
            self.assertEqual(len(lines), len(set(lines)), "keyword runner must not repeat an id")

    def test_keyword_empty_query_is_empty_not_a_crash(self):
        proc = _run_runner(RUNNERS_DIR / "keyword_baseline.py", "question", "   ")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_keyword_respects_limit(self):
        argv = [sys.executable, str(RUNNERS_DIR / "keyword_baseline.py"), "--corpus", str(CORPUS),
                "--kind", "question", "--query", "the a of and to", "--limit", "2"]
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertLessEqual(len(lines), 2)

    def test_bad_corpus_dir_is_a_clean_nonzero_exit_not_a_traceback(self):
        for script in ("nomemory.py", "keyword_baseline.py"):
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

_STOPWORDS = frozenset("""
a an the is are was were be been being do does did doing have has had having
i you he she it we they me him her us them my your his its our their this
that these those to of in on at by for with about against between into
through during before after above below from up down out off over under
again further then once here there when where why how all any both each
few more most other some such no nor not only own same so than too very
can will just don should now what which who whom or and but if because as
until while
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return {w for w in _TOKEN_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1}


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


def _record_paths_by_id() -> dict[str, Path]:
    """record id -> the markdown file it lives in, for the stricter
    full-raw-file independence check below (keyword_baseline.py's own
    search surface, not memidx's narrower indexed-text subset)."""
    out: dict[str, Path] = {}
    for f in memidx.walk_markdown(CORPUS):
        result = memidx.parse_record(f)
        if not result.valid:
            continue
        rid = str(result.frontmatter.get("id") or f.stem)
        out[rid] = f
    return out


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

    def test_paraphrase_queries_share_no_vocabulary_with_the_full_raw_record(self):
        """Fix round (Grok 8, MAJOR/must-fix in that gate): the check above
        matches what memidx.py/memcontinuum actually index and search, but
        bench/README.md's `keyword` baseline searches the ENTIRE raw
        markdown file -- frontmatter, `alternatives`, `evidence`, `tags`,
        even a superseded link's own text. A query can pass the
        indexed-text check above and still leak through a field that check
        never reads: para-01 did, before this fix round -- 'behave' and
        'version' leaked via TOP-107's `alternatives.rejected_because` and
        its superseded L1 link's own ruling text, and keyword ranked
        TOP-107 first (MRR 1.0) for what was supposed to be the hard case.
        This is the stricter check against that whole surface, so a future
        corpus edit that reintroduces a full-file leak fails the suite the
        same way the indexed-text-only leak used to slip through."""
        paths = _record_paths_by_id()
        para = [q for q in load_queries() if q["id"].startswith("para-")]
        for q in para:
            target = q["expect"][0]
            raw = paths[target].read_text(encoding="utf-8")
            overlap = _tokens(q["query"]) & _tokens(raw)
            self.assertEqual(
                overlap, set(),
                f"{q['id']} -> {target}: shares vocabulary {sorted(overlap)} with its target's "
                f"FULL RAW FILE (not just its indexed text) -- keyword_baseline.py searches "
                f"this whole surface, so this leak can inflate keyword's score on what should "
                f"be the hard case",
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


class TestRunnersDoNotShadowStdlib(unittest.TestCase):
    """A runner is executed as a script, so its own directory is first on
    sys.path. A runner named after a standard-library module therefore
    shadows it for every import the interpreter makes while starting --
    `collections` imports `keyword`, so `bench/runners/keyword.py` broke
    every runner on CI with a circular-import AttributeError while passing
    locally. Names are the whole defence; this test is the guard."""

    def test_no_runner_filename_shadows_a_stdlib_module(self):
        import importlib.util
        offenders = []
        for script in sorted(RUNNERS_DIR.glob("*.py")):
            name = script.stem
            if name.startswith("_"):
                continue
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            if spec is None:
                continue
            origin = spec.origin or ""
            if origin == "built-in" or "lib/python" in origin.replace("\\", "/"):
                offenders.append(f"{script.name} shadows stdlib {name!r} ({origin})")
        self.assertEqual(offenders, [], "; ".join(offenders))


class TestNegativeControl(unittest.TestCase):
    """A benchmark can report a flattering number while separating nothing.
    Fix round (external review, two independent gates): the ORIGINAL control
    reversed each runner's own ranked output and rescored it against the
    SAME query's expect. That tested ranking order, not query-sensitivity --
    a query-blind fixed-list runner passed (reversing a fixed list still
    looks query-sensitive on average), and a length-1 or match-set result
    (this corpus's own `path` kind) could never change under reversal at
    all, so 20 of 57 queries were invisible to it. The control now severs
    the QUERY-TO-RESULT association instead: each query's own ranked output
    is rescored against a DIFFERENT query's expect (a fixed, deterministic
    derangement of the query id list -- see bench/score.py's negative_control
    module comment for the full derivation, including why the fixed-ranker
    case below produces an EXACT zero gain, not an approximate one).

    Fix round 2 (a SECOND external re-gate, two independent reviewers, same
    residual hole): that single corpus-wide derangement mixed `path` and
    `question` ids, and this corpus's two kinds have systematically
    different expect distributions -- a runner that never reads `--query`
    and branches only on `--kind` passed. The derangement is now
    kind-preserving (one rotation per kind group, see
    `TestKindPreservingDerangementClosesTheKindLeak` below for the live
    counterexamples), and the exemption below is bound to
    `is_canonical_null_baseline`, a flag `run_all` sets from the RESOLVED
    SCRIPT PATH -- never from `display`, which is user-controlled for a
    path-spec runner (`script.stem`). Tests below that construct a `rep`
    dict directly (bypassing `run_all`) must set this flag explicitly; it
    defaults to `False`, matching a runner `run_all` never vouched for."""

    def _report(self, ranked_by_qid, expect_by_qid, display="r", is_canonical_null_baseline=False):
        per_query = {}
        for qid, ranked in ranked_by_qid.items():
            m = score.evaluate_query(expect_by_qid[qid], ranked)
            m["ranked"] = ranked
            per_query[qid] = m
        return {display: {
            "per_query": per_query,
            "errors": {},
            "is_canonical_null_baseline": is_canonical_null_baseline,
        }}

    def _queries(self, expect_by_qid, kind="question"):
        return [{"id": q, "kind": kind, "query": "q", "expect": e}
                for q, e in expect_by_qid.items()]

    def test_a_good_ranking_separates_from_its_shuffle(self):
        expect = {"q1": ["a"], "q2": ["b"], "q3": ["c"], "q4": ["d"]}
        ranked = {"q1": ["a", "x", "y"], "q2": ["b", "x", "y"],
                  "q3": ["c", "x", "y"], "q4": ["d", "x", "y"]}
        rep = self._report(ranked, expect)
        c = score.negative_control(rep, self._queries(expect))
        self.assertEqual(c["verdict"], "ok", c)
        self.assertTrue(c["r"]["separates"])
        self.assertAlmostEqual(c["r"]["real_mrr"], 1.0)
        self.assertAlmostEqual(c["r"]["shuffled_mrr"], 0.0)
        self.assertGreater(c["r"]["gain"], 0)

    def test_a_query_blind_fixed_ranker_gets_exactly_zero_gain_and_fails(self):
        """Codex's counterexample against the OLD (reversal) control: a
        runner that returns the identical list for every query, ignoring
        the query entirely, used to pass with gain +0.4583. Here the same
        shape of runner gets EXACTLY zero gain (proven algebraically in
        bench/score.py, not just empirically low) and fails."""
        expect = {"q1": ["a"], "q2": ["b"], "q3": ["c"], "q4": ["d"]}
        fixed_list = ["a", "b", "c", "d"]  # returned for every query, unchanged
        ranked = {qid: fixed_list for qid in expect}
        rep = self._report(ranked, expect, display="fixed")
        c = score.negative_control(rep, self._queries(expect))
        self.assertAlmostEqual(c["fixed"]["gain"], 0.0, places=9)
        self.assertFalse(c["fixed"]["separates"])
        self.assertEqual(c["verdict"], "inconclusive")
        self.assertIn("fixed", c["failed_runners"])

    def test_a_perfect_ranker_passes_and_path_kind_length_one_results_participate(self):
        """The OLD control could never move a length-1 (or match-set)
        result at all -- this benchmark's own `path` queries were
        structurally invisible to it. Here a perfect `path`-shaped runner
        (one exact id per query, the for-path match-set shape) shows a real
        gain, proving path-kind queries now participate."""
        expect = {"p1": ["A"], "p2": ["B"], "p3": ["C"], "p4": ["D"]}
        ranked = {"p1": ["A"], "p2": ["B"], "p3": ["C"], "p4": ["D"]}
        rep = self._report(ranked, expect, display="path-perfect")
        c = score.negative_control(rep, self._queries(expect, kind="path"))
        self.assertEqual(c["verdict"], "ok", c)
        self.assertTrue(c["path-perfect"]["separates"])
        self.assertAlmostEqual(c["path-perfect"]["real_mrr"], 1.0)
        self.assertGreater(c["path-perfect"]["gain"], 0,
                            "a length-1 path-kind result must be able to show a nonzero "
                            "gain now -- reversal could never move it at all")

    def test_a_query_set_that_cannot_tell_them_apart_is_inconclusive(self):
        # every runner scores identically against every query's own expect
        # AND against its derangement partner's expect (all four expects
        # are found at the same rank in every ranked list) -- nothing
        # distinguishes a real run from a shuffled one.
        expect = {"q1": ["x"], "q2": ["x"], "q3": ["x"], "q4": ["x"]}
        ranked = {qid: ["x", "y"] for qid in expect}
        rep = self._report(ranked, expect)
        c = score.negative_control(rep, self._queries(expect))
        self.assertEqual(c["verdict"], "inconclusive")
        self.assertIn("r", c["failed_runners"])
        self.assertFalse(c["r"]["separates"])

    def test_all_erroring_runner_is_inconclusive_not_excused(self):
        """B2: all(...) over an empty per_query used to be vacuously True,
        so a runner that errors on every query was reported `ok`."""
        expect = {"q1": ["a"], "q2": ["b"]}
        rep = {"broken": {"per_query": {}, "errors": {"q1": "boom", "q2": "boom"}}}
        c = score.negative_control(rep, self._queries(expect))
        self.assertFalse(c["broken"]["separates"])
        self.assertEqual(c["verdict"], "inconclusive")
        self.assertIn("broken", c["failed_runners"])

    def test_silently_empty_undeclared_runner_is_inconclusive_not_excused(self):
        """B2: only the runner run_all flagged as the canonical null baseline
        (`is_canonical_null_baseline`, from the resolved script path -- see
        `score.NULL_BASELINE_SCRIPT`) may return nothing and still pass."""
        expect = {"q1": ["a"], "q2": ["b"]}
        rep = self._report({"q1": [], "q2": []}, expect, display="mystery-empty")
        c = score.negative_control(rep, self._queries(expect))
        self.assertTrue(c["mystery-empty"]["returns_nothing"])
        self.assertFalse(c["mystery-empty"]["is_exempt_baseline"])
        self.assertFalse(c["mystery-empty"]["separates"])
        self.assertEqual(c["verdict"], "inconclusive")

    def test_the_real_nomemory_baseline_is_still_ok(self):
        """B2 red test: the one runner actually entitled to the exemption
        (canonical script path, empty by design, zero errors, full
        coverage) still passes."""
        expect = {"q1": ["a"], "q2": ["b"]}
        rep = self._report({"q1": [], "q2": []}, expect, display="nomemory",
                            is_canonical_null_baseline=True)
        c = score.negative_control(rep, self._queries(expect))
        self.assertTrue(c["nomemory"]["returns_nothing"])
        self.assertTrue(c["nomemory"]["is_exempt_baseline"])
        self.assertTrue(c["nomemory"]["separates"])
        self.assertEqual(c["verdict"], "ok")

    def test_declared_baseline_with_errors_is_not_exempt(self):
        """Being the canonical baseline SCRIPT is not the whole exemption --
        it must also have zero errors and full query coverage (Grok's
        'footgun for any other caller' concern, made explicit rather than
        implicit). `is_canonical_null_baseline` is True here (this IS the
        real nomemory.py, per run_all's own resolved-path check), yet the
        run still fails the exemption because it errored on q2."""
        expect = {"q1": ["a"], "q2": ["b"]}
        m = score.evaluate_query(expect["q1"], [])
        m["ranked"] = []
        rep = {"nomemory": {
            "per_query": {"q1": m}, "errors": {"q2": "boom"},
            "is_canonical_null_baseline": True,
        }}
        c = score.negative_control(rep, self._queries(expect))
        self.assertFalse(c["nomemory"]["separates"])
        self.assertEqual(c["verdict"], "inconclusive")

    def test_display_name_alone_no_longer_grants_the_exemption(self):
        """R2 (fix round 2, external re-gate, two independent reviewers):
        `display` is `script.stem` for a path-spec runner, entirely
        user-controlled. Naming an impostor script "nomemory.py" used to be
        enough to be exempted; here the display string IS "nomemory" but
        `is_canonical_null_baseline` is (correctly) unset, because nothing
        vouched for this being the real script -- exemption must not
        trigger."""
        expect = {"q1": ["a"], "q2": ["b"]}
        rep = self._report({"q1": [], "q2": []}, expect, display="nomemory")
        c = score.negative_control(rep, self._queries(expect))
        self.assertTrue(c["nomemory"]["returns_nothing"])
        self.assertFalse(c["nomemory"]["is_exempt_baseline"])
        self.assertFalse(c["nomemory"]["separates"])
        self.assertEqual(c["verdict"], "inconclusive")

    def test_unknown_query_id_fails_closed_with_a_clear_message(self):
        """Grok 10: a report carrying a qid absent from the query set used
        to KeyError deep inside the derangement lookup. Now it raises a
        clear, specific error instead."""
        expect = {"q1": ["a"], "q2": ["b"]}
        rep = self._report({"q1": ["a"], "not-a-real-query": ["a"]},
                            {"q1": ["a"], "not-a-real-query": ["a"]})
        with self.assertRaises(ValueError) as ctx:
            score.negative_control(rep, self._queries(expect))
        self.assertIn("not-a-real-query", str(ctx.exception))

    def test_the_live_corpus_separates_for_the_keyword_baseline(self):
        # the real thing, not a fixture: if this ever goes inconclusive the
        # query set has decayed and every published number is suspect.
        queries = score.load_queries(QUERIES_PATH)
        rep = score.run_all(queries, CORPUS, ["keyword"], 10)
        c = score.negative_control(rep, queries)
        self.assertEqual(c["verdict"], "ok", c)


# ---------------------------------------------------------------------------
# Fix round 2 (external re-gate, two independent reviewers): both built a
# WORKING runner script and ran it through the live bench/score.py to defeat
# the control that shipped after fix round 1. Kept here as regression
# fixtures, run through the real bench/score.py pipeline (run_all + the live
# 57-query corpus), not just against a small in-memory report, so a future
# change to the derangement or the exemption is caught the same way the
# reviewers caught the original hole.
# ---------------------------------------------------------------------------

_KIND_BRANCHING_RUNNER = '''#!/usr/bin/env python
"""Never reads --query at all: a fixed corpus-class ranking keyed only on
--kind. This is the exact shape both re-gate reviewers independently built
against fix round 1's corpus-wide derangement."""
import argparse, sys

CON = ["CON-301", "CON-302", "CON-303", "CON-304"]
INC = ["INC-201", "INC-202", "INC-203", "INC-204", "INC-205", "INC-206"]

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--corpus", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--mode", default=None)
    args = p.parse_args(argv)
    out = CON if args.kind == "path" else INC
    print("\\n".join(out))
    return 0

if __name__ == "__main__":
    sys.exit(main())
'''


class TestKindPreservingDerangementClosesTheKindLeak(unittest.TestCase):
    """R1 (fix round 2, Codex 1 BLOCKING / Grok 1 MAJOR on re-gate): the
    runner receives `--kind` separately from `--query` (see `run_query`),
    and fix round 1's derangement rotated the WHOLE 57-query id list,
    pairing `path` queries with `question` queries (and vice versa) about
    70% of the time. Because this corpus's two kinds have systematically
    different expect distributions (path expects skew toward CON-* ids;
    some question expects are INC-* ids), a runner that ignores the query
    text and returns a fixed concept list for `path` / a fixed incident
    list otherwise beat its own shuffled twin and passed (real gate:
    Codex measured +0.0839; Grok's own script scored similarly and exited
    0). The derangement is now kind-preserving (see
    `_kind_preserving_derangement`): this exact runner, run through the
    real `bench/score.py` pipeline against the live corpus, must now score
    EXACTLY zero gain and fail."""

    def test_the_kind_branching_counterexample_now_scores_exactly_zero_gain(self):
        queries = score.load_queries(QUERIES_PATH)
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "con_inc.py"
            script.write_text(_KIND_BRANCHING_RUNNER, encoding="utf-8")
            script.chmod(0o755)
            rep = score.run_all(queries, CORPUS, [str(script)], 10)
            c = score.negative_control(rep, queries)
        display = "con_inc"
        self.assertIn(display, c)
        self.assertAlmostEqual(c[display]["gain"], 0.0, places=9,
                                msg="a runner that only ever reads --kind must score "
                                    "EXACTLY zero gain under a kind-preserving derangement")
        self.assertFalse(c[display]["separates"])
        self.assertEqual(c["verdict"], "inconclusive")
        self.assertIn(display, c["failed_runners"])

    def test_real_runners_still_separate_under_the_kind_preserving_derangement(self):
        # The fix must not have narrowed the control so far that a runner
        # that genuinely reads the query now fails it.
        queries = score.load_queries(QUERIES_PATH)
        rep = score.run_all(queries, CORPUS, ["keyword"], 10)
        c = score.negative_control(rep, queries)
        self.assertEqual(c["verdict"], "ok", c)
        self.assertGreater(c["keyword"]["gain"], 0)


class TestBaselineExemptionBoundToScriptPath(unittest.TestCase):
    """R2 (fix round 2, Codex 2 BLOCKING / Grok 5 NIT on re-gate): the
    exemption used to check `display`, which is `script.stem` for a
    path-spec runner -- entirely user-controlled. An empty impostor script
    saved as `.../nomemory.py` anywhere was exempted; the identical file
    saved as `empty.py` correctly failed. `run_all` now sets
    `is_canonical_null_baseline` from the RESOLVED SCRIPT PATH, compared
    against `bench/runners/nomemory.py` itself -- these tests exercise
    `run_all`, not a hand-built `rep`, so they prove the binding actually
    happens where the spoof was demonstrated, not just in negative_control's
    own logic."""

    def test_the_real_nomemory_spec_is_flagged_canonical(self):
        queries = score.load_queries(QUERIES_PATH)[:2]
        rep = score.run_all(queries, CORPUS, ["nomemory"], 10)
        self.assertTrue(rep["nomemory"]["is_canonical_null_baseline"])
        c = score.negative_control(rep, queries)
        self.assertTrue(c["nomemory"]["is_exempt_baseline"])
        self.assertEqual(c["verdict"], "ok")

    def test_an_impostor_script_named_nomemory_py_is_not_flagged_canonical(self):
        queries = score.load_queries(QUERIES_PATH)[:2]
        with tempfile.TemporaryDirectory() as td:
            # Byte-for-byte the same empty-output behavior as the real
            # nomemory.py, saved under the SAME basename, at a DIFFERENT
            # path -- this is exactly what the re-gate report ran.
            script = Path(td) / "nomemory.py"
            script.write_text(RUNNERS_DIR.joinpath("nomemory.py").read_text(encoding="utf-8"),
                               encoding="utf-8")
            script.chmod(0o755)
            rep = score.run_all(queries, CORPUS, [str(script)], 10)
            self.assertFalse(rep["nomemory"]["is_canonical_null_baseline"])
            c = score.negative_control(rep, queries)
        self.assertFalse(c["nomemory"]["is_exempt_baseline"])
        self.assertFalse(c["nomemory"]["separates"])
        self.assertEqual(c["verdict"], "inconclusive")


class TestSampleStandardErrorReplacesPopulation(unittest.TestCase):
    """R3 (fix round 2, Codex 3 MAJOR / Grok 2 MAJOR on re-gate): using
    `statistics.pstdev` (population) instead of `statistics.stdev` (sample)
    made a runner correct on exactly ONE query, empty on every other one,
    ALWAYS pass -- for a single 1.0 among (n-1) zeros, mean > population_se
    reduces algebraically to sqrt(n) > sqrt(n-1), true for every n. The
    sample-SD version makes that an EXACT algebraic tie (mean == sample_se
    == 1/n), so the strict `gain > se` correctly reports it as not
    separating. This is a floor-arithmetic fix, not a claim that every
    one-hit-shaped runner now fails -- see bench/score.py's negative_control
    module comment for why a real single correct answer nobody else could
    reproduce by chance can still legitimately separate."""

    def test_one_hit_among_many_empties_is_now_a_tie_not_a_guaranteed_pass(self):
        n = 57
        expect = {f"q{i}": [f"ans{i}"] for i in range(n)}
        ranked = {f"q{i}": (["ans0"] if i == 0 else []) for i in range(n)}
        rep = TestNegativeControl()._report(ranked, expect, display="one-hit")
        c = score.negative_control(rep, TestNegativeControl()._queries(expect))
        self.assertAlmostEqual(c["one-hit"]["gain"], c["one-hit"]["se"], places=9)
        self.assertFalse(c["one-hit"]["separates"])


if __name__ == "__main__":
    unittest.main()
