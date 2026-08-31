#!/usr/bin/env bash
# codanna-bench.sh -- compares Anatomy's code-reindex/code-search (memidx.py)
# against Codanna (https://github.com/bartolli/codanna) on the same probe
# protocol over a real code corpus of your choosing.
#
# Standalone: memidx.py never imports or references this script (or
# Codanna) -- this script calls memidx.py's CLI as a subprocess, the same
# way any other caller would.
#
# Codanna is sandbox-installed under ~/.cache/codanna-bench (a release
# binary preferred, falling back to `cargo install` if a binary isn't
# available for this platform) and left there for reuse on the next run.
# If install fails outright (no network, no matching release asset, no
# cargo), this script prints a message and SKIPS the Codanna columns --
# it still runs our own fts/vector/hybrid probes and prints the comparison
# table with Codanna's columns marked SKIPPED, rather than producing no
# output at all.
#
# Usage: scripts/codanna-bench.sh [SOURCES_ROOT]
#   SOURCES_ROOT, or $MEMCONTINUUM_BENCH_CORPUS, or the `#corpus:` line of
#   the probe file. The probe set itself is read from
#   $MEMCONTINUUM_BENCH_PROBES (default docs/internal/gold-probes.tsv) --
#   untracked, because probe queries and expected qualified names describe
#   a real private codebase (privacy requirement, see
#   tests/test_repo_init.py TestNoMachineIdentifyingContent).
#
# Every python invocation clears PYTHONPATH -- a Windows numpy install
# leaks onto it by default in this shell and breaks fastembed under Linux
# Python otherwise (AttributeError: module 'os' has no attribute
# 'add_dll_directory').

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEMIDX="$REPO_ROOT/memidx.py"
# Python resolution order (README.md "Requirements" / hooks/memlib.sh): env
# override -> <engine>/.venv/bin/python -> error naming the fix. No machine
# path is ever hardcoded in tracked content (privacy requirement,
# tests/test_repo_init.py TestNoMachineIdentifyingContent).
PY="${MEMCONTINUUM_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "ERROR: no python resolved -- set \$MEMCONTINUUM_PYTHON to a venv python" \
    "with fastembed/PyYAML installed (see README.md Requirements)" >&2
  exit 1
fi
PROBES_FILE="${MEMCONTINUUM_BENCH_PROBES:-$REPO_ROOT/docs/internal/gold-probes.tsv}"
if [ ! -f "$PROBES_FILE" ]; then
  echo "ERROR: no probe set at $PROBES_FILE -- set \$MEMCONTINUUM_BENCH_PROBES to a" \
    "TSV of 'query<TAB>qualified_name<TAB>negative_substr' rows with a '#corpus:' header" >&2
  exit 1
fi
# corpus root: argv -> env -> the probe file's own #corpus: line
PROBES_CORPUS="$(sed -n 's/^#corpus:[[:space:]]*//p' "$PROBES_FILE" | head -1)"
PROBES_CORPUS="${PROBES_CORPUS/#\~/$HOME}"
SOURCES_ROOT="${1:-${MEMCONTINUUM_BENCH_CORPUS:-$PROBES_CORPUS}}"
if [ -z "$SOURCES_ROOT" ]; then
  echo "ERROR: no corpus root -- pass one as argv[1], set \$MEMCONTINUUM_BENCH_CORPUS," \
    "or give $PROBES_FILE a '#corpus:' line" >&2
  exit 1
fi

CACHE_DIR="$HOME/.cache/codanna-bench"
BIN_DIR="$CACHE_DIR/bin"
CODANNA_BIN="$BIN_DIR/codanna"
SANDBOX_DIR="$CACHE_DIR/index-sandbox"

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

OUR_DB="$CACHE_DIR/bench-code.sqlite"
OUR_PROJECT="codanna-bench"

echo "=== codanna-bench: comparing memidx.py code-search vs Codanna ==="
echo "sources root: $SOURCES_ROOT"
if [ ! -d "$SOURCES_ROOT" ]; then
  echo "ERROR: $SOURCES_ROOT does not exist -- nothing to index" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 1. sandbox-install Codanna (best effort, never fatal)
# ---------------------------------------------------------------------------

CODANNA_AVAILABLE=0

