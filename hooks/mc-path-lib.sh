#!/usr/bin/env bash
# mc-path-lib.sh -- pure, side-effect-free path helpers: mc_path_under_root,
# mc_nested_worktree_gitfile, and mc_remap_worktree_path (fix round 2 NIT:
# the file grew two more public functions after the "one function" header
# was written; corrected here rather than left to describe only the first
# one added). Source this file (not hooks/memlib.sh) when that is all a
# caller needs -- unlike memlib.sh, sourcing this file does no I/O, sets no
# MEMCONTINUUM_* defaults, and touches no filesystem beyond what the
# function actually CALLED does. Safe to source unconditionally at the top
# of a script, or lazily right before first use, with identical cost
# either way.
#
# Sourced by hooks/memlib.sh (so every one of its callers gets these for
# free) and directly by hooks/newfile-nudge.sh (which needs
# mc_path_under_root but deliberately does NOT source memlib.sh -- see that
# file's own header for why) and hooks/pre-edit-chain.sh (which needs all
# three but also deliberately does not source memlib.sh). One
# implementation of each, several independent callers -- extracted here
# specifically so a caller that only needs path logic never pays
# memlib.sh's mkdir/config.sh/MC_PY-resolution cost just to reach it
# (symlink-paths review round 1, finding 3). hooks/memlib.sh has no remap
# helper of its own -- both hooks that need one call into this file.
#
# _mc_nearest_existing_ancestor (below, internal-only, no leading "mc_" --
# not part of this file's public surface) is the walk-to-nearest-existing-
# ancestor-directory step (via `dirname`, bash-only) shared by
# mc_path_under_root, mc_nested_worktree_gitfile, and
# mc_remap_worktree_path -- fix round 2 NIT (Grok): this walk plus its `..`
# traversal guard used to be copied verbatim into each function that
# needed it; factored out once here instead.

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
# _mc_nearest_existing_ancestor PATH
#
# Internal, not part of this file's public surface (no "mc_" prefix).
# Bash-only (dirname loop, no git, no python): walks PATH up to its
# nearest EXISTING ancestor directory -- handles both a PATH that already
# exists (ledger-post-edit.sh's usual case) and one that does not yet
# (newfile-nudge.sh's usual case, and a Write of a brand-new file in
# general). Prints the ancestor on stdout and returns 0; returns 1 if the
# walk reaches "/" without finding one (should not happen for a normal
# absolute path, since "/" itself always exists, but guards a malformed
# PATH from looping forever on a `dirname` fixed point).
_mc_nearest_existing_ancestor() {
    local p="$1" next
    while [ ! -d "$p" ]; do
        next="$(dirname "$p")"
        if [ "$next" = "$p" ]; then
            return 1
        fi
        p="$next"
    done
    printf '%s\n' "$p"
    return 0
}

