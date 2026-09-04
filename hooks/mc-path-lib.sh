#!/usr/bin/env bash
# mc-path-lib.sh -- one pure, side-effect-free function: mc_path_under_root.
# Source this file (not hooks/memlib.sh) when that is all a caller needs --
# unlike memlib.sh, sourcing this file does no I/O, sets no MEMCONTINUUM_*
# defaults, and touches no filesystem beyond what mc_path_under_root itself
# does when actually CALLED. Safe to source unconditionally at the top of a
# script, or lazily right before first use, with identical cost either way.
#
# Sourced by hooks/memlib.sh (so every one of its callers gets
# mc_path_under_root for free) and directly by hooks/newfile-nudge.sh (which
# needs mc_path_under_root but deliberately does NOT source memlib.sh -- see
# that file's own header for why). One implementation, two independent
# callers, extracted here specifically so newfile-nudge.sh never has to pay
# memlib.sh's mkdir/config.sh/MC_PY-resolution cost just to reach this one
# function (symlink-paths review round 1, finding 3).

# mc_path_under_root FILE_PATH ROOT
#
# Symlink-safe containment: does FILE_PATH's real location sit under ROOT's
# real location? Originally hooks/newfile-nudge.sh's own fix (2026-08-31
# review) for its PreToolUse containment check; factored out (first into
# hooks/memlib.sh, then here) so hooks/ledger-post-edit.sh shares the SAME
# implementation instead of keeping the plain lexical prefix match that fix
# already replaced in newfile-nudge.sh -- one implementation, both hooks
# call it.
#
# A plain lexical `case "$FILE_PATH" in "$ROOT"/*` prefix match is fooled
# both by a literal `/../` traversal segment (textually under ROOT while
# actually resolving to a sibling of it) and by a symlinked ancestor
# directory (every path segment textually under ROOT, but the real
# directory it names lives elsewhere). Fixed bash-3.2-safe, no external
# binaries beyond what every caller here already uses:
#   1. reject any literal `/../` traversal segment (or a leading `../`, or
#      a bare `..`) outright, purely as a string -- a syntactic red flag
#      regardless of what it would resolve to.
#   2. canonicalize ROOT and the nearest EXISTING ancestor directory of
#      FILE_PATH (walking up via dirname -- handles both a FILE_PATH that
#      already exists, ledger-post-edit.sh's usual case, and one that does
#      not yet, newfile-nudge.sh's usual case) via `cd ... && pwd -P`,
#      which resolves symlinks, and require that ancestor to sit under the
#      canonicalized root -- compared as a LITERAL string (see below), not
#      as a case/glob pattern.
#
# Returns WHY, not just yes/no -- newfile-nudge.sh's outcome= vocabulary
# distinguishes these in hook.log (memidx.py stats greps it); a caller
# that only needs yes/no (ledger-post-edit.sh) collapses every nonzero
# into its own single out-of-scope outcome.
#   0  under ROOT
#   1  outside ROOT (both resolve, but FILE_PATH's ancestor is not under it)
#   2  literal `..` traversal segment in FILE_PATH
#   3  ROOT itself does not resolve (missing, not a directory, etc.)
#   4  FILE_PATH has no existing ancestor to resolve from
#   5  FILE_PATH's existing ancestor does not resolve
mc_path_under_root() {
    local file_path="$1" root="$2" root_real ancestor next ancestor_real stripped
    case "$file_path" in
        */../*|*/..|../*|..) return 2 ;;
    esac
    root_real="$(cd "$root" 2>/dev/null && pwd -P)"
    [ -n "$root_real" ] || return 3
    ancestor="$file_path"
    while [ ! -d "$ancestor" ]; do
        next="$(dirname "$ancestor")"
        if [ "$next" = "$ancestor" ]; then
            return 4
        fi
        ancestor="$next"
    done
    ancestor_real="$(cd "$ancestor" 2>/dev/null && pwd -P)"
    [ -n "$ancestor_real" ] || return 5
    if [ "$ancestor_real" = "$root_real" ]; then
        return 0
    fi
    # Segment-aware, LITERAL prefix test -- never a case/glob pattern match
    # on $root_real (symlink-review round 1, finding 1): a store/code root
    # whose physical directory name happens to contain a shell glob
    # metacharacter (*, ?, [) must still be compared as a literal string,
    # never interpreted as a wildcard. `${var#"$prefix"}` with the prefix
    # itself double-quoted performs LITERAL removal (bash's quote-removal
    # applies to the quoted portion of a parameter-expansion pattern before
    # any globbing would apply to it) -- so `stripped` differs from
    # `ancestor_real` iff `ancestor_real` truly began with the literal
    # `"$root_real/"` string. `/foo` can never match a `/foobar` ancestor
    # this way (segment-aware), and a literal `*`/`?`/`[` inside
    # `$root_real` can never accidentally widen the match (glob-safe).
    # Verified directly under both bash 5.2 and the real bash 3.2.57.
    stripped="${ancestor_real#"$root_real"/}"
    if [ "$stripped" != "$ancestor_real" ]; then
        return 0
    fi
    return 1
}
