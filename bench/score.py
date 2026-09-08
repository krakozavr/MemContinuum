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
import os
import re
import shutil
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
        script = RUNNERS_DIR / f"{name}.py"
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
    # Only the aggregate table is ever printed here -- never a query's own
    # text or the path substring it resolved against (see module docstring).
    print(f"private gate: {len(queries)} private questions, corpus = fixtures/records/ (untracked)\n")
    print_table(summary)
    return 0


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

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{len(queries)} queries ({sum(1 for q in queries if q['kind']=='path')} path, "
              f"{sum(1 for q in queries if q['kind']=='question')} question, "
              f"{sum(1 for q in queries if q['id'].startswith('para-'))} paraphrase) "
              f"against corpus={corpus}\n")
        print_table(summary)
        for display, data in report.items():
            for qid, msg in data["errors"].items():
                print(f"ERROR  {display}  {qid}: {msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
