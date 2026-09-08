#!/usr/bin/env python
"""memcontinuum -- the runner under test. Wraps `memidx.py for-path` (kind:
path) and `memidx.py search` (kind: question) as subprocesses; never
imports memidx, never touches its behaviour. See bench/README.md "Runner
interface" for the CLI contract every runner in this directory implements,
and "The memcontinuum runner" for what this file does with the JSON it
gets back.

Hard constraints this file exists to satisfy without depending on the
caller's shell state:
  - PYTHONPATH is always cleared for the memidx.py subprocess (the
    machine's own ~/.bashrc poisons it with unrelated site-packages).
  - The private/live store is NEVER touched: this runner always passes an
    explicit --db, computed from the corpus path under the system temp
    directory by default, and never falls back to memidx's own
    $MEMCONTINUUM_HOME / ~/.memcontinuum default resolution.
  - A nonzero exit from memidx.py (a real error: missing/uninitialized
    index, a schema mismatch) is reported as a runner ERROR, never
    silently treated as "no results" -- score.py must not mistake a
    broken index for the nomemory floor.
  - Under --mode vector/hybrid, if memidx itself reports the embedding
    backend fell back to FTS-only ("embedding": "unavailable" or
    "fingerprint-mismatch" in the --json envelope), that is also an
    ERROR here: silently returning FTS results under a "vector" label
    would misrepresent the measurement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MEMIDX = REPO_ROOT / "memidx.py"


def default_db_path(corpus_dir: Path, project: str) -> Path:
    """A stable cache path outside the repo and outside any live
    MemContinuum store, keyed by the corpus's own resolved path so two
    different corpora (bench/corpus vs. a private one passed via --corpus)
    never share a database."""
    key = hashlib.sha256(str(corpus_dir.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "memcontinuum-bench-index" / f"{project}-{key}.sqlite"


def run_memidx(python: str, args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = ""  # hard rule: never inherit the machine's poisoned PYTHONPATH
    return subprocess.run(
        [python, str(MEMIDX), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def ensure_index(python: str, corpus_dir: Path, project: str, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    proc = run_memidx(python, ["reindex", "--root", str(corpus_dir), "--project", project, "--db", str(db_path)])
    if proc.returncode != 0:
        raise RuntimeError(f"reindex failed (rc={proc.returncode}): {proc.stderr.strip()}")


def _parse_json_envelope(stdout: str, mode: str | None) -> tuple[list, str | None]:
    """Returns (results_list, warning). Handles both the bare-list shape
    and the {"state"/"embedding": ..., "results": [...]} envelope shape
    cmd_search/cmd_for_path can emit. Raises RuntimeError if a vector/
    hybrid query silently fell back to FTS-only -- see module docstring."""
    data = json.loads(stdout) if stdout.strip() else []
    if isinstance(data, list):
        return data, None
    results = data.get("results", [])
    embedding = data.get("embedding")
    if embedding in ("unavailable", "fingerprint-mismatch") and mode in ("vector", "hybrid"):
        raise RuntimeError(
            f"mode={mode!r} but embeddings are {embedding!r} -- results would silently be "
            f"FTS-only; refusing to report them as {mode!r}"
        )
    warning = None
    if data.get("dimension_mismatch_rows"):
        warning = f"{data['dimension_mismatch_rows']} vector row(s) skipped (dimension mismatch)"
    return results, warning


def _dedupe_preserve_order(ids: list[str]) -> list[str]:
    seen = set()
    out = []
    for rid in ids:
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
    return out


def query_path(python: str, project: str, db_path: Path, file_path: str, limit: int) -> list[str]:
    proc = run_memidx(python, ["for-path", "--project", project, "--db", str(db_path), file_path, "--json"])
    if proc.returncode not in (0, 4):
        # rc 4 is for-path's own "no topics reference this path, and the
        # index itself is in a degraded state" fallback (still prints a
        # valid empty JSON envelope); anything else (1, 3, a crash) is a
        # real error, not a normal empty match.
        raise RuntimeError(f"for-path failed (rc={proc.returncode}): {proc.stderr.strip()}")
    results, _warning = _parse_json_envelope(proc.stdout, mode=None)
    flat: list[str] = []
    for entry in results:
        rid = entry.get("id")
        if rid:
            flat.append(rid)
        for nested in entry.get("governed_by") or []:
            nrid = nested.get("id")
            if nrid:
                flat.append(nrid)
    return _dedupe_preserve_order(flat)[:limit]


def query_question(python: str, project: str, db_path: Path, query: str, mode: str, limit: int) -> list[str]:
    proc = run_memidx(python, [
        "search", "--project", project, "--db", str(db_path), query,
        "--mode", mode, "--json", "--limit", str(limit),
    ])
    if proc.returncode != 0:
        raise RuntimeError(f"search failed (rc={proc.returncode}): {proc.stderr.strip()}")
    results, warning = _parse_json_envelope(proc.stdout, mode=mode)
    if warning:
        print(f"memcontinuum: {warning}", file=sys.stderr)
    ids = [entry.get("id") for entry in results if entry.get("id")]
    return _dedupe_preserve_order(ids)[:limit]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="memcontinuum")
    parser.add_argument("--corpus", required=True, help="path to a corpus root (e.g. bench/corpus)")
    parser.add_argument("--kind", required=True, choices=["path", "question"])
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mode", default="hybrid", choices=["fts", "vector", "hybrid"],
                         help="search mode for kind=question; ignored for kind=path (for-path has no mode)")
    parser.add_argument("--project", default="bench-corpus",
                         help="index project name; only distinguishes rows inside --db, not meaningful outside this runner")
    parser.add_argument("--db", default=None,
                         help="override the private index db path (default: a deterministic path "
                              "under the system temp dir, keyed by the corpus path -- never the live store)")
    parser.add_argument("--python", default=None,
                         help="python interpreter to run memidx.py under (default: $MEMCONTINUUM_PYTHON, else this interpreter)")
    args = parser.parse_args(argv)

    corpus_dir = Path(args.corpus)
    if not corpus_dir.is_dir():
        print(f"memcontinuum: {corpus_dir} is not a directory", file=sys.stderr)
        return 2

    python = args.python or os.environ.get("MEMCONTINUUM_PYTHON") or sys.executable
    db_path = Path(args.db) if args.db else default_db_path(corpus_dir, args.project)

    try:
        ensure_index(python, corpus_dir, args.project, db_path)
        if args.kind == "path":
            ids = query_path(python, args.project, db_path, args.query, args.limit)
        else:
            ids = query_question(python, args.project, db_path, args.query, args.mode, args.limit)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"memcontinuum: {exc}", file=sys.stderr)
        return 1

    for rid in ids:
        print(rid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
