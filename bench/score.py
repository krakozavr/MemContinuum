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
        prints ONLY the aggregate table plus the negative control's own
        aggregate numbers (fix round, M3: "every run" now means this path
        too) -- never a query's own text or the path substring it resolves
        against, so nothing private reaches report.md or any other tracked
        output even by accident. Skips cleanly, printing why, when
        fixtures/records/ is absent (a fresh clone, or any checkout other
        than the one this was authored on).
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

NULL_BASELINE_SCRIPT = RUNNERS_DIR / "nomemory.py"


def run_all(queries: list[dict], corpus: Path, runner_specs: list[str], limit: int) -> dict:
    """{display_name: {"per_query": {qid: {...metrics, "ranked": [...]}},
    "errors": {qid: "message"}, "is_canonical_null_baseline": bool}}

    `is_canonical_null_baseline` is set from the RESOLVED SCRIPT PATH, not
    from `display` -- fix round 2 (external re-gate, two independent
    reviewers): the old check matched `display`'s own basename against
    "nomemory", and `display` for a path spec is `script.stem`, which is
    whatever the caller named their file. An empty impostor script saved as
    `/anywhere/nomemory.py` produced `display == "nomemory"` and was
    exempted from the negative control; the identical file saved as
    `empty.py` correctly failed it. A user-controlled filename must never
    grant a free pass -- only actually BEING the repository's own
    `bench/runners/nomemory.py` (resolved, so a symlink to it still
    counts -- the symlink IS that file) does."""
    report = {}
    for spec in runner_specs:
        display, script, mode = resolve_runner(spec)
        is_canonical_null_baseline = script.resolve() == NULL_BASELINE_SCRIPT.resolve()
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
        report[display] = {
            "per_query": per_query,
            "errors": errors,
            "is_canonical_null_baseline": is_canonical_null_baseline,
        }
    return report