install_codanna_from_release() {
  command -v curl >/dev/null 2>&1 || return 1
  command -v jq >/dev/null 2>&1 || return 1
  local arch os asset_name api_url dl_url tmp_tar
  arch="$(uname -m)"
  os="$(uname -s)"
  case "$os-$arch" in
    Linux-x86_64) asset_name="linux-x64" ;;
    Linux-aarch64|Linux-arm64) asset_name="linux-arm64" ;;
    Darwin-x86_64) asset_name="macos-x64" ;;
    Darwin-arm64) asset_name="macos-arm64" ;;
    *) echo "codanna-bench: no known Codanna release asset for $os-$arch" >&2; return 1 ;;
  esac
  api_url="https://api.github.com/repos/bartolli/codanna/releases/latest"
  dl_url="$(curl -sL "$api_url" | jq -r --arg pat "$asset_name" \
    '.assets[] | select(.name | test($pat)) | select(.name | endswith(".tar.xz")) | .browser_download_url' \
    | head -1)"
  if [ -z "$dl_url" ] || [ "$dl_url" = "null" ]; then
    echo "codanna-bench: could not resolve a Codanna release asset for $asset_name" >&2
    return 1
  fi
  tmp_tar="$WORK_DIR/codanna-release.tar.xz"
  if ! curl -sL -o "$tmp_tar" "$dl_url"; then
    echo "codanna-bench: download failed ($dl_url)" >&2
    return 1
  fi
  mkdir -p "$BIN_DIR"
  local extract_dir="$WORK_DIR/codanna-extract"
  mkdir -p "$extract_dir"
  if ! tar -xJf "$tmp_tar" -C "$extract_dir"; then
    echo "codanna-bench: extract failed" >&2
    return 1
  fi
  local found_bin
  found_bin="$(find "$extract_dir" -type f -name codanna | head -1)"
  if [ -z "$found_bin" ]; then
    echo "codanna-bench: no 'codanna' binary found in the release archive" >&2
    return 1
  fi
  cp "$found_bin" "$CODANNA_BIN"
  chmod +x "$CODANNA_BIN"
  return 0
}

install_codanna_from_cargo() {
  command -v cargo >/dev/null 2>&1 || return 1
  mkdir -p "$BIN_DIR"
  if ! cargo install codanna --root "$CACHE_DIR" --quiet; then
    return 1
  fi
  [ -x "$CACHE_DIR/bin/codanna" ]
}

if [ -x "$CODANNA_BIN" ]; then
  CODANNA_AVAILABLE=1
  echo "codanna-bench: using cached Codanna at $CODANNA_BIN"
else
  echo "codanna-bench: Codanna not cached yet, attempting sandbox install under $CACHE_DIR ..."
  if install_codanna_from_release; then
    CODANNA_AVAILABLE=1
    echo "codanna-bench: installed Codanna release binary to $CODANNA_BIN"
  elif install_codanna_from_cargo; then
    CODANNA_AVAILABLE=1
    echo "codanna-bench: built Codanna via cargo install to $CODANNA_BIN"
  else
    echo "codanna-bench: SKIPPING Codanna (no release asset for this platform, no network, or no cargo available)."
    echo "codanna-bench: continuing with our own fts/vector/hybrid probes only; Codanna columns will read SKIPPED."
  fi
fi

if [ "$CODANNA_AVAILABLE" = "1" ]; then
  mkdir -p "$SANDBOX_DIR"
  ( cd "$SANDBOX_DIR" && [ -d .codanna ] || "$CODANNA_BIN" init >/dev/null 2>&1 )
  echo "codanna-bench: indexing $SOURCES_ROOT with Codanna (reused if already indexed) ..."
  T0=$(date +%s.%N)
  if ! ( cd "$SANDBOX_DIR" && "$CODANNA_BIN" index "$SOURCES_ROOT" --no-progress >"$WORK_DIR/codanna-index.log" 2>&1 ); then
    echo "codanna-bench: Codanna indexing failed -- see $WORK_DIR/codanna-index.log; disabling Codanna columns" >&2
    CODANNA_AVAILABLE=0
  else
    T1=$(date +%s.%N)
    echo "codanna-bench: Codanna index done in $(PYTHONPATH= "$PY" -c "print(f'{$T1-$T0:.2f}s')")"
  fi
fi

# ---------------------------------------------------------------------------
# 2. our side: code-reindex (embeddings on, so vector/hybrid modes work)
# ---------------------------------------------------------------------------