mc_path_under_root() {
    local file_path="$1" root="$2" root_real ancestor ancestor_real stripped
    case "$file_path" in
        */../*|*/..|../*|..) return 2 ;;
    esac
    root_real="$(cd "$root" 2>/dev/null && pwd -P)"
    [ -n "$root_real" ] || return 3
    ancestor="$(_mc_nearest_existing_ancestor "$file_path")" || return 4
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

# mc_nested_worktree_gitfile FILE_PATH ROOT
#
# Round 2 BLOCKER fix (Grok): both callers used to skip
# mc_remap_worktree_path entirely whenever `mc_path_under_root FILE_PATH
# ROOT` already returned 0 -- reasoning that a file physically under a
# configured root can never need remapping. False for a `git worktree add`
# checkout made INSIDE the root itself (e.g. `<root>/.worktrees/feat/`):
# FILE_PATH is genuinely under ROOT by that plain containment test, but the
# worktree is a SEPARATE git checkout from ROOT's own -- its tracked-file
# paths are relative to the WORKTREE's own toplevel, not ROOT's, so the
# raw/cwd-relative/STRIP_PREFIX candidates every caller already tries for
# FILE_PATH itself are the wrong relative form and a real decision binds
# to nothing. This function is the caller-side gate that decides whether
# that expensive check (one identity `git` call for FILE_PATH itself, plus
# one more per configured root while mc_remap_worktree_path searches for a
# match) is worth paying for an in-root FILE_PATH: bash-only, no git, no
# python --
# does a `.git` FILE (a linked worktree's own marker -- an ordinary
# checkout's `.git` is a DIRECTORY) sit somewhere on FILE_PATH's ancestor
# chain STRICTLY BELOW ROOT's own real location?
#
# Deliberately EXCLUDES ROOT's own `.git` from the check (stops the walk
# the instant it reaches ROOT's real path, never tests ROOT itself): a
# root created via `--separate-git-dir`, or a root that is itself a linked
# worktree of some other checkout (both real layouts this repo supports,
# see mc_remap_worktree_path's own root-loop fix below), has a `.git` FILE
# right AT the root -- an ordinary in-root main-checkout file must still
# pay zero git calls in that layout too, and would not if this function
# tested ROOT's own entry as well.
#
# Returns 0 (nested gitfile found -- pay for the real check) or 1
# (no nested gitfile before reaching ROOT, or ROOT/FILE_PATH don't
# resolve -- ordinary in-root file, skip the remap attempt entirely, zero
# cost beyond this bash-only walk).
mc_nested_worktree_gitfile() {
    local file_path="$1" root="$2" root_real ancestor
    root_real="$(cd "$root" 2>/dev/null && pwd -P)"
    [ -n "$root_real" ] || return 1
    ancestor="$(_mc_nearest_existing_ancestor "$file_path")" || return 1
    ancestor="$(cd "$ancestor" 2>/dev/null && pwd -P)"
    [ -n "$ancestor" ] || return 1
    while [ "$ancestor" != "$root_real" ] && [ -n "$ancestor" ] && [ "$ancestor" != "/" ]; do
        if [ -f "$ancestor/.git" ]; then
            return 0
        fi
        ancestor="$(dirname "$ancestor")"
    done
    return 1
}

# mc_remap_worktree_path FILE_PATH ROOTS
#
# The worktree gap (docs/internal/SESSION-HANDOFF-releases-0.3-to-0.6.md
# SS"0.2.0 final" item 1): MEMCONTINUUM_CODE_ROOTS/MEMCONTINUUM_STRIP_PREFIX name
# absolute paths fixed at repo-init time. A `git worktree add` checkout of
# a wired repo is a different absolute directory nothing wired -- every
# candidate hooks/pre-edit-chain.sh builds and hooks/ledger-post-edit.sh's
# own root-containment loop both come up empty, indistinguishable from a
# genuine absence. This function maps the EDITED PATH back to its
# main-checkout equivalent so the existing candidate/containment logic can
# be reused unchanged -- it does not itself decide anything about whether a
# decision applies, and it never causes worktree CONTENT to be indexed
# (only a path string is computed here).
#
# ROOTS is a newline-separated list of configured code-root absolute paths
# (mc_code_roots' own output shape for ledger-post-edit.sh; the caller
# builds it from MEMCONTINUUM_STRIP_PREFIX -- stripped of a trailing slash --
# for pre-edit-chain.sh, which deliberately does not source memlib.sh/
# mc_code_roots to avoid that file's mkdir/config.sh cost on every
# already-a-miss lookup; repo-init.sh renders the STRIP_PREFIX entries
# = the retrieval roots (the git toplevel of each code root), per
# CODE_ROOT_FILTERS in templates/code-root-filter-pair.json.tmpl -- a
# hand-wired STRIP_PREFIX that happens not to equal any real repo root
# only ever makes this root's OWN common-dir resolution fail below, i.e.
# the remap silently doesn't fire for that root; it can never produce a
# FALSE match, since the match test is exact common-dir identity, never a
# prefix/substring test.
#
# Caller contract (both hooks, round 2 revision -- Grok BLOCKER): call
# this ONLY after every existing candidate/containment check has already
# failed for FILE_PATH itself -- see each hook's own comment at its call
# site for why. A file already inside a configured root does NOT skip
# this call any more (round 1's "never reaches here" was the bug Grok
# found: it silently dropped exactly the in-root worktree case, the ONLY
# worktree location the settings-level PreToolUse matcher can ever
# invoke pre-edit-chain.sh for at all) -- callers instead gate on the
# cheap, bash-only mc_nested_worktree_gitfile FILE_PATH ROOT first for a
# FILE_PATH already under that ROOT: a `.git` FILE strictly below ROOT
# means a real nested linked worktree, worth paying this function's `git`
# call for; no such gitfile means an ordinary in-root file, and callers
# skip this call entirely, so the ordinary common case still pays zero
# extra `git` calls. A FILE_PATH NOT already under any configured root
# still calls this unconditionally, same as round 1.
#
# Detection, in order (cheapest guard first, at least one `git` process
# once a `.git` entry is confirmed to exist, one more per configured ROOT
# on the miss branch below):
#   1. FILE_PATH must be absolute and free of a literal `..` traversal
#      segment (same syntactic guard as mc_path_under_root) -- anything
#      else: not applicable, return 3, no git call.
#   2. Walk FILE_PATH up to its nearest EXISTING ancestor directory (same
#      walk as mc_path_under_root -- covers a Write of a file that does not
#      exist yet), then walk THAT up to `/` doing a bare `[ -e "$d/.git" ]`
#      check (no subprocess -- same "rule out no .git entry at all"
#      precedent hooks/ledger-post-edit.sh's own shell-diff branch already
#      uses for exactly this reason). No `.git` anywhere on the chain: this
#      ancestor is not in any git checkout at all -- not applicable, return
#      3, no git call. This is what keeps an ordinary /tmp or /mnt file (by
#      far the common shape of a genuine miss) from ever spawning git.
#   3. A `.git` entry exists somewhere above: exactly ONE `git -C <ancestor>
#      rev-parse --git-common-dir --show-toplevel` call. Either line
#      missing, or either resolves-to-absolute step below fails: git itself
#      could not settle the question -- return 2 (worktree-unresolved; the
#      caller logs a NAMED outcome instead of a silent no-match/out-of-
#      scope, per the "say something when it cannot resolve" floor -- this
#      also covers a repo whose `.git` was created via `--separate-git-dir`
#      pointing somewhere this resolution can't follow, deliberately: out
#      of scope, treated the same as "can't tell").
#   4. --git-common-dir and --show-toplevel are each resolved to an
#      absolute, symlink-safe path via `cd ... && pwd -P` (the same
#      resolution mc_path_under_root already uses for ROOT/ancestor above;
#      --git-common-dir can print a RELATIVE path -- relative to the `-C`
#      directory -- on git versions this repo doesn't pin a floor above,
#      hence the two-step `cd ancestor && cd common_raw`).
#   5. Resolved common-dir == resolved-toplevel + "/.git" (a plain STRING
#      comparison, not a second `cd` into it -- a linked worktree's own
#      `.git` is a FILE, not a directory, so relying on a `cd` failing
#      there would be an accident of the worktree case, not a real check):
#      this ancestor is an ORDINARY checkout, not a linked worktree at all
#      (editing a file that happens to live under some other, unconfigured
#      repo's own main checkout) -- not applicable, return 3. This is what
#      keeps the common "genuinely outside every configured root, and not
#      a worktree either" miss from ever being renamed away from its
#      today's plain outcome.
#   6. Otherwise this genuinely IS a linked worktree. Compute FILE_PATH's
#      physical location (ancestor_real + whatever of FILE_PATH sat past
#      the ancestor, same reconstruction mc_path_under_root's callers rely
#      on for a not-yet-existing file) and its path relative to the
#      worktree's own toplevel -- code_refs are written relative to a
#      checkout root and a worktree mirrors its main checkout's tracked
#      layout exactly, so that relative form is exactly what a main-
#      checkout edit of the same file would also be relative to.
#   7. For each ROOT: resolve ROOT's OWN common-dir via `git -C "$ROOT"
#      rev-parse --git-common-dir` (round 2 fix, Grok + Codex MAJOR --
#      `cd "$ROOT/.git"` assumed ROOT's `.git` is a directory, which is
#      false when ROOT is itself a linked worktree or a
#      `--separate-git-dir` checkout; asking git for ROOT's identity
#      instead of assuming a layout fixes both) and compare it to
#      FILE_PATH's already-resolved common-dir, by exact string identity
#      first and `-ef` filesystem identity as a fallback (never a
#      prefix/glob test -- see the STRIP_PREFIX note above for why this
#      can only fail closed). First match: print
#      "<toplevel of ROOT's own checkout>/$relative" and return 0 (for a
#      ROOT that is its own toplevel, "$ROOT/$relative"; a ROOT narrower
#      than its checkout maps to the checkout, so the caller's root test
#      then decides whether the real file is in scope). No ROOT matches at all: this worktree's main repo was
#      never wired as a code root here -- return 1 (worktree-unwired) --
#      the correctness condition the caller must not skip (docs/internal
#      handoff item 1): remapping onto an UNWIRED repo's records would be
#      a false hit against a decision that has nothing to do with it.
#
# Returns (never writes anything on any nonzero return):
#   0  remapped -- stdout carries the one-line main-checkout-equivalent
#      absolute path
#   1  worktree-unwired  -- confirmed a linked worktree; no ROOT matched
#   2  worktree-unresolved -- confirmed (or suspected, via a `.git` entry)
#      to be repo-related but git itself failed or a resolution step did
#   3  not applicable -- FILE_PATH is malformed/relative, no `.git` entry
#      exists on its ancestor chain at all, or this ancestor is an
#      ordinary (non-worktree) checkout -- caller's existing outcome is
#      unchanged, nothing new to log
mc_remap_worktree_path() {
    local file_path="$1" roots="$2"
    local ancestor probe found_git
    local cd_out common_raw toplevel_raw common_real toplevel_real
    local ancestor_real tail physical_file_path relative
    local root root_slash root_common_raw root_common_real remapped
    local root_top_raw root_top_real

    case "$file_path" in
        /*) ;;
        *) return 3 ;;
    esac
    case "$file_path" in
        */../*|*/..|../*|..) return 3 ;;
    esac

    ancestor="$(_mc_nearest_existing_ancestor "$file_path")" || return 3

    found_git=0
    probe="$ancestor"
    while :; do
        if [ -e "$probe/.git" ]; then
            found_git=1
            break
        fi
        [ "$probe" = "/" ] && break
        probe="$(dirname "$probe")"
    done
    [ "$found_git" -eq 1 ] || return 3

    # Round 2 fix (Grok MAJOR, mc-path-lib.sh:234): run WITHOUT the
    # ambient GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR -- if the process this
    # hook runs under has any of those set (e.g. a wrapper that pins them
    # to a DIFFERENT repo than the one FILE_PATH actually lives in), `git
    # -C "$ancestor" rev-parse` obeys them instead of discovering
    # $ancestor's own repo, producing a false remap onto a decision that
    # has nothing to do with FILE_PATH. A subshell with an explicit
    # `unset` (not `env -u`, whose availability on macOS's own `env` is
    # unverified here) is bash-3.2-safe and portable.
    cd_out="$(unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR; git -C "$ancestor" rev-parse --git-common-dir --show-toplevel 2>/dev/null)"
    [ -n "$cd_out" ] || return 2
    common_raw=""
    toplevel_raw=""
    {
        IFS= read -r common_raw
        IFS= read -r toplevel_raw
    } <<EOF
$cd_out
EOF
    [ -n "$common_raw" ] && [ -n "$toplevel_raw" ] || return 2

    toplevel_real="$(cd "$toplevel_raw" 2>/dev/null && pwd -P)"
    [ -n "$toplevel_real" ] || return 2
    common_real="$(cd "$ancestor" 2>/dev/null && cd "$common_raw" 2>/dev/null && pwd -P)"
    [ -n "$common_real" ] || return 2

    if [ "$common_real" = "$toplevel_real/.git" ]; then
        return 3
    fi

    ancestor_real="$(cd "$ancestor" 2>/dev/null && pwd -P)"
    [ -n "$ancestor_real" ] || return 2
    tail="${file_path#"$ancestor"}"
    physical_file_path="${ancestor_real}${tail}"

    relative="${physical_file_path#"$toplevel_real"/}"
    if [ "$relative" = "$physical_file_path" ]; then
        return 2
    fi

    # Round 2 fix (Grok + Codex MAJOR, mc-path-lib.sh:268): a configured
    # ROOT can itself be a linked worktree, or a checkout made with
    # `git init --separate-git-dir` -- either way its OWN `.git` is a
    # FILE, not a directory, so `cd "$root_slash/.git"` used to fail and
    # this root was silently skipped, never matching ANY worktree of the
    # same repo (both reviewers' second finding; Codex's own MAJOR is the
    # identical defect). Fixed by asking git for the root's *identity* --
    # its common dir -- the same way $ancestor's own identity was asked
    # above, rather than assuming a directory layout. `--git-common-dir`
    # can print a path RELATIVE to the `-C` directory (git versions this
    # repo does not pin a floor above), hence the two-step `cd
    # "$root_slash" && cd "$root_common_raw"` -- the same pattern already
    # used for $common_real above. Same env isolation as the first git
    # call, for the same reason (this root's own ambient GIT_DIR must
    # never leak in from the hook's environment either).
    while IFS= read -r root; do
        [ -n "$root" ] || continue
        root_slash="${root%/}"
        root_common_raw="$(unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR; git -C "$root_slash" rev-parse --git-common-dir 2>/dev/null)"
        [ -n "$root_common_raw" ] || continue
        root_common_real="$(cd "$root_slash" 2>/dev/null && cd "$root_common_raw" 2>/dev/null && pwd -P)"
        [ -n "$root_common_real" ] || continue
        # String identity (both sides already resolved via `pwd -P`) is
        # the primary test; `-ef` (bash 3.2-safe, works for directories)
        # is an additional filesystem-identity check for a
        # case-insensitive filesystem (macOS default) where two
        # differently-cased spellings of the SAME directory could in
        # principle still diverge as strings after resolution.
        if [ "$root_common_real" = "$common_real" ] || [ "$root_common_real" -ef "$common_real" ]; then
            # The remap target is the toplevel of the checkout that
            # CONTAINS this root, never the root itself: `relative` is
            # relative to the worktree's toplevel, so it only names the
            # right file under the matching checkout's toplevel. A root
            # narrower than its checkout (e.g. <repo>/Sources) would
            # otherwise yield <repo>/Sources/Sources/Foo.swift. When the
            # root is its own toplevel the result is unchanged.
            root_top_raw="$(unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR; git -C "$root_slash" rev-parse --show-toplevel 2>/dev/null)"
            [ -n "$root_top_raw" ] || continue
            root_top_real="$(cd "$root_top_raw" 2>/dev/null && pwd -P)"
            [ -n "$root_top_real" ] || continue
            remapped="${root_top_real}/${relative}"
            printf '%s\n' "$remapped"
            return 0
        fi
    done <<EOF
$roots
EOF

    return 1
}

# mc_remap_worktree_gated FILE_PATH ROOTS
#
# The worktree remap exactly as the search-capable hooks apply it
# (hooks/pre-edit-chain.sh, hooks/newfile-nudge.sh): ROOTS is the
# newline-separated list of configured code roots (no trailing slashes).
# Wraps mc_remap_worktree_path with the in-root gate -- a FILE_PATH already
# physically under one of the roots pays for the remap only when a nested
# `.git` FILE (a worktree checked out inside the root) sits between it and
# that root (mc_nested_worktree_gitfile), and an in-root path whose remap
# comes back unwired/unresolved is an ordinary in-root file, not a worktree
# gap. Return codes are mc_remap_worktree_path's, with that in-root
# downgrade applied:
#   0  remapped -- stdout is the main-checkout-equivalent absolute path
#   1  worktree-unwired    (FILE_PATH is NOT under any root)
#   2  worktree-unresolved (FILE_PATH is NOT under any root)
#   3  not applicable (ordinary file, or in-root with nothing to remap)
mc_remap_worktree_gated() {
    local file_path="$1" roots="$2" root in_root="" out rc
    while IFS= read -r root; do
        [ -n "$root" ] || continue
        if mc_path_under_root "$file_path" "$root"; then
            in_root="$root"
            break
        fi
    done <<EOF2
$roots
EOF2
    if [ -n "$in_root" ]; then
        mc_nested_worktree_gitfile "$file_path" "$in_root" || return 3
    fi
    out="$(mc_remap_worktree_path "$file_path" "$roots")"
    rc=$?
    case $rc in
        0) printf '%s\n' "$out"; return 0 ;;
        1 | 2) [ -n "$in_root" ] && return 3 ;;
    esac
    return $rc
}

# mc_strip_prefix_roots STRIP_PREFIXES
#
# The configured code roots a hook knows about, read from its
# colon-separated MEMCONTINUUM_STRIP_PREFIX: one root per line on stdout,
# trailing slash removed, empty entries dropped. The roots argument of
# mc_remap_worktree_gated / mc_remap_worktree_path.
mc_strip_prefix_roots() {
    local prefixes="$1" prefix root out="" parts=()
    # read -a, never an unquoted word split: a root containing * ? [ must
    # stay literal, not pathname-expand against sibling directories.
    IFS=: read -r -a parts <<< "$prefixes"
    for prefix in ${parts[@]+"${parts[@]}"}; do
        root="${prefix%/}"
        [ -z "$root" ] && continue
        out="${out:+$out
}$root"
    done
    [ -n "$out" ] && printf '%s\n' "$out"
    return 0
}