# --- negative control -------------------------------------------------------
#
# A benchmark can report a flattering number while measuring almost nothing:
# if the query set is so easy that a deliberately crippled runner scores the
# same as the real one, the metric is not separating anything and the headline
# figure is noise. The control's PRECISE, narrowed claim (see "Fix round 2"
# below for why it is narrowed): would this query set notice a runner that
# ignores the query text and, at most, branches on `kind` -- the one other
# channel a runner actually receives (see `run_query`'s `--kind` argument)?
# It does not, and cannot, promise to catch every conceivable way a runner
# could be blind to a query's actual content (see "What this does not claim"
# at the end of this comment).
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
# Fix round 2 (a SECOND external re-gate, two independent reviewers, same
# residual hole): the replacement above severed the QUERY-TO-RESULT
# association by rescoring query i's ranked output against query perm(i)'s
# expect, where perm was a SINGLE derangement of the WHOLE 57-query id list,
# mixing `path` and `question` ids freely. Both reviewers built a runner that
# never reads `--query` at all and branches only on `--kind` -- a fixed
# concept-id list for `path`, a fixed incident-id list otherwise -- and it
# PASSED (gain +0.08 to +0.12 depending on which reviewer's exact fixed
# lists). The reason: this corpus's `path` and `question` populations have
# systematically different expect distributions (path expects skew toward
# CON-* ids; some question expects are INC-* ids that essentially no path
# query ever expects), so a derangement that pairs a path query with a
# question query is comparing two different populations, not testing
# query-sensitivity -- exactly the kind of leak the ORIGINAL corpus-wide
# derangement was supposed to prevent, just moved one level up (from
# "ignores the query" to "ignores the query but reads `kind`").
#
# Two designs were evaluated for the fix, not just one:
#
# - KIND-PRESERVING DERANGEMENT (chosen): restrict the derangement so it
#   never pairs a query with one of a different `kind` -- one rotation per
#   kind group instead of one rotation over the whole list. Zero extra
#   runner invocations (still rescores the SAME already-computed `ranked`
#   output, just against a same-kind partner's expect). Verified (see
#   tests/test_bench.py and this round's fix report): both reviewers'
#   counterexample runners now score EXACTLY gain 0.0 -- not approximately,
#   the same algebraic-identity argument as the original design (below)
#   applies per kind group instead of over the whole list, since a
#   kind-branching runner's output is CONSTANT within each kind group.
#
# - RE-RUNNING each runner on deranged (kind, query-text) pairs instead of
#   relabeling the cached output (this round's brief, citing the second
#   reviewer): rejected. For a DETERMINISTIC runner (every runner this
#   benchmark measures; `run_query` passes no randomness), re-invoking the
#   runner on query perm(i)'s (kind, text) produces EXACTLY the same output
#   already cached as `ranked[perm(i)]` -- the runner cannot know it is
#   being fed a "deranged" query; feeding it query perm(i)'s own real input
#   is indistinguishable, to the runner, from having been asked query
#   perm(i) honestly. So the re-run design's "shuffled" score for slot i,
#   `score(expect_i, ranked[perm(i)])`, is the SAME family of quantity as
#   the label-shuffle design's `score(expect_perm(i), ranked_i)` -- literally
#   equal once you substitute j = perm(i), modulo using perm's inverse
#   instead of perm. It is the identical test at 2x the cost (a second full
#   subprocess pass per runner, doubling wall-clock time and doubling
#   exposure to timeouts/flakiness for anything that shells out, e.g. the
#   `memcontinuum` runners), and it does NOT close anything label-shuffle
#   cannot: empirically, re-running con_inc.py (this file's counterexample
#   fixture, kept as a test fixture below) against the OLD, non-kind-
#   preserving corpus-wide derangement gave gain +0.115, an even LARGER
#   pass margin than the label-shuffle version's +0.084 on the same
#   derangement -- re-running bought nothing because the leak was never
#   about staleness of the cached output, it was about the derangement
#   crossing kinds. Kind-preserving derangement closes the identified leak
#   at zero extra cost; re-running does not close it at all unless the
#   derangement is ALSO made kind-preserving, at which point re-running adds
#   cost without adding power over the cheaper design already chosen.
#
# Why kind-preserving derangement is immune to BOTH reviewers' counterexample
# (the general argument, restated per kind group): a runner whose output for
# a query of kind k depends only on k (not on the query's own text) returns
# the same ranked list R_k for every query of that kind, so its real score
# restricted to kind k is mean_{i in k} score(expect_i, R_k) and its
# shuffled score restricted to kind k is mean_{i in k} score(expect_perm(i),
# R_k). Because perm restricted to kind k is a bijection ON kind k's own id
# set (never leaves the group), {perm(i) : i in k} is the SAME SET as
# {i : i in k}, just relabeled -- summing score(expect_x, R_k) over that set
# gives the identical total either way, for EACH kind group independently.
# The overall gain, a weighted average of the two (now individually zero)
# per-kind gains, is therefore also exactly zero. This generalizes the
# original (pre-fix-round-2) proof, which only had one kind group to begin
# with in that argument's own terms -- restricting the derangement to be
# kind-preserving is what makes the "same set, just relabeled" step true
# when the corpus has more than one population with different expect
# distributions.
#
# Why this is still immune to the reversal-era counterexample (2): the
# control never reorders a result list, so it does not depend on that list
# having an order to destroy. A length-1 or match-set ranked output ["A"]
# scored against its own expect ["A"] (real MRR 1.0) and against some OTHER
# same-kind query's expect ["B"] (shuffled MRR 0.0, since "A" != "B") differ
# exactly as they should -- `path` queries participate in the control.
#
# What this does not claim: a runner that reads the query TEXT and branches
# on some OTHER discrete signal uncorrelated with `kind` (say, a hardcoded
# check for one specific substring, or a length threshold that does not
# track path-vs-question) is not provably caught by this design -- the
# provable-zero argument above only holds for a partition the derangement is
# built to preserve, and `kind` is the one channel this benchmark's own CLI
# contract (`run_query`) hands a runner SEPARATELY from the query text, which
# is why it is the one this control targets. This IS the narrowed claim the
# coordinator's decision rule for this round asked for if a broader one
# could not be delivered: this control catches a runner that is blind to
# query TEXT and keys on `kind` (or on anything else that happens to
# partition the query set exactly the way `kind` does) -- not "any runner
# that is blind to the query" in full generality. bench/README.md states
# this narrowed claim too.
#
# The derangement: one rotation per kind group (not one rotation over the
# whole list), each by an offset COPRIME with that group's own size --
# `_coprime_offset_near_half` -- rather than always using half the group's
# length. This matters for a reason fix round 2 also surfaced (a MAJOR
# finding on the calibration, not the derangement, but the same fix serves
# both): half of an EVEN group size is not coprime with it (half of 20 is
# 10, and gcd(10, 20) = 10, not 1), so a half-length rotation of an even
# group splits into gcd(offset, n) separate short cycles -- for `path`
# (n=20) that would be ten 2-cycles, each pairing query i with i+10 and
# i+10 back with i. A 2-cycle's two paired differences are exact NEGATIVES
# of each other (diff(i) = real_i - shuffled_i where shuffled_i uses i+10's
# expect, and diff(i+10) uses i's expect against i+10's own shuffled score --
# not independent observations by construction), which is exactly the kind
# of correlated-observation problem the calibration below must not silently
# assume away. Choosing the offset closest to half that is still coprime
# with the group's size guarantees a SINGLE cycle spanning the whole group
# (no fixed points, and no short cycles either), while staying close to the
# original "half length, not adjacent, spread the mismatch across the whole
# group" reasoning. For `question` (n=37, prime) every offset 1..36 is
# already coprime with 37, so the offset is unchanged from before (18, i.e.
# half of 37 rounded down). For `path` (n=20) the nearest coprime offset is
# 11. Under these offsets, exactly ONE of this corpus's same-expect pairs
# (see below) happens to coincide with the derangement: kw-12 <-> et-05,
# both expecting only TOP-113 -- correct behavior, not a loophole, since a
# runner cannot be faulted for not telling apart two queries whose correct
# answer is identical. (para-03 and path-08 also share an expect, TOP-109,
# but can never coincide under this derangement now that `path` and
# `question` never pair with each other -- resolved for free by kind
# preservation, not by choice of offset.)
#
# Calibration: for each query, take the PAIRED difference between its real
# score and its shuffled score (real_i - shuffled_i), then require the MEAN
# of those paired differences to exceed their own SAMPLE standard error
# (SAMPLE standard deviation of the per-query differences, not population --
# see below -- divided by sqrt(n)). Be precise about what this is NOT: it is
# not a calibrated significance test. A true null distribution would come
# from MANY independent derangements; this control uses exactly ONE fixed
# derangement (deliberately, for reproducibility -- no RNG, same numbers on
# every machine), so there is only one realization of "what would a
# query-blind runner's gain look like," not a sampling distribution of it.
# The n per-query differences from that one derangement are also not n
# independent draws in the rigorous sense -- they all come from the SAME
# permutation. This is a MINIMUM-EFFECT FLOOR, not a p-value: "the observed
# gain must clear the spread already visible in this one derangement's own
# paired differences," nothing more. (Fix round 2 considered replacing this
# with a real permutation test over many kind-preserving derangements
# instead -- cheap, since it only re-scores the already-cached `ranked`
# output rather than re-running any runner. It was NOT adopted: built and
# checked against the one-hit-runner case below, it does not actually fail
# that case either -- when another query in the same kind group happens to
# share the one-hit runner's lone correct expect (this corpus has such
# pairs, e.g. path-01/path-20), a permutation test correctly and honestly
# reports that single hit as distinguishable from chance, because under
# that specific null model it genuinely is: no relabeling among that
# derangement family ever reproduces it by luck. The one-hit-runner
# complaint below is a SMALL-SAMPLE EFFECT-SIZE problem, not a defect in
# how the null is built -- a fancier null does not fix it, so the simpler,
# cheaper, already-narrowly-scoped calibration was kept instead of adding
# permutation-test machinery that would not have changed this outcome.)
#
# Fix round 2 also corrected an arithmetic error in the calibration itself
# (MAJOR, both reviewers): the previous code used `statistics.pstdev`
# (population standard deviation) over the per-query differences. The n
# differences are a SAMPLE of the underlying variability, not the full
# population of it, so the textbook-correct estimator is the SAMPLE standard
# deviation (`statistics.stdev`, Bessel-corrected, dividing by n-1 not n).
# Using population SD instead of sample SD is not a stylistic choice -- it
# is why a runner correct on exactly ONE query and empty on every other one
# used to ALWAYS pass, for any n: for a single 1.0 among (n-1) zeros, the
# mean is 1/n and population-SD-based SE works out to sqrt(n-1)/n**1.5,
# and mean > population_se reduces algebraically to sqrt(n) > sqrt(n-1),
# true for every n -- a guaranteed pass, not a coincidence, and not a
# statistical property of the runner at all. The sample-SD version makes
# the identical one-hit case an EXACT ALGEBRAIC TIE (mean == sample SE ==
# 1/n) for any n, so `gain > se` (a strict inequality) correctly reports it
# as NOT separating, in exact arithmetic. This still does not mean every
# one-hit-shaped runner fails in practice (see the permutation-test
# discussion above: a real single correct answer nobody else could
# reproduce by chance IS informative, and floating-point rounding on a
# specific corpus can tip an exact tie either way) -- it means the
# calibration no longer manufactures a GUARANTEED pass via the wrong
# formula. The printed control line and the `--json` output both show the
# `se` value the decision actually uses (Grok's finding 2, this round: the
# previous printout showed `spread` -- the raw sample SD of the per-query
# differences -- labeled in a way a reader could mistake for the quantity
# the pass/fail line compares against, when the decision actually divides
# that by sqrt(n) first).
#
# The multiplier on standard error is still 1x, not 2x or another value
# chosen for a stricter confidence level: this control's job is a FLOOR
# (reject a runner statistically indistinguishable from query-blind), not a
# significance certificate, and the printed gain/se numbers are exactly what
# a reader needs to judge the margin for themselves.
def _coprime_offset_near_half(n: int) -> int:
    """The offset closest to n // 2 (ties broken toward the LARGER offset)
    that is coprime with n, for 1 <= offset <= n - 1. A rotation by this
    offset is always a single n-cycle (no fixed point, and -- unlike a
    non-coprime offset -- no short cycles either): see the module comment
    above `_coprime_offset_near_half`'s callers for why a short cycle (in
    particular a 2-cycle) breaks the calibration's per-query-difference
    accounting. n must be >= 2 (every integer >= 2 has at least one such
    offset: 1 is always coprime with n)."""
    if n < 2:
        raise ValueError(f"_coprime_offset_near_half needs n >= 2, got {n}")
    half = n // 2
    for delta in range(n):
        hi = half + delta
        if 1 <= hi <= n - 1 and math.gcd(hi, n) == 1:
            return hi
        lo = half - delta
        if delta and 1 <= lo <= n - 1 and math.gcd(lo, n) == 1:
            return lo
    raise AssertionError(f"unreachable: no offset coprime with {n} in [1, {n - 1}]")  # pragma: no cover


