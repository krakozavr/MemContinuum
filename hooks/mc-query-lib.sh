#!/usr/bin/env bash
# mc-query-lib.sh -- pure, side-effect-free helpers that turn an edited
# FILE_PATH into a small bag of search words: the query the search-fallback
# channel (TOP-0133 L1) hands to `memidx.py search` when a pre-edit lookup
# (hooks/pre-edit-chain.sh) or a new-file nudge (hooks/newfile-nudge.sh)
# found nothing bound to the path. No I/O beyond argv/stdout, no MEMCONTINUUM_*
# defaulting, no git -- safe to source unconditionally, same discipline as
# hooks/mc-path-lib.sh (see that file's own header). bash-3.2-safe throughout
# (macOS CI): no `${var,,}`, no `mapfile`, no associative arrays -- `tr`/`sed
# -E`/a plain `for word in $unquoted` split instead.
#
# Two public functions:
#
#   mc_query_source_path PATH CWD STRIP_PREFIXES
#     Picks the form of PATH to tokenize -- a repo-relative path when one is
#     recoverable, the raw PATH otherwise. Tokenizing a raw absolute path
#     buries the filename's own words under machine-path noise ("home
#     someuser dev projectname ...", capped out by mc_query_tokens' own
#     12-token cap before the words that actually matter ever get a look),
#     and a worktree path's raw form spells its `.worktrees/x/...` segment
#     into the query too -- both are exactly the noise the query is
#     supposed to be built FROM THE PATH'S OWN WORDS, not from where it
#     happens to sit on disk. Tries, in order: CWD-relative (mirrors
#     pre-edit-chain.sh's own first candidate), then each colon-separated
#     STRIP_PREFIXES entry in turn (mirrors its MEMCONTINUUM_STRIP_PREFIX
#     candidates) -- the FIRST one that actually strips something wins;
#     falls back to PATH unchanged when none apply (STRIP_PREFIXES empty,
#     CWD empty/not a real ancestor, or PATH simply isn't under any of
#     them). Callers pass the WORKTREE-REMAPPED path here when one exists
#     (pre-edit-chain.sh's own WT_REMAPPED, already `<configured-root>/
#     <relative>` -- one of STRIP_PREFIXES strips it straight down to
#     <relative>), never the worktree's own raw path.
#
#   mc_query_tokens PATH
#     The query-builder proper. Prints the resulting tokens space-joined
#     on stdout (empty string, never an error, when nothing survives the
#     filters -- callers check for that themselves via `[ -z "$q" ]`, same
#     "print nothing" convention `mc_remap_worktree_path` etc. already use
#     for "not applicable"). Stated limits, in the order applied:
#       1. extension stripped from the FINAL path component only (a dot in
#          a directory name never truncates the path) -- `${base%.*}`.
#       2. split into words on `/ _ - .` (directories and the stem both)
#          AND on a camelCase boundary (a lowercase/digit letter directly
#          followed by an uppercase one).
#       3. lowercased.
#       4. any token shorter than 3 characters dropped.
#       5. any token in the fixed generic-stem list dropped (see
#          MC_QUERY_GENERIC_STEMS below) -- these name no project-specific
#          concept, they just describe "this is code".
#       6. deduped, first occurrence wins (path order preserved).
#       7. capped at 12 tokens.
#     No relevance floor, no ranking here -- this only decides WHICH words
#     reach the search call at all; how those words rank once they get
#     there is memidx.py search's own job (deliberately unchanged by this
#     feature -- see docs/DESIGN.md's search-fallback ruling for why).
#
#     Stated limit (MINOR fix-round item, not a bug to fix here): the
#     lowercasing step (`tr '[:upper:]' '[:lower:]'`) and the length/split
#     rules above are ASCII-only. A path whose meaningful words are
#     non-ASCII (Cyrillic, CJK, accented Latin, etc.) is not lowercased,
#     not camelCase-split, and its "length" is a BYTE count under `${#word}`
#     in a non-UTF-8 locale (bash 3.2/macOS default `C`/`POSIX` locale
#     included) -- a short multi-byte word can be dropped as "too short"
#     or kept as an oversized byte-run depending on the runtime locale.
#     This channel degrades to "no query" or a partial/garbled one on such
#     a path rather than crashing; full Unicode-aware tokenization is out
#     of scope for this feature (see docs/INTERNALS.md's own note).
#
# mc_now_ms -- millisecond-epoch timestamp, bash-3.2-safe (no
# $EPOCHREALTIME, added in bash 5.0; used directly when present). `date +%s%3N` (GNU coreutils) prints
# 3 zero-padded fractional digits; BSD/macOS `date` has no `%N` at all and
# prints the literal characters `3N` back -- probed ONCE per process via
# MC_QUERY_MS_MODE (empty until the first call) rather than re-probing on
# every timing call a hook makes. The probe result is a plain digit
# string check (`case ... in *[!0-9]*)`), not a hardcoded GNU-vs-BSD
# uname branch, so it also degrades correctly on any OTHER date build
# with partial/no %N support. Falls back to a python one-liner
# (time.time()) when `date` itself can't do it -- always available here
# (every caller already requires python for the search subprocess itself).

