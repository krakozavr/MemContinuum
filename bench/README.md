# MemContinuum public retrieval benchmark

The project has over a thousand unit tests and, until this, no measurement of
whether retrieval returns the RIGHT record. This is that measurement: a
public corpus, a public query set with a justified answer key, three
runners, and a scorer. Everything here is synthetic and everything here is
disputable — that is the point. Publish the method before the number.

## What is measured

Two things a coding agent's decision memory actually has to do, matching
the two commands `memidx.py` exposes for them:

- **`kind: path`** — given a file path about to be edited, which records
  should be injected? This is `memidx.py for-path`, the pre-edit hook's own
  query.
- **`kind: question`** — given a natural-language question, which records
  answer it? This is `memidx.py search`.

For each query we compute, per runner:

- **Recall@k** (k = 1, 3, 10) — `|top-k ranked ids ∩ expect| / |expect|`.
  This is the textbook definition, not "hit@k": most queries here have a
  single correct id, where the two are identical, but several `path`
  queries legitimately have two to four correct ids (see "The path-kind
  answer key" below), and Recall@k gives partial credit for finding some
  of them. **This means a system that returns every correct id, in any
  order, cannot reach Recall@1 = 1.0 on a query with more than one
  expected id** — only one id can occupy rank 1. Read `path` kind's Recall@1
  and Recall@3 rows next to Recall@10: on this corpus, all three
  `memcontinuum` modes reach Recall@10 = 1.00 on `path` queries, meaning
  `for-path` returns **exactly** the expected set, no more and no less, on
  every one of the 20 `path` queries — the sub-1.0 Recall@1/Recall@3 numbers
  are the metric's arithmetic on multi-id queries, not a retrieval miss.
  `path` kind is a correctness gate, not a ranking contest: `for-path` has
  no relevance score, only a match list, so its own emission order (not a
  ranking) is what Recall@1/MRR are computed against.
- **MRR** — `1 / rank of the first expected id` anywhere in the runner's
  output (not capped at any k). Zero if no expected id was returned at all.

`bench/score.py` reports these per `kind` (`path`, `question`), three
disjoint sub-slices of `question` by id prefix — `paraphrase` (`para-`, see
below), `exact-term` (`et-`, an error message/file name/symbol/flag/quoted
phrase a keyword search should nail — see "The exact-term queries" below),
`plain` (`kw-`, ordinary keyword-shaped developer questions, see "The
paraphrase queries" below) — and `overall`. `overall` and `question` are
each the union of every query of that shape, `exact-term` included: they
moved slightly easier when the 12 `et-` queries were added (fix round: was
45 queries with no `et-` slice; the exact-term R@1/MRR are near-ceiling by
construction, since that is the point of the slice), which is exactly why
the sliced numbers exist — read `paraphrase` as the hard number and
`overall`/`question` as a blend, not the other way around.

## How to run it

```
export MEMCONTINUUM_PYTHON=/path/to/a/venv/python   # needs fastembed + PyYAML
PYTHONPATH= "$MEMCONTINUUM_PYTHON" bench/score.py
```

`score.py` itself and every runner run under whatever Python invoked
`score.py` (no fastembed/PyYAML needed for `score.py`, `nomemory`, or
`keyword` — only the `memcontinuum` runner's own subprocess call into
`memidx.py` needs `$MEMCONTINUUM_PYTHON`, and it reads that variable
itself). `--json` gets the same result as machine-readable JSON instead of
a table. `--runner NAME[:mode]` (repeatable) selects a subset; see "Runner
interface" below for what `NAME` can be. Everything runs strictly
sequentially — several `memcontinuum:*` runner invocations share one
on-disk SQLite index (see below), and concurrent writers to one SQLite file
is not a scenario this harness needs to solve.

## How the answer key was built, and by whom

One agent session (Claude, this repository's usual working model) wrote
the corpus, the code tree it binds to, every query, and every `notes`
justification, in one pass, cross-checking every `path`-kind answer against
a live `for-path --json` run on the built corpus before committing to it
(see "The path-kind answer key" below) and mechanically verifying every
paraphrase query's word-level independence from its target before
committing to it (see "The paraphrase queries" below). No second, human or
independent-agent review of the answer key has happened yet. That is a real
limitation, not a formality — see "Honest limitations."

### The corpus

`bench/corpus/` is a synthetic decision store — 22 topics, 6 incidents, 4
concepts (32 records) — describing an invented file-sync client project
("Driftwood") that exists only for this benchmark. `bench/codebase/` is a
small, syntactically real (if stubbed-out) Python tree the corpus's
`code_refs`/`implemented_by`/`tested_by` fields bind to, so `path`-kind
queries have real file paths to ask about. No real project's content
appears anywhere in either.

The corpus was built for **ranking difficulty on purpose**, not coverage
for its own sake:

- **Look-alike pairs that share vocabulary but are different records.**
  Upload retry backoff (`TOP-110`) and merge-queue retry backoff
  (`TOP-109`) both back off exponentially and both say so in almost the
  same words; bandwidth throttle (`TOP-111`) and the separate mobile data
  cap (`TOP-112`) both throttle transfers; conflict detection (`TOP-108`,
  vector clocks) and conflict resolution (`TOP-107`, keep-both-copies) are
  two halves of the same feature that a system must not conflate; token
  refresh timing (`TOP-115`) and token storage (`TOP-116`) are two separate
  rulings about the same file.
- **Superseded chains** (5 of the 22 topics): content hash SHA-256→BLAKE3
  (`TOP-102`), chunk boundaries fixed-size→content-defined (`TOP-103`),
  conflict policy last-write-wins→keep-both (`TOP-107`), token storage
  plaintext-file→OS-keychain (`TOP-116`), config format YAML→TOML
  (`TOP-119`). Every current link's own rationale names what it replaced by
  name (e.g. TOP-102's current link says "replacing the SHA-256 hash"), on
  purpose — `memidx.py search`'s default `--status` filter hides a
  superseded link's own row, so a query about the *old* choice is only
  answerable at all through what the *current* link says about it. A query
  that needed `--status any` to be answerable would be testing
  `memidx.py`'s CLI, not the corpus, so none of the 57 queries need it.
- **Concept boundaries that pull in topics no direct `code_refs` would
  find.** `CON-303` ("Upload Pipeline") is governed by both `TOP-110`
  (retry, this file's own `code_refs`) and `TOP-113` (streaming, a
  *different* file's `code_refs`) — editing `pipeline.py` should surface
  both, and it does, through the concept, not through any `code_refs`
  match on `pipeline.py` itself. Four of the twenty `path` queries exercise
  this; `path-13` is the widest case (one file, four expected ids).
- **A test file with no topic `code_refs` at all** (`path-20`,
  `tests/test_dedup.py`): it matches only through a concept's `tested_by`
  entry. Without concept-level path matching, editing a test file would
  surface nothing.
- Every incident cites the topic its fix lives in **without duplicating
  that topic's own wording**, so an incident and its topic are genuinely
  different retrieval targets, not two copies of the same text.

### The path-kind answer key

`for-path` returns a **match set**, not a ranked list: matched topics
(`code_refs` match), then matched concepts (`implemented_by`/`tested_by`
match), and — nested *inside* each matched concept's own JSON, not as
separate top-level entries — that concept's `governed_by` topic chains.
Each `expect` list in `bench/queries.jsonl` for a `path` query is the
**flattened, deduplicated union** of every record id anywhere in that
structure: direct topic-id matches, concept-id matches, and each matched
concept's nested `governed_by` topic ids — because a governed topic's full
ruling text genuinely gets injected via `for-path --with-chain-text` even
though it is not its own top-level JSON entry. The `memcontinuum` runner
(`bench/runners/memcontinuum.py`) walks the same structure the same way.

Every one of the 20 `path` queries' `expect` lists was cross-checked
against a live `for-path --json` run on the built corpus before being
committed to `bench/queries.jsonl` — but that is a cross-check, not the
definition of correctness, and a live run can never catch a mistake in the
corpus itself (an `id` typo'd into the wrong `governed_by` list, say).
`tests/test_bench.py`'s `TestPathOracle` is the independent check: it
recomputes each `path` query's expected set straight from the corpus's own
frontmatter (`code_refs` segment-aware prefix/glob matching, `concept
implemented_by`/`tested_by`, `concept governed_by`) using memidx's own
`code_ref_matches` — the one shared primitive both `for-path` and `drift`
already trust — and asserts it equals the hand-authored `expect` list. That
catches a key/corpus mismatch on every future edit to either, without
running `memidx.py` at all.

### The paraphrase queries

11 of the 37 `question` queries (id prefix `para-`, exceeding the 10
required) share **zero word-level vocabulary** with their target record's
own indexed text: lowercase, `[a-z0-9]+`-tokenized, common-English-stopword
-removed, no stemming, checked against the target's title + body + (for a
topic) its *current* link's `ruling.text` + `rationale.text` — exactly the
text `memidx.py reindex` actually puts into FTS5 and the embedding input
(a superseded link's own text is a separate, unindexed-by-default row; see
above). This was verified mechanically against the real corpus files
before being written into `bench/queries.jsonl`, and
`tests/test_bench.py`'s `TestParaphraseIndependence` re-verifies it on
every run, so a later corpus edit that accidentally reintroduces a shared
word fails the test suite instead of silently rotting the benchmark's own
central claim.

Two paraphrase pairs are deliberately adversarial: `para-01`
(`TOP-107`, resolution) vs. `para-02` (`TOP-108`, detection) rephrase two
tightly related topics with no shared vocabulary *between the two
paraphrases either*, so a system cannot get both right by accident through
one lucky shared word; `para-08` (`TOP-118`, the general rename rule) and
`para-11` (`INC-204`, the one incident where that rule's known limitation
actually fired) probe whether a system distinguishes "what's the rule" from
"what went wrong once."

The 14 `kw-` queries are ordinary keyword-shaped developer questions
(real, expected vocabulary overlap with the target) — the case a plain
keyword search is *supposed* to do well on, kept in the same file so the
paraphrase slice's difficulty is visible by contrast, not assumed.

### The exact-term queries

The 12 `et-` queries each quote (or near-quote) a specific error
message/file name/symbol/flag/figure/proper noun that appears verbatim in
exactly one record's own indexed text (`et-05`'s `256MB`, `et-08`'s
`Credential Manager`, `et-11`'s near-verbatim quote of an incident's own
title — see each query's own `notes` for its literal phrase and source
line). This is the case a keyword search is expected to nail outright
(`tests/test_bench.py`'s corpus-lint and expect-id checks cover this
slice like any other, and a floor on its own count keeps it from silently
shrinking away, but there is no dedicated mechanical check that a claimed
exact phrase is actually present in the target's text — verified by hand,
once, against the corpus, when each query was written; see "Honest
limitations"). Folded into `overall`/`question` for backward
compatibility with the pre-`et-` query set (see "What is measured" above)
— read the `exact-term` row in isolation to compare it against
`paraphrase`, not against `overall`.

### `notes`

Every one of the 57 queries' `notes` field states which record(s) are
expected and why, in terms of what is actually in the corpus (a
`code_refs` entry, a concept's `governed_by`, shared or absent vocabulary)
— never "because the tool returns this," which would make the key a
description of current behavior rather than a judgment about correct
behavior.

## Runner interface

Every script in `bench/runners/` is invoked the same way and must behave
the same way, so a new one can be dropped in without reading any other
runner's code:

```
<runner> --corpus DIR --kind {path,question} --query STRING [--limit N] [--mode {fts,vector,hybrid}]
```

- `--corpus` — a corpus root shaped like `bench/corpus/` (topics/incidents/
  concepts as markdown under it, walked recursively).
- `--kind` / `--query` — one query. `path`: `--query` is a file path
  (relative, matching how `code_refs` are authored). `question`:
  `--query` is natural-language text.
- `--limit` — how many ranked ids to return at most. A runner with no
  natural notion of a limit (`nomemory`) accepts and ignores it.
- `--mode` — accepted by every runner for interface uniformity; only
  `memcontinuum` uses it (`for-path` has no mode of its own, so it is
  ignored for `--kind path` even by `memcontinuum`).
- **stdout**: the ranked record ids, most relevant first, **one per
  line**, nothing else. Empty stdout is a valid answer ("no results").
- **exit code**: `0` on success, including a genuine "no results" — a
  runner must never map an internal failure to empty output. Nonzero means
  the runner itself failed; `score.py` reports this as an error for that
  query, distinct from an empty (but successful) result, and excludes it
  from that runner's own Recall/MRR averages rather than silently scoring
  it as zero.

To add a runner for another system: write one script obeying the contract
above, drop it anywhere, and run `score.py --runner /path/to/it.py` (or
`--runner name` if it lives in `bench/runners/name.py`). Nothing else in
this directory needs to change.

### `nomemory`

Always empty output, rc 0. The floor every real system must clear.

### `keyword`

Plain TF-IDF term-overlap ranking (`score = Σ tf(t,record)·idf(t)` over
shared terms; see the file's own docstring for the exact formula and
tokenizer) over the **entire raw markdown file** — frontmatter (including
`code_refs:` paths and `id:`) and body together — for every record under
`--corpus`. Zero third-party dependencies on purpose (stdlib `re` and
`math` only): this is meant to be the thing anyone could have written
without installing anything, and it is why `keyword` is *not* blind to
`path`-kind queries — a `code_refs:` line is literal text in the file, so
ordinary term overlap can still find a directory-prefix or filename match.
What it structurally cannot do is `code_ref_matches`'s segment-aware
prefix/glob semantics (a directory ref like `src/storage/dedup/` matching
every file under it, never a same-prefix sibling like
`src/storage/dedup2/`), or tell a real concept boundary from an accidental
word overlap. This is the baseline that matters: if `memcontinuum` cannot
beat it, nothing else about the comparison is interesting.

### `memcontinuum`

Wraps `memidx.py for-path` (`kind: path`) and `memidx.py search`
(`kind: question`) as subprocesses — it never imports `memidx`, and it
changes nothing about how either command behaves. It builds and reuses its
own private SQLite index (`memidx.py reindex`) at a path derived from the
corpus's own resolved path under the system temp directory, **never**
`$MEMCONTINUUM_HOME` or `~/.memcontinuum` regardless of what the calling
shell has exported (`--db` overrides this explicitly if a caller wants a
fixed path). `PYTHONPATH` is cleared for every `memidx.py` subprocess call
it makes.

This system-temp cache is deliberate residue, not an oversight (Codex 8):
its whole point is to survive between runs (`bench/runners/memcontinuum.py`
reuses it instead of reindexing from scratch on every single-query
subprocess call this file makes), and its path is keyed off a hash of the
corpus's own resolved path, so a different `--corpus` never collides with
it. It lives under `tempfile.gettempdir()`, never under `MEMCONTINUUM_HOME`
or any real store, and it holds nothing but a rebuild of this file's own
public, synthetic corpus — safe to delete by hand at any time; the next
run just rebuilds it.

For `kind: path`, it flattens `for-path --json`'s match structure exactly
as described in "The path-kind answer key" above: top-level topic/concept
ids plus, for each matched concept, its nested `governed_by` topic ids,
deduplicated, in emission order (**not** a relevance ranking — see the
Recall@k note at the top of this file). For `kind: question`, it passes
`--mode` straight through to `search --json` and reads off each result's
`id`, deduplicated in score order. If `search --json`'s envelope reports
`"embedding": "unavailable"` or `"fingerprint-mismatch"` under
`--mode vector`/`--mode hybrid` (the embedding backend silently fell back
to FTS-only), the runner treats this as an **error**, not a result — it
would otherwise silently report FTS numbers under a "vector" label. Any
other nonzero exit from `memidx.py` (a genuinely broken/missing index) is
likewise an error, never reinterpreted as "no results."

## Verify

```
PYTHONPATH= "$MEMCONTINUUM_PYTHON" memlint.py bench/corpus                            # 0 errors, 0 warnings
PYTHONPATH= "$MEMCONTINUUM_PYTHON" memlint.py --code-root bench/codebase bench/corpus  # same, with the code tree bound
PYTHONPATH= "$MEMCONTINUUM_PYTHON" bench/score.py                                     # end to end, all five runners
PYTHONPATH= "$MEMCONTINUUM_PYTHON" python -m unittest tests.test_bench -v
```

## Honest limitations

- **The corpus is synthetic.** "Driftwood" does not exist. A real
  project's decisions are messier — inconsistent authoring, real
  disagreement between rulings, records nobody had time to write well.
  This corpus is clean by construction, which almost certainly makes every
  number here an upper bound, not a realistic estimate.
- **The answer key is one agent's judgment**, cross-checked mechanically
  against the corpus (the `for-path` cross-check, the paraphrase
  independence check) but not against a second reviewer's independent
  read. A key that is wrong in a way its own author cannot see is not
  caught by that author re-checking their own work.
- **32 records and 57 queries is small.** Recall@k on a corpus this size
  moves by more than one query's worth of luck; treat single-decimal
  differences between runners as noise and multi-decimal differences (the
  paraphrase-slice gap between `keyword` and `memcontinuum:vector`, for
  instance) as the signal worth trusting.
- **The `keyword` baseline's TF-IDF has no document-length normalization,
  so a paraphrase query can be vocabulary-independent by this file's own
  definition (zero *content* words shared, stopwords removed) and still
  rank the target first for `keyword`.** Found during the fix round that
  closed Grok 8: after removing every leaked content word from `para-01`,
  `keyword` still ranked its target (`TOP-107`) first, dominated by raw
  counts of "the"/"a"/"is"/"and" and similar words this file's own
  independence check deliberately excludes (that is what "stopword" means
  here) but `bench/runners/keyword_baseline.py`'s scorer does not — a
  longer or more repetitively-worded record accumulates more of these
  regardless of query content, with no length normalization (BM25-style or
  cosine) to correct for it. `para-06` (a similarly two-link topic,
  `TOP-116`) does NOT show the same effect, so this is not simply "every
  long record wins" — it was not chased further than the one probe that
  found it. Not fixed here: changing `keyword_baseline.py`'s scoring
  formula moves every keyword number in every slice this file and both
  external gates already cite, which is a design decision for its own
  review, not a drive-by edit inside a query-wording fix.
- **The exact-term slice's literal phrases are hand-verified, not
  mechanically checked.** `tests/test_bench.py` floors the slice's own
  count (Grok 13) but does not assert that each query's claimed quoted
  phrase actually appears in its target's text — that check was done once,
  by hand, against the corpus, when each `et-` query was written (see "The
  exact-term queries" above), and could in principle rot silently on a
  future corpus edit.
- **A system we did not run is named as not run, never compared from its
  documentation.** No hosted or third-party retrieval system has a runner
  here yet; adding one is exactly the "drop in a script" path described
  above, and until that happens no claim is made about how any such system
  would score.
- **The private gate (`score.py --private`) is closed by construction** —
  it needs `fixtures/records/`, which is not published and never will be
  (it paraphrases a real project's incident history). It exists so the
  same method can be pointed at real data without that data becoming
  public, not so a reader can reproduce its numbers.
- **`docs/internal/gold-probes.tsv` is out of scope for this harness.**
  Its probes are qualified code-symbol names checked against the *code*
  index (`memidx.py code-search`/`why`), a different retrieval question
  ("which function implements X") than the decision-memory retrieval this
  harness measures ("which ruling governs X"). Nothing here runs against
  it, and nothing here claims to.

## The negative control

A benchmark can report a flattering number while separating nothing. If the
query set is easy enough that a runner ignoring the query entirely scores as
well as the real one, the metric is measuring the corpus rather than the
retrieval, and the headline figure is noise.

**Fix-round history.** The first version of this control reversed each
runner's own ranked output and rescored it against the SAME query's expect.
Two external reviews (Codex, Grok) independently proved that tests ranking
*order*, not query-sensitivity: a query-blind runner that returns the
identical list for every query passed (reversing a fixed list can still look
query-sensitive if the corpus rewards that fixed order on average), and
reversing a length-1 list or a match-set whose order is not a ranking at all
(this file's own `path` kind) is a no-op — MRR cannot change, so all 20 `path`
queries were structurally invisible to it regardless of the runner.

**What it does now.** Every run also scores each runner against a
**query-shuffled** twin: each query's already-computed ranked output is
rescored against a *different* query's expected answer, the pairing fixed by
a deterministic derangement (no fixed point) of the query id list — a
half-length rotation, no random seed, reproducible everywhere (see
`bench/score.py`'s `negative_control` for the full derivation, including an
algebraic proof that a query-blind runner's gain is EXACTLY zero under this
design, and why a length-1/match-set result now participates). The threshold
is calibrated from the run's own data rather than a picked constant: the mean
paired difference between each query's real and shuffled score must exceed
that difference's own standard error across the query set.

A runner that returns nothing is excluded from the verdict, rather than
counted as a failure, **only** when it is explicitly declared the null
baseline (`nomemory`) AND it has zero errors AND it covered every query —
anything else that returns nothing, or has any error at all, makes that
runner's own result inconclusive rather than excused.

If any runner fails, the harness prints **INCONCLUSIVE**, names the runners,
and says the numbers say nothing about retrieval quality — and now (fix
round) `score.py`'s own exit code is nonzero on that verdict too (was:
always `0`, silently unnoticed by any caller checking only the exit code).
That verdict is in `--json` too, under `negative_control`. A published number
without an `ok` verdict beside it should not be believed. `score.py
--private` computes and prints this control too, on nothing but the
aggregate numbers already safe to print there — the same "every run" promise
this section makes, minus a query count under 2 (a derangement needs at
least two queries), which it states and skips cleanly instead of crashing.

Idea taken from klypix-mcp, whose benchmark runs unlocked writers as a negative
control and declares itself inconclusive if they lose nothing.