def _kind_preserving_derangement(queries: list[dict]) -> dict[str, str]:
    """A fixed-point-free permutation of the query id list that NEVER pairs
    two queries of different `kind` -- one single-cycle rotation per kind
    group (see `_coprime_offset_near_half`), as {id: paired_id}. See the
    module comment above `negative_control` for why crossing `kind` in the
    derangement is exactly the leak fix round 2 closed."""
    by_kind: dict[str, list[str]] = {}
    for q in queries:
        by_kind.setdefault(q["kind"], []).append(q["id"])
    perm: dict[str, str] = {}
    for kind, ids in by_kind.items():
        n = len(ids)
        if n < 2:
            raise ValueError(
                f"negative_control needs at least 2 queries of kind {kind!r} for a "
                f"kind-preserving derangement, got {n}"
            )
        offset = _coprime_offset_near_half(n)
        for i in range(n):
            perm[ids[i]] = ids[(i + offset) % n]
    return perm


def negative_control(report: dict, queries: list[dict]) -> dict:
    """{runner: {"real_mrr", "shuffled_mrr", "gain", "spread", "se",
    "returns_nothing", "is_exempt_baseline", "separates"}} plus "verdict":
    "ok" | "inconclusive", naming the runners that failed. See the module
    comment above for the derangement and the calibrated threshold; see
    `run_all`'s `is_canonical_null_baseline` for the one exemption, bound to
    the resolved runner script path, never to `display`."""
    expect_by_id = {q["id"]: q["expect"] for q in queries}
    perm = _kind_preserving_derangement(queries)

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
        # Sample standard deviation (Bessel-corrected), not population: see
        # the module comment -- these n differences are a sample, and using
        # population SD is what let a one-hit runner always pass.
        spread = statistics.stdev(diffs) if len(diffs) > 1 else 0.0
        se = spread / math.sqrt(len(diffs)) if diffs else 0.0

        returns_nothing = bool(pq) and all(not (m.get("ranked") or []) for m in pq.values())
        full_coverage = len(pq) == len(queries)
        is_exempt_baseline = (
            data.get("is_canonical_null_baseline", False) and returns_nothing and not errors and full_coverage
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
            "se": round(se, 4),
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
    print("negative control (query/result association broken by a deterministic, "
          "kind-preserving derangement of the query list -- see bench/score.py's "
          "negative_control comment; a MINIMUM-EFFECT FLOOR, not a calibrated "
          "significance test: a runner must beat its own query-shuffled score by "
          "more than that shuffled score's sample standard error, printed below "
          "as `se`, to count as separating):")
    for display in runner_names:
        if display not in control:
            continue
        c = control[display]
        note = " (returns nothing by design)" if c["is_exempt_baseline"] else ""
        flag = "ok " if c["separates"] else "FLAT"
        print(f"  {flag}  {display:22s} real {c['real_mrr']:.2f}  "
              f"shuffled {c['shuffled_mrr']:.2f}  gain {c['gain']:+.2f}  "
              f"(need > se {c['se']:.4f}, spread {c['spread']:.2f}){note}")
    if control["verdict"] == "inconclusive":
        print()
        print("INCONCLUSIVE: " + ", ".join(control["failed_runners"])
              + " did not beat their own query-shuffled score by more than that "
                "score's sample standard error. The query set does not separate a "
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