# MC_QUERY_GENERIC_STEMS -- exact list per TOP-0133 L1's own spec: words that
# describe "this is code" rather than naming a project-specific concept.
# Space-separated (matches the split-word loop's own `for word in $list`
# idiom used throughout hooks/*.sh -- see hooks/newfile-nudge.sh's
# _ext_matches for the same unquoted-on-purpose pattern), checked via a
# padded `case " $list " in *" $word "*)` substring test so no per-word
# array/loop is needed to answer "is $word in this list".
MC_QUERY_GENERIC_STEMS="src lib sources source docs doc test tests spec main index utils util helpers internal app core common base readme package makefile config init setup"

# mc_query_source_path PATH CWD STRIP_PREFIXES
mc_query_source_path() {
    local path="$1" cwd="$2" prefixes="$3" prefix stripped
    if [ -n "$cwd" ]; then
        stripped="${path#"$cwd"/}"
        if [ "$stripped" != "$path" ]; then
            printf '%s' "$stripped"
            return 0
        fi
    fi
    if [ -n "$prefixes" ]; then
        # Split with `read -a`, the same way mc-path-lib.sh and
        # pre-edit-chain.sh do: an unquoted `for ... in $prefixes` would
        # pathname-expand a prefix containing `*`, `?` or `[`.
        local _mc_qsp_prefix _mc_qsp_parts
        IFS=: read -r -a _mc_qsp_parts <<< "$prefixes"
        for _mc_qsp_prefix in "${_mc_qsp_parts[@]}"; do
            [ -z "$_mc_qsp_prefix" ] && continue
            stripped="${path#"$_mc_qsp_prefix"}"
            if [ "$stripped" != "$path" ]; then
                # STRIP_PREFIX entries are rendered WITHOUT a trailing
                # slash's guarantee either way (pre-edit-chain.sh's own
                # candidate builder makes no such assumption -- see its
                # `add_candidate "${FILE_PATH#"$prefix"}"` above); a bare
                # leading slash left behind by the strip is trimmed so the
                # relative form never starts with one.
                stripped="${stripped#/}"
                printf '%s' "$stripped"
                return 0
            fi
        done
    fi
    printf '%s' "$path"
}