echo "codanna-bench: running memidx.py code-reindex on $SOURCES_ROOT ..."
T0=$(date +%s.%N)
PYTHONPATH= "$PY" "$MEMIDX" code-reindex --code-root "$SOURCES_ROOT" --project "$OUR_PROJECT" \
  --db "$OUR_DB" || { echo "codanna-bench: our code-reindex failed" >&2; exit 1; }
T1=$(date +%s.%N)
echo "codanna-bench: our code-reindex done in $(PYTHONPATH= "$PY" -c "print(f'{$T1-$T0:.2f}s')")"

# ---------------------------------------------------------------------------
# 3. the probe protocol
#
# Probes come from $PROBES_FILE (untracked -- see the header). TSV:
# query \t gold_qualified_name \t negative_substr ("-" = none; never empty,
# see the read loop below) -- the
# "negatives rule": if a hit whose qualified_name contains negative_substr
# outranks the gold within the top 3, that probe FAILS even if the gold is
# technically present, because a bypass/exception site beating the sanctioned
# implementation is the wrong answer to give a caller asking "how do I do X".
# ---------------------------------------------------------------------------

PROBES_TSV="$WORK_DIR/probes.tsv"
grep -v '^#' "$PROBES_FILE" | grep -v '^[[:space:]]*$' > "$PROBES_TSV"
echo "codanna-bench: $(wc -l < "$PROBES_TSV") probes from $PROBES_FILE"

# ---------------------------------------------------------------------------
# 4. run every probe through fts/vector/hybrid (ours) and Codanna's lexical
#    (`retrieve search`) + semantic (`mcp semantic_search_docs`) modes, top-3
#    each, then print a comparison table.
# ---------------------------------------------------------------------------

our_probe() {
  local query="$1" mode="$2"
  PYTHONPATH= "$PY" "$MEMIDX" code-search "$query" --project "$OUR_PROJECT" --db "$OUR_DB" \
    --mode "$mode" --limit 3 --json 2>/dev/null
}

codanna_lexical_probe() {
  local query="$1"
  ( cd "$SANDBOX_DIR" && "$CODANNA_BIN" retrieve search "$query" --limit 3 --json 2>/dev/null )
}

codanna_semantic_probe() {
  local query="$1"
  ( cd "$SANDBOX_DIR" && "$CODANNA_BIN" mcp semantic_search_docs "query:$query" limit:3 --json 2>/dev/null )
}

RESULTS_JSON="$WORK_DIR/results.jsonl"
: > "$RESULTS_JSON"

while IFS=$'\t' read -r query gold negative gold_flag; do
  [ -z "$query" ] && continue
  # Columns are never empty in the probe file ("-" means none) precisely
  # because TAB is IFS whitespace: `read` collapses consecutive tabs, so an
  # empty column would shift every later one into the wrong variable.
  [ "$negative" = "-" ] && negative=""
  our_fts="$(our_probe "$query" fts)"
  our_vector="$(our_probe "$query" vector)"
  our_hybrid="$(our_probe "$query" hybrid)"
  if [ "$CODANNA_AVAILABLE" = "1" ]; then
    cod_lex="$(codanna_lexical_probe "$query")"
    cod_sem="$(codanna_semantic_probe "$query")"
  else
    cod_lex="null"
    cod_sem="null"
  fi
  jq -nc --arg query "$query" --arg gold "$gold" --arg negative "$negative" \
    --argjson our_fts "${our_fts:-[]}" --argjson our_vector "${our_vector:-[]}" \
    --argjson our_hybrid "${our_hybrid:-[]}" \
    --argjson cod_lex "${cod_lex:-null}" --argjson cod_sem "${cod_sem:-null}" \
    '{query: $query, gold: $gold, negative: $negative, our_fts: $our_fts,
      our_vector: $our_vector, our_hybrid: $our_hybrid, cod_lex: $cod_lex, cod_sem: $cod_sem}' \
    >> "$RESULTS_JSON"
done < "$PROBES_TSV"

# ---------------------------------------------------------------------------
# 5. render the comparison table (rank-finding + the negatives rule live
#    here, in ad-hoc script-local Python -- never imported by memidx.py)
# ---------------------------------------------------------------------------

PYTHONPATH= "$PY" - "$RESULTS_JSON" "$CODANNA_AVAILABLE" <<'PYEOF'
import json
import sys

results_path, codanna_available = sys.argv[1], sys.argv[2] == "1"


def our_rank(hits, gold):
    for i, h in enumerate(hits, start=1):
        if h.get("qualified_name") == gold:
            return i
    return None


