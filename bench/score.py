#!/usr/bin/env python
"""bench/score.py -- runs every query in a query set through one or more
runners and reports Recall@1/3/10 and MRR, per query kind and overall.

Deterministic, no network, no model of its own: this file only reads
files, spawns the runner subprocesses documented in bench/README.md, and
does arithmetic on their output.

    python bench/score.py
        runs bench/queries.jsonl against bench/corpus with the five
        canonical runners (nomemory, keyword, memcontinuum:fts,
        memcontinuum:vector, memcontinuum:hybrid) and prints a table.

    python bench/score.py --runner keyword --runner memcontinuum:hybrid
        runs only the named runners. A bare NAME resolves to
        bench/runners/<NAME>.py; a NAME:mode suffix is forwarded to the
        runner as --mode (meaningful for memcontinuum, ignored by the
        others). A path (containing "/" or ending ".py") is used as-is,
        which is how a runner for another system, living anywhere, plugs
        in without editing this file -- see bench/README.md.

    python bench/score.py --json
        same run, machine-readable output instead of the table.

    python bench/score.py --private
        the closed extra gate (project brief line 9): resolves
        fixtures/records/queries.json (PRIVATE, untracked) against a
        throwaway copy of the private incident corpus, the same
        construction tests/test_memidx.py::TestD5Paraphrase uses, and
        prints ONLY the aggregate table -- never a query's own text or the
        path substring it resolves against, so nothing private reaches
        report.md or any other tracked output even by accident. Skips
        cleanly, printing why, when fixtures/records/ is absent (a fresh
        clone, or any checkout other than the one this was authored on).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNNERS_DIR = REPO_ROOT / "bench" / "runners"
DEFAULT_QUERIES = REPO_ROOT / "bench" / "queries.jsonl"
DEFAULT_CORPUS = REPO_ROOT / "bench" / "corpus"
DEFAULT_RUNNERS = ["nomemory", "keyword", "memcontinuum:fts", "memcontinuum:vector", "memcontinuum:hybrid"]
KS = (1, 3, 10)

_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)


# ---------------------------------------------------------------------------
# query set / runner resolution
# ---------------------------------------------------------------------------

def load_queries(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            missing = {"id", "kind", "query", "expect", "notes"} - obj.keys()
            if missing:
                raise ValueError(f"{path}:{lineno}: missing field(s) {sorted(missing)}")
            if obj["kind"] not in ("path", "question"):
                raise ValueError(f"{path}:{lineno}: kind must be 'path' or 'question', got {obj['kind']!r}")
            if not obj["expect"]:
                raise ValueError(f"{path}:{lineno}: expect must be a non-empty list")
            out.append(obj)
    return out


def resolve_runner(spec: str) -> tuple[str, Path, str | None]:
    """spec -> (display_name, script_path, mode). spec is NAME or NAME:mode
    or a/path.py or a/path.py:mode."""
    name, _, mode = spec.partition(":")
    mode = mode or None
    if "/" in name or name.endswith(".py"):
        script = Path(name)
        if not script.is_absolute():
            script = (REPO_ROOT / script).resolve()
        display = script.stem
    else:
        # The canonical name "keyword" maps to keyword_baseline.py: a runner
        # is executed as a script, so its own directory is first on sys.path,
        # and a file named keyword.py shadows the stdlib module `collections`
        # imports during interpreter startup -- which broke every runner on CI
        # while passing locally. The user-facing name stays "keyword".
        filename = "keyword_baseline" if name == "keyword" else name
        script = RUNNERS_DIR / f"{filename}.py"
        display = name
    if not script.is_file():
        raise FileNotFoundError(f"runner script not found: {script} (from spec {spec!r})")
    display = f"{display}:{mode}" if mode else display
    return display, script, mode


# ---------------------------------------------------------------------------
# running one query through one runner
# ---------------------------------------------------------------------------

class RunnerError(Exception):
    pass


def run_query(script: Path, corpus: Path, kind: str, query: str, mode: str | None, limit: int) -> list[str]:
    argv = [sys.executable, str(script), "--corpus", str(corpus), "--kind", kind, "--query", query, "--limit", str(limit)]
    if mode:
        argv += ["--mode", mode]
    env = dict(os.environ)
    env["PYTHONPATH"] = ""  # hard rule: never inherit the machine's poisoned PYTHONPATH
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"timed out: {exc}") from exc
    if proc.returncode != 0:
        raise RunnerError(f"exit {proc.returncode}: {proc.stderr.strip()[:500]}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# metrics -- pure functions, unit-tested directly in tests/test_bench.py
# ---------------------------------------------------------------------------

def evaluate_query(expect: list[str], ranked_ids: list[str], ks: tuple[int, ...] = KS) -> dict:
    """Recall@k = |top-k results ∩ expect| / |expect| (generalizes the
    common hit@k/success@k to a query with more than one correct answer;
    reduces to hit@k exactly when len(expect) == 1, true for most of this
    benchmark's queries). MRR = 1 / rank of the first expected id anywhere
    in ranked_ids (not capped at any k -- a correct id ranked 11th still
    contributes 1/11, it just never counts for recall@10)."""
    expect_set = set(expect)
    if not expect_set:
        raise ValueError("expect must be non-empty")
    out = {}
    for k in ks:
        topk = set(ranked_ids[:k])
        out[f"recall@{k}"] = len(topk & expect_set) / len(expect_set)
    rr = 0.0
    for i, rid in enumerate(ranked_ids, start=1):
        if rid in expect_set:
            rr = 1.0 / i
            break
    out["mrr"] = rr
    return out


def aggregate(per_query: list[dict], ks: tuple[int, ...] = KS) -> dict:
    n = len(per_query)
    if n == 0:
        return {**{f"recall@{k}": 0.0 for k in ks}, "mrr": 0.0, "n": 0}
    agg = {f"recall@{k}": sum(m[f"recall@{k}"] for m in per_query) / n for k in ks}
    agg["mrr"] = sum(m["mrr"] for m in per_query) / n
    agg["n"] = n
    return agg


# ---------------------------------------------------------------------------
# driving a whole run
# ---------------------------------------------------------------------------

def run_all(queries: list[dict], corpus: Path, runner_specs: list[str], limit: int) -> dict:
    """{display_name: {"per_query": {qid: {...metrics, "ranked": [...]}},
    "errors": {qid: "message"}}}"""
    report = {}
    for spec in runner_specs:
        display, script, mode = resolve_runner(spec)
        per_query = {}
        errors = {}
        for q in queries:
            try:
                ranked = run_query(script, corpus, q["kind"], q["query"], mode, limit)
            except RunnerError as exc:
                errors[q["id"]] = str(exc)
                continue
            metrics = evaluate_query(q["expect"], ranked)
            metrics["ranked"] = ranked
            per_query[q["id"]] = metrics
        report[display] = {"per_query": per_query, "errors": errors}
    return report


# --- negative control -------------------------------------------------------
#
# A benchmark can report a flattering number while measuring almost nothing:
# if the query set is so easy that a deliberately crippled runner scores the
# same as the real one, the metric is not separating anything and the headline
# figure is noise. The control answers "would this query set notice if a
# runner ignored the query entirely?"
#
# Fix-round history: the first version of this control REVERSED each
# runner's own ranked output and rescored it against the SAME query's own
# expect. That tests whether the ORDER of a result list carries information,
# not whether the result RESPONDS TO THE QUERY -- two counterexamples from
# external review proved it wrong. (1) A query-blind runner that returns the
# identical fixed list for every query passed with gain +0.4583: reversing a
# fixed list still looks query-sensitive if the corpus happens to reward that
# fixed order on average. (2) Reversing a length-1 list, or a match-set whose
# order is not a ranking at all (this benchmark's own `path` kind -- see
# bench/README.md), is a no-op: MRR cannot change, so 20 of this file's 57
# queries were structurally invisible to the control regardless of the
# runner. Reversal is not kept as a secondary signal: both failure modes are
# severe enough (gameable in one direction, blind in the other) that a
# second number next to a misleading one adds confusion, not signal.
#
# The replacement severs the QUERY-TO-RESULT association instead of
# reordering a single result list: query i's ALREADY-COMPUTED ranked output
# is rescored against query perm(i)'s expect, where perm is a fixed,
# deterministic derangement (a permutation with no fixed point) of the query
# id list -- no RNG, reproducible on every machine, defined below.
#
# Why this is immune to counterexample (1): a query-blind runner returns the
# same ranked list R for every query, so its real score is
#   mean_i score(expect_i, R)
# and its shuffled score is
#   mean_i score(expect_perm(i), R).
# Because perm is a bijection on the query id set, {perm(i) : all i} is the
# SAME SET as {i : all i}, just relabeled -- summing score(expect_x, R) over
# every x in that set gives the identical total either way. The two means
# are therefore EXACTLY equal (not approximately, not "usually"): gain is 0
# for any query-blind ranker, for any R, on any query set, at any threshold.
# The corollary is also correct behavior, not a loophole: two queries that
# legitimately share the same expect (this corpus has three such pairs,
# path-06/path-07, path-09/path-10, path-13/path-14, sharing a concept's
# whole expansion) are mutually indistinguishable under a derangement that
# happens to pair them -- exactly as they should be, since a runner cannot
# be faulted for not telling apart two queries whose correct answer is
# identical.
#
# Why this is immune to counterexample (2): the control never reorders a
# result list, so it does not depend on that list having an order to
# destroy. A length-1 or match-set ranked output ["A"] scored against its
# own expect ["A"] (real MRR 1.0) and against some OTHER query's expect
# ["B"] (shuffled MRR 0.0, since "A" != "B") differ exactly as they should --
# `path` queries participate in the control for the first time in this file.
#
# The derangement: rotate the (file-order) query id list by half its own
# length. Any nonzero rotation has no fixed point (i + n/2 == i (mod n)
# would require n/2 == 0 (mod n), false for 0 < n/2 < n), so this is a valid
# derangement for any query count >= 2. Half the list length, rather than a
# rotation by 1, is deliberate: three pairs of ADJACENT queries in
# bench/queries.jsonl share an identical expect (see corollary above) purely
# because they were authored next to each other, not because a derangement
# should privilege pairing neighbors -- a rotate-by-1 derangement would pair
# exactly those adjacent duplicates with each other on every run, which
# weakens the control precisely where authoring order, not query content,
# put two expects next to each other. A half-length rotation is far enough
# from every adjacent pair in this file to avoid that, and (57 being odd)
# produces a single 57-element cycle rather than many short ones, spreading
# the mismatch across the whole query set rather than a few local swaps.
#
# Calibration: for each query, take the PAIRED difference between its real
# score and its shuffled score (real_i - shuffled_i), then require the MEAN
# of those paired differences to exceed their own standard error
# (population standard deviation of the per-query differences / sqrt(n)) --
# a lightweight one-sample test of "is this gain distinguishable from the
# per-query noise in the shuffled scores," rather than an unexplained
# constant someone picked. Standard error, not the raw per-query standard
# deviation: the raw spread is a property of ONE query's score, and
# comparing a MEAN gain against it penalizes exactly the runners that are
# consistently, moderately better across many queries (division by sqrt(n)
# is the textbook correction from "how noisy is one query" to "how noisy is
# the mean of n queries," which is what the gain actually estimates).
# Documented, known limitation of any threshold on a benchmark this size: a
# runner correct on exactly one query and empty on every other one produces
# a paired-difference distribution close to a single spike, whose standard
# error is (by construction, for large n) always just under its own tiny
# mean gain -- such a runner passes marginally. No fixed-size control can
# rule this out; it is a reason to read the MRR/Recall numbers alongside the
# verdict, not instead of it, exactly as bench/README.md's "Honest
# limitations" section already says about the corpus size.
def _derangement(ids: list[str]) -> dict[str, str]:
    """A fixed-point-free permutation of `ids`, as {id: paired_id}. See the
    module comment above `negative_control` for why a half-length rotation,
    not a rotation by 1 or a random permutation, is used."""
    n = len(ids)
    if n < 2:
        raise ValueError(f"negative_control needs at least 2 queries for a derangement, got {n}")
    offset = n // 2
    return {ids[i]: ids[(i + offset) % n] for i in range(n)}


NULL_BASELINE_RUNNERS = frozenset({"nomemory"})


def _is_declared_null_baseline(display: str) -> bool:
    """Only a runner spec whose base name (before any `:mode` suffix) is
    explicitly listed here is allowed to skip the gain requirement -- see
    the exemption's four-way AND in negative_control below. Nothing else
    that returns empty output, declared or not, gets a free pass."""
    return display.split(":", 1)[0] in NULL_BASELINE_RUNNERS


def negative_control(report: dict, queries: list[dict]) -> dict:
    """{runner: {"real_mrr", "shuffled_mrr", "gain", "spread", "returns_nothing",
    "separates"}} plus "verdict": "ok" | "inconclusive", naming the runners
    that failed. See the module comment above for the derangement and the
    calibrated threshold; see NULL_BASELINE_RUNNERS for the one exemption."""
    ids = [q["id"] for q in queries]
    expect_by_id = {q["id"]: q["expect"] for q in queries}
    perm = _derangement(ids)

    result = {}
    failed = []
    for display, data in report.items():
        pq = data["per_query"]
        errors = data.get("errors", {})
        unknown = set(pq) - expect_by_id.keys()
        if unknown:
            raise ValueError(
                f"negative_control: runner {display!r} reported result(s) for "
                f"query id(s) {sorted(unknown)} not present in the query set passed in -- "
                f"cannot pair them with a derangement partner"
            )

        diffs = []
        for qid, metrics in pq.items():
            ranked = metrics.get("ranked") or []
            real_mrr = metrics["mrr"]
            shuffled_mrr = evaluate_query(expect_by_id[perm[qid]], ranked)["mrr"]
            diffs.append(real_mrr - shuffled_mrr)

        real = aggregate(list(pq.values()))["mrr"]
        # shuffled_mrr is its own quantity (not derived from the paired
        # diffs' mean, though the two agree by construction) so the printed
        # table shows an average a reader could recompute independently.
        shuffled = aggregate([
            {**metrics, "mrr": evaluate_query(expect_by_id[perm[qid]], metrics.get("ranked") or [])["mrr"]}
            for qid, metrics in pq.items()
        ])["mrr"] if pq else 0.0
        gain = real - shuffled
        spread = statistics.pstdev(diffs) if len(diffs) > 1 else 0.0
        se = spread / math.sqrt(len(diffs)) if diffs else 0.0

        returns_nothing = bool(pq) and all(not (m.get("ranked") or []) for m in pq.values())
        full_coverage = len(pq) == len(queries)
        is_exempt_baseline = (
            _is_declared_null_baseline(display) and returns_nothing and not errors and full_coverage
        )

        if is_exempt_baseline:
            separates = True
        elif errors or not full_coverage:
            # A runner that errored on any query (compounded: score.py drops
            # errored queries from per_query entirely -- run_all above) or
            # otherwise did not cover every query cannot be trusted to
            # support a verdict either way.
            separates = False
        else:
            separates = gain > se

        result[display] = {
            "real_mrr": round(real, 4),
            "shuffled_mrr": round(shuffled, 4),
            "gain": round(gain, 4),
            "spread": round(spread, 4),
            "returns_nothing": returns_nothing,
            "is_exempt_baseline": is_exempt_baseline,
            "separates": separates,
        }
        if not separates:
            failed.append(display)
    result["verdict"] = "inconclusive" if failed else "ok"
    result["failed_runners"] = failed
    return result


def summarize(report: dict, queries: list[dict]) -> dict:
    """{display_name: {"overall": {...}, "path": {...}, "question": {...},
    "paraphrase": {...}, "exact-term": {...}, "plain": {...}, "errors": n}}
    -- a query with a runner error is excluded from that runner's own
    aggregates (never silently scored as zero, never silently dropped
    without a count).

    Three disjoint sub-slices of `question` by id prefix: "paraphrase"
    (`para-`, zero shared vocabulary with the target -- see bench/README.md),
    "exact-term" (`et-`, an error message/file name/symbol/flag/quoted
    phrase a keyword search should nail -- INC-0115 step 2), and "plain"
    (`kw-`, ordinary keyword-shaped developer questions, the baseline
    "plain" slice INC-0115 step 4 measures a fix against for regressions).
    `question` itself stays the union of all three (plus any other
    question-kind query), unchanged, for backward compatibility."""
    by_id = {q["id"]: q for q in queries}
    out = {}
    for display, data in report.items():
        pq = data["per_query"]

        def subset(pred):
            return [m for qid, m in pq.items() if pred(by_id[qid])]

        out[display] = {
            "overall": aggregate(list(pq.values())),
            "path": aggregate(subset(lambda q: q["kind"] == "path")),
            "question": aggregate(subset(lambda q: q["kind"] == "question")),
            "paraphrase": aggregate(subset(lambda q: q["id"].startswith("para-"))),
            "exact-term": aggregate(subset(lambda q: q["id"].startswith("et-"))),
            "plain": aggregate(subset(lambda q: q["id"].startswith("kw-"))),
            "errors": len(data["errors"]),
        }
    return out


# ---------------------------------------------------------------------------
# printing
# ---------------------------------------------------------------------------

def print_table(summary: dict) -> None:
    rows = ["overall", "path", "question", "paraphrase", "exact-term", "plain"]
    header = f"{'runner':<22}{'slice':<11}{'n':>4}{'R@1':>7}{'R@3':>7}{'R@10':>7}{'MRR':>7}{'errors':>8}"
    print(header)
    print("-" * len(header))
    for display, slices in summary.items():
        for i, row in enumerate(rows):
            s = slices[row]
            name = display if i == 0 else ""
            errcol = str(slices["errors"]) if i == 0 else ""
            print(
                f"{name:<22}{row:<11}{s['n']:>4}{s['recall@1']:>7.2f}{s['recall@3']:>7.2f}"
                f"{s['recall@10']:>7.2f}{s['mrr']:>7.2f}{errcol:>8}"
            )
        print()


def print_control(control: dict, runner_names) -> None:
    """Prints the negative-control block for either the public or the
    private gate (M3: "every run" in bench/README.md means every run --
    the private gate calls this too, on nothing but the aggregate numbers
    already safe to print there)."""
    print("negative control (query/result association broken by a deterministic "
          "derangement of the query list -- see bench/score.py's negative_control "
          "comment; a runner must beat its own query-shuffled score by more than "
          "that shuffled score's standard error to count as separating):")
    for display in runner_names:
        if display not in control:
            continue
        c = control[display]
        note = " (returns nothing by design)" if c["is_exempt_baseline"] else ""
        flag = "ok " if c["separates"] else "FLAT"
        print(f"  {flag}  {display:22s} real {c['real_mrr']:.2f}  "
              f"shuffled {c['shuffled_mrr']:.2f}  gain {c['gain']:+.2f}  "
              f"(shuffled spread {c['spread']:.2f}){note}")
    if control["verdict"] == "inconclusive":
        print()
        print("INCONCLUSIVE: " + ", ".join(control["failed_runners"])
              + " did not beat their own query-shuffled score by more than that "
                "score's standard error. The query set does not separate a "
                "query-sensitive ranking from a query-blind one for these "
                "runners, so their numbers say nothing about retrieval quality.")


# ---------------------------------------------------------------------------
# the private gate (project brief line 9): the same shape, run against
# fixtures/records/queries.json (untracked, private). Never prints a
# query's own text or the path substring it resolves against.
# ---------------------------------------------------------------------------

def _record_id_of(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="ignore")
    m = _ID_RE.search(text)
    return m.group(1) if m else path.stem


def _build_private_corpus(dest_root: Path) -> bool:
    """Mirrors tests/test_memidx.py::build_real_corpus: the 14 real
    incidents plus the hidden-files topic, copied into dest_root. Returns
    False (nothing built) when the private incidents directory is absent
    or empty."""
    src_incidents = REPO_ROOT / "fixtures" / "records" / "incidents"
    if not src_incidents.is_dir() or not any(src_incidents.glob("*.md")):
        return False
    inc_dir = dest_root / "incidents"
    inc_dir.mkdir(parents=True, exist_ok=True)
    for f in src_incidents.glob("*.md"):
        shutil.copy(f, inc_dir / f.name)
    hidden_files = REPO_ROOT / "fixtures" / "schema" / "topics" / "processing" / "hidden-files-in-count.md"
    if hidden_files.is_file():
        topic_dir = dest_root / "topics" / "processing"
        topic_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(hidden_files, topic_dir / hidden_files.name)
    return True


def _load_private_queries(private_corpus_root: Path) -> list[dict]:
    qjson = REPO_ROOT / "fixtures" / "records" / "queries.json"
    raw = json.loads(qjson.read_text(encoding="utf-8"))
    files = list(private_corpus_root.rglob("*.md"))
    out = []
    for i, (query, path_substr) in enumerate(raw):
        matches = [f for f in files if path_substr in str(f)]
        if not matches:
            continue  # unresolved entry: skip rather than fail the whole gate
        out.append({
            "id": f"private-{i}", "kind": "question", "query": query,
            "expect": [_record_id_of(matches[0])], "notes": "private gate (redacted)",
        })
    return out


def run_private_gate(runner_specs: list[str], limit: int) -> int:
    qjson = REPO_ROOT / "fixtures" / "records" / "queries.json"
    if not qjson.exists():
        print(
            "score.py --private: fixtures/records/queries.json not present -- "
            "this gate only runs on the machine that holds the private evaluation "
            "data; skipping cleanly.",
        )
        return 0
    with tempfile.TemporaryDirectory(prefix="memcontinuum-bench-private-") as td:
        root = Path(td) / "root"
        root.mkdir()
        if not _build_private_corpus(root):
            print("score.py --private: fixtures/records/incidents/ is empty; skipping cleanly.")
            return 0
        queries = _load_private_queries(root)
        if not queries:
            print("score.py --private: no private query resolved to a record; skipping cleanly.")
            return 0
        report = run_all(queries, root, runner_specs, limit)
        summary = summarize(report, queries)
        control = negative_control(report, queries) if len(queries) >= 2 else None
    # Only the aggregate table and the control's own numbers are ever
    # printed here -- never a query's own text or the path substring it
    # resolved against (see module docstring). The control's inputs are
    # score.py's own already-redacted `expect`/`ranked` id lists, never the
    # private query text itself, so this is safe on the same grounds.
    print(f"private gate: {len(queries)} private questions, corpus = fixtures/records/ (untracked)\n")
    print_table(summary)
    print()
    if control is None:
        print(f"negative control: skipped -- {len(queries)} private query(ies) resolved, "
              f"need >= 2 for a derangement.")
        return 0
    print_control(control, list(summary))
    return 1 if control["verdict"] == "inconclusive" else 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="score.py")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--runner", action="append", default=None,
                         help="NAME, NAME:mode, or a script path; repeatable. Default: the five canonical runners.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--private", action="store_true", help="run the closed extra gate instead (see module docstring)")
    args = parser.parse_args(argv)

    runner_specs = args.runner or DEFAULT_RUNNERS

    if args.private:
        if args.json:
            print("score.py --private: --json is not supported for the private gate (aggregate-only, deliberately).", file=sys.stderr)
            return 2
        return run_private_gate(runner_specs, args.limit)

    queries = load_queries(Path(args.queries))
    corpus = Path(args.corpus)
    report = run_all(queries, corpus, runner_specs, args.limit)
    summary = summarize(report, queries)
    control = negative_control(report, queries)

    if args.json:
        # keep the control OUT of `summary` itself: print_table and every
        # consumer iterate summary's keys as runner names.
        print(json.dumps({"runners": summary, "negative_control": control}, indent=2))
    else:
        print(f"{len(queries)} queries ({sum(1 for q in queries if q['kind']=='path')} path, "
              f"{sum(1 for q in queries if q['kind']=='question')} question, "
              f"{sum(1 for q in queries if q['id'].startswith('para-'))} paraphrase) "
              f"against corpus={corpus}\n")
        print_table(summary)
        print()
        print_control(control, list(summary))
        for display, data in report.items():
            for qid, msg in data["errors"].items():
                print(f"ERROR  {display}  {qid}: {msg}", file=sys.stderr)
    # M3 (fix round, external review): INCONCLUSIVE used to still exit 0,
    # so a caller/CI step that only checks the exit code would never
    # notice the harness disowning its own numbers. bench/README.md
    # documents this.
    return 1 if control["verdict"] == "inconclusive" else 0


if __name__ == "__main__":
    sys.exit(main())