# mc_query_tokens PATH -- see file header for the full, numbered rule.
mc_query_tokens() {
    local path="$1" dir base words lower word out="" count=0
    base="${path##*/}"
    dir="${path%/*}"
    [ "$dir" = "$path" ] && dir=""
    case "$base" in
        *.*) base="${base%.*}" ;;
    esac
    if [ -n "$dir" ]; then
        path="$dir/$base"
    else
        path="$base"
    fi
    # Split on / \ _ - . (rule 2, path half) via `tr`; the camelCase half
    # of rule 2 (`sed -E`) runs on the ALREADY-space-split text next --
    # doing both in one `tr` pass is not possible (tr has no lookaround),
    # and running sed first (before the path separators are gone) would
    # let `-E`'s own `.` metacharacter make the camelCase pattern match
    # across a `/`, which it must never do. MINOR fix-round item: `\` is
    # normalized as a separator too -- a Windows-style path (a payload
    # from a Windows checkout, or a query string authored with `\`
    # literally) tokenizes exactly like its `/`-separated form instead of
    # gluing two path segments into one bogus token. `\` is escaped as
    # `\\` in tr's SET1 -- GNU/BSD tr both treat a bare, un-escaped `\`
    # before a non-escape character (e.g. `\_`) as consuming the
    # backslash without actually adding it to the match set (verified:
    # `tr '/\_.-' ...` left `\` unmatched in an embedded-backslash
    # fixture); `\\` is the one spelling that reliably means "match one
    # literal backslash" on both. `-` stays LAST in the set (never
    # adjacent to another literal after escape resolution), so it can
    # never be misread as a `X-Y` range.
    words="$(printf '%s' "$path" | tr '/\\_.-' '     ')"
    words="$(printf '%s' "$words" | sed -E 's/([a-z0-9])([A-Z])/\1 \2/g')"
    lower="$(printf '%s' "$words" | tr '[:upper:]' '[:lower:]')"
    for word in $lower; do
        [ ${#word} -lt 3 ] && continue
        case " $MC_QUERY_GENERIC_STEMS " in *" $word "*) continue ;; esac
        case " $out " in *" $word "*) continue ;; esac
        out="${out:+$out }$word"
        count=$((count + 1))
        [ "$count" -ge 12 ] && break
    done
    printf '%s' "$out"
}

# MC_QUERY_MS_MODE -- "date" | "perl" | "python", empty until mc_now_ms's
# first call (per-process probe cache, see the header comment above).
MC_QUERY_MS_MODE="${MC_QUERY_MS_MODE:-}"

# mc_now_ms [PYTHON_INTERPRETER [NO_PYTHON]] -- millisecond-epoch on stdout, and also in
# $MC_NOW_MS when called in the current shell (not inside $(...)), so
# hooks/memlib.sh's elapsed timer reads it with no subshell. bash >= 5 reads
# $EPOCHREALTIME (no fork at all); older bash uses the probe below. See the
# file header comment for the GNU-%3N-vs-BSD-no-%N probe this caches. This is
# the ONE "now" helper in the tree: memlib.sh's mc_elapsed_field is built on it.
# Ladder: EPOCHREALTIME -> date +%3N -> perl Time::HiRes (ships with macOS,
# about 4 ms a call; a whole-second clock would log a 20 ms hook that spans
# a second boundary as elapsed=1.0s) -> python (unless NO_PYTHON=1) -> whole
# seconds from `date +%s` (times 1000). NO_PYTHON=1 (a log field must never
# fork an interpreter) skips the python rung only. Callers that omit it keep
# the python fallback.
mc_now_ms() {
    local py="${1:-python3}" nopy="${2:-}" probe
    if [ -n "${EPOCHREALTIME:-}" ]; then
        MC_NOW_MS="${EPOCHREALTIME/[.,]/}"
        MC_NOW_MS="${MC_NOW_MS%???}"
        printf '%s\n' "$MC_NOW_MS"
        return 0
    fi
    if [ -z "$MC_QUERY_MS_MODE" ]; then
        probe="$(date +%3N 2>/dev/null)"
        case "$probe" in
            [0-9][0-9][0-9]) MC_QUERY_MS_MODE="date" ;;
            *)
                probe="$(perl -MTime::HiRes=time -e 'printf("%.0f\n", time()*1000)' 2>/dev/null)"
                case "$probe" in
                    ""|*[!0-9]*) MC_QUERY_MS_MODE="python" ;;
                    *) [ "${#probe}" -ge 13 ] && MC_QUERY_MS_MODE="perl" || MC_QUERY_MS_MODE="python" ;;
                esac ;;
        esac
    fi
    if [ "$MC_QUERY_MS_MODE" = "date" ]; then
        MC_NOW_MS="$(date +%s%3N 2>/dev/null)"
    elif [ "$MC_QUERY_MS_MODE" = "perl" ]; then
        MC_NOW_MS="$(perl -MTime::HiRes=time -e 'printf("%.0f\n", time()*1000)' 2>/dev/null)"
    elif [ "$nopy" = "1" ]; then
        MC_NOW_MS="$(( $(date +%s 2>/dev/null || echo 0) * 1000 ))"
    else
        MC_NOW_MS="$(PYTHONPATH= "$py" -c 'import time; print(int(time.time() * 1000))' 2>/dev/null)"
    fi
    printf '%s\n' "$MC_NOW_MS"
}