def our_negative_beats_gold(hits, gold, negative):
    if not negative:
        return False
    gold_rank = our_rank(hits, gold)
    for i, h in enumerate(hits, start=1):
        if negative in (h.get("qualified_name") or ""):
            if gold_rank is None or i < gold_rank:
                return True
    return False


def codanna_items(payload):
    if not payload or payload in (None, "null"):
        return []
    return payload.get("data") or []


def codanna_qname(item):
    sym = item.get("symbol") or {}
    mod = sym.get("module_path") or ""
    name = sym.get("name") or ""
    return f"{mod}.{name}" if mod else name


def codanna_rank(payload, gold):
    gold_symbol = gold.split(".")[-1]
    for i, item in enumerate(codanna_items(payload), start=1):
        qn = codanna_qname(item)
        if qn == gold or qn.endswith("." + gold_symbol) or (item.get("symbol") or {}).get("name") == gold_symbol:
            return i
    return None


def codanna_negative_beats_gold(payload, gold, negative):
    if not negative:
        return False
    g = codanna_rank(payload, gold)
    for i, item in enumerate(codanna_items(payload), start=1):
        qn = codanna_qname(item)
        if negative in qn:
            if g is None or i < g:
                return True
    return False


def fmt_rank(r, neg_fail):
    if neg_fail:
        return "FAIL(neg)"
    return str(r) if r is not None else "-"


rows = []
our_top1_hits = {"fts": 0, "vector": 0, "hybrid": 0}
cod_top1_hits = {"lex": 0, "sem": 0}
n = 0

with open(results_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        n += 1
        query, gold, negative = r["query"], r["gold"], r["negative"]

        fts_rank = our_rank(r["our_fts"], gold)
        vec_rank = our_rank(r["our_vector"], gold)
        hyb_rank = our_rank(r["our_hybrid"], gold)
        fts_neg = our_negative_beats_gold(r["our_fts"], gold, negative)
        vec_neg = our_negative_beats_gold(r["our_vector"], gold, negative)
        hyb_neg = our_negative_beats_gold(r["our_hybrid"], gold, negative)

        if fts_rank == 1 and not fts_neg:
            our_top1_hits["fts"] += 1
        if vec_rank == 1 and not vec_neg:
            our_top1_hits["vector"] += 1
        if hyb_rank == 1 and not hyb_neg:
            our_top1_hits["hybrid"] += 1

        if codanna_available:
            lex_rank = codanna_rank(r["cod_lex"], gold)
            sem_rank = codanna_rank(r["cod_sem"], gold)
            lex_neg = codanna_negative_beats_gold(r["cod_lex"], gold, negative)
            sem_neg = codanna_negative_beats_gold(r["cod_sem"], gold, negative)
            if lex_rank == 1 and not lex_neg:
                cod_top1_hits["lex"] += 1
            if sem_rank == 1 and not sem_neg:
                cod_top1_hits["sem"] += 1
            lex_cell, sem_cell = fmt_rank(lex_rank, lex_neg), fmt_rank(sem_rank, sem_neg)
        else:
            lex_cell = sem_cell = "SKIPPED"

        rows.append((
            query[:42], gold,
            fmt_rank(fts_rank, fts_neg), fmt_rank(vec_rank, vec_neg), fmt_rank(hyb_rank, hyb_neg),
            lex_cell, sem_cell,
        ))

col_widths = [44, 42, 5, 5, 5, 9, 9]
headers = ["query", "gold", "fts", "vec", "hyb", "cod-lex", "cod-sem"]

def row_line(cells):
    return "  ".join(str(c).ljust(w) for c, w in zip(cells, col_widths))

print()
print("=== comparison table (rank of gold in top-3; '-' = not in top-3; 'FAIL(neg)' = a bypass/exception site outranked the gold) ===")
print(row_line(headers))
print(row_line(["-" * w for w in col_widths]))
for row in rows:
    print(row_line(row))

print()
print(f"our top-1 hit rate over {n} probes: fts={our_top1_hits['fts']}/{n} vector={our_top1_hits['vector']}/{n} hybrid={our_top1_hits['hybrid']}/{n}")
if codanna_available:
    print(f"codanna top-1 hit rate over {n} probes: lexical={cod_top1_hits['lex']}/{n} semantic={cod_top1_hits['sem']}/{n}")
else:
    print("codanna: SKIPPED (not installed in this environment)")
PYEOF

echo
echo "=== codanna-bench: done ==="
