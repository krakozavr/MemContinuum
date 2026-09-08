#!/usr/bin/env python
"""keyword -- plain ranked keyword search over the corpus markdown.

The baseline that matters: a coding agent with nothing but `grep`-shaped
term matching, no decision-memory engine, no embeddings. If memcontinuum
cannot beat this, nothing else about it is interesting. See
bench/README.md "Runner interface" for the CLI contract every runner in
this directory implements, and "The keyword baseline" for exactly what it
does and does not see.

Zero third-party dependencies on purpose (stdlib only) -- this is meant to
be the thing anyone could have written without installing anything.

Algorithm: classic TF-IDF, cosine-free (a plain dot product is enough
since only relative ranking matters, and both vectors are dominated by a
handful of nonzero terms on a corpus this size):

    score(query, record) = sum over shared terms t of
        tf(t, record) * idf(t)

    tf(t, record)  = raw count of t in record's text
    idf(t)         = ln((N + 1) / (df(t) + 1)) + 1      (smoothed)

Tokenization: lowercase, `[a-z0-9]+` runs (roughly SQLite FTS5's own
unicode61 tokenizer for ASCII text) -- no stemming, no stopword removal.
Not removing stopwords is deliberate: a real "grep-shaped" baseline
doesn't know which words are noise either, and it costs this baseline
nothing on this corpus (idf already down-weights common terms).

The search text for one record is the ENTIRE raw markdown file --
frontmatter (including `code_refs:` paths, ids, ruling/rationale prose)
and body together, exactly as `plain ranked keyword search over the same
markdown` in the project brief describes it. This is why the keyword
baseline is NOT blind to `kind: path` queries: a topic's own `code_refs`
line is literal text in the file, so a query that is itself a file path
can still score a directory-prefix or filename match through ordinary
term overlap -- it just cannot express fnmatch-glob semantics the way
memidx's own `code_ref_matches` does, which is exactly the gap this
baseline is meant to expose.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_ID_RE = re.compile(r"^id:\s*(\S+)\s*$", re.MULTILINE)


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def record_id_for(path: Path, text: str) -> str:
    """The frontmatter `id:` field, or the file stem when absent -- the
    same fallback memlint/memidx use (a record with no explicit id is
    looked up by its filename stem)."""
    m = _ID_RE.search(text)
    if m:
        return m.group(1)
    return path.stem


def load_corpus(corpus_dir: Path) -> list[tuple[str, str]]:
    """[(record_id, full_raw_text), ...] for every markdown file under
    corpus_dir, sorted by path for determinism."""
    out = []
    for f in sorted(corpus_dir.rglob("*.md")):
        text = f.read_text(encoding="utf-8", errors="ignore")
        out.append((record_id_for(f, text), text))
    return out


def build_index(records: list[tuple[str, str]]):
    """(term_freqs: [{term: count}, ...], idf: {term: float}) aligned to
    `records`' own order."""
    term_freqs = []
    df: dict[str, int] = {}
    for _rid, text in records:
        counts: dict[str, int] = {}
        for tok in tokenize(text):
            counts[tok] = counts.get(tok, 0) + 1
        term_freqs.append(counts)
        for tok in counts:
            df[tok] = df.get(tok, 0) + 1
    n = len(records)
    idf = {t: math.log((n + 1) / (d + 1)) + 1.0 for t, d in df.items()}
    return term_freqs, idf


def rank(query: str, records: list[tuple[str, str]], term_freqs, idf, limit: int) -> list[str]:
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    scores: list[tuple[float, str]] = []
    for (rid, _text), counts in zip(records, term_freqs):
        score = 0.0
        for tok in q_tokens:
            tf = counts.get(tok)
            if tf:
                score += tf * idf.get(tok, 0.0)
        if score > 0.0:
            scores.append((score, rid))
    # Highest score first; ties broken by record id ascending, so output
    # is fully deterministic regardless of corpus walk order or dict
    # iteration order.
    scores.sort(key=lambda pair: (-pair[0], pair[1]))
    seen = set()
    out = []
    for _score, rid in scores:
        if rid in seen:
            continue
        seen.add(rid)
        out.append(rid)
        if len(out) >= limit:
            break
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="keyword")
    parser.add_argument("--corpus", required=True, help="path to a corpus root (e.g. bench/corpus)")
    parser.add_argument("--kind", required=True, choices=["path", "question"], help="unused; the same ranking runs for both kinds")
    parser.add_argument("--query", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mode", default=None, help="unused; accepted for interface parity")
    args = parser.parse_args(argv)

    corpus_dir = Path(args.corpus)
    if not corpus_dir.is_dir():
        print(f"keyword: {corpus_dir} is not a directory", file=sys.stderr)
        return 2

    records = load_corpus(corpus_dir)
    if not records:
        return 0
    term_freqs, idf = build_index(records)
    for rid in rank(args.query, records, term_freqs, idf, args.limit):
        print(rid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
