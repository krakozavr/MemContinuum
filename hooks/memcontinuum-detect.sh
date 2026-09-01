#!/usr/bin/env bash
# SessionStart hook, USER level (~/.claude/settings.json) -- the only
# MemContinuum hook that runs in repositories which have never been
# initialized. Its whole job: notice a repo where the decision "use
# MemContinuum here?" has not been made, and say so ONCE, so the assistant can
# ask. It never installs anything and never records a decision -- a hook must
# not write down a consent it did not collect. Both of those belong to the
# `memcontinuum` skill, which runs only after a human answers.
#
# Deliberately unlike every other hook in this directory:
#
#   * No python, no memlib.sh, no watchdog. This fires on EVERY session start
#     on the machine, including repos that have nothing to do with this tool,
#     so it must cost near-nothing and depend on nothing but git and the pure-
#     bash scripts/mc-registry-lib.sh sibling. Bash 3.2 compatible (no
#     `timeout`, no `flock`, no bash-4 syntax).
#   * No logging by default. A silent no-op in an unrelated repo should leave
#     no trace; set $MEMCONTINUUM_DETECT_LOG=1 to trace decisions into
#     hook.log while debugging.
#
# Fails open, always exit 0: any error, any missing tool, any unparseable
# payload -- including a missing/broken mc-registry-lib.sh -- means "say
# nothing", never "block the session".
#
# States, in the order they are checked (first match wins, all but the last
# are silent):
#
#   not-a-repo   cwd is not inside a git working tree -- nothing to wire.
#   opted-out    $MEMCONTINUUM_HOME/no-ask exists -- the machine-wide "never
#                ask me in any repo" switch.
#   decided      the repo's key appears in $MEMCONTINUUM_HOME/decisions.tsv --
#                a human already answered, either way. Never ask twice,
#                regardless of what the repo's current wiring looks like: the
#                recorded answer is the source of truth, the wiring scan
#                below is diagnostic only (fix-round-4 F5).
#   wired-full   no recorded row, but the repo's own .claude settings already
#                reference ALL FIVE always-wired write-side hooks --
#                grandfathered installs that predate this registry.
#   undecided    none of the above (including a PARTIAL wiring match: some
#                but not all five hooks present is the repair path, and
#                silence there would leave a half-wired repo with no route
#                back to health) -> emit ONE additionalContext line.
#
# Repo key: the `origin` remote URL when there is one, else the working
# tree's absolute path. Remote-keyed on purpose -- a path key silently
# evaporates the moment a repo is moved on disk, and then a settled decision
# looks unmade. On a path-key miss after a move the repo simply reads as
# undecided and the question is asked once more, which is the honest failure
# direction: re-ask, never assume.

set -u

exec 2>/dev/null

# detect_log falls back to the fixed default inline, in a LOCAL, rather than
# defaulting the real $MEMCONTINUUM_HOME up front: mc_resolve_home below (F7)
# distinguishes "unset" (check the pointer config) from "already resolved"
# purely by whether $MEMCONTINUUM_HOME is non-empty, so pre-seeding it here
# would make every custom-HOME machine look already-resolved to the fixed
# default and silently skip the pointer.
detect_log() {
    [ -n "${MEMCONTINUUM_DETECT_LOG:-}" ] || return 0
    local home="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
    mkdir -p "$home" 2>/dev/null
    printf '%s detect: %s\n' "$(date -Iseconds 2>/dev/null || date)" "$1" \
        >>"$home/hook.log" 2>/dev/null
}

quiet_exit() {
    detect_log "$1"
    exit 0
}

# The engine dir is wherever THIS script lives, one level up from hooks/ --
# same layout the installed hook line always ships (hooks/ and scripts/ are
# checkout siblings). If the checkout has moved or lost the lib, fail open:
# that is the existing posture for every other error in this file.
MC_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
[ -n "$MC_SELF_DIR" ] || quiet_exit "no-self-dir"
# shellcheck source=./mc-registry-lib.sh
. "$MC_SELF_DIR/../scripts/mc-registry-lib.sh" 2>/dev/null || quiet_exit "lib-missing"

# F7: resolve MEMCONTINUUM_HOME the same way state.sh/decide.sh do (env ->
# pointer at the fixed default path -> fixed default) now that the installed
# hook line no longer bakes it in literally. Cheap (one optional file read),
# and must happen before the no-ask/git work below since NO_ASK and DECISIONS
# depend on the final value.
mc_resolve_home
DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"
NO_ASK="$MEMCONTINUUM_HOME/no-ask"

# Interactive/no-stdin invocation must never hang -- and neither may a pipe
# whose writer stalls or never closes: this hook has no watchdog, so it
# bounds its own read. `read -t 2` (bash 3.2 supports -t) gives up after 2s
# of idle; the size cap stops an absurd payload from buffering unbounded.
[ -t 0 ] && exit 0

# Chunked: `read -n 4096` returns every 4KB (or at a newline/EOF), so the cap
# is enforced BETWEEN chunks -- a plain line-oriented read would buffer a
# delimiter-free payload in full before the cap could ever run (regate
# finding 3, demonstrated with 70KB of unbroken JSON).
PAYLOAD=""
while IFS= read -r -t 2 -n 4096 MC_CHUNK; do
    PAYLOAD="$PAYLOAD$MC_CHUNK"
    [ "${#PAYLOAD}" -gt 65536 ] && quiet_exit "payload-too-large"
done
PAYLOAD="$PAYLOAD${MC_CHUNK:-}"
[ "${#PAYLOAD}" -gt 65536 ] && quiet_exit "payload-too-large"

# Cheap shape check, not a JSON parser (bash has none): the payload must at
# least BE a JSON object before field extraction is trusted. This rejects
# arbitrary text that merely contains field-shaped fragments. Residual and
# accepted: a real JSON object with those keys nested in the wrong place
# still passes -- full validation would need python, which this hook
# deliberately avoids.
case "$PAYLOAD" in
    "{"*) ;;
    *) quiet_exit "payload-not-an-object" ;;
esac

# Field extraction without a JSON parser: `grep -o` emits every match in
# order, `head -1` keeps the FIRST -- consistent between compact and
# pretty-printed JSON (a bare greedy-sed `.*` prefix would be last-wins on
# compact input and first-wins pretty-printed). Values here are a filesystem
# path and a short enum; an escaped quote inside one still truncates the
# value, which then fails the -d / case checks below and stays silent.
json_str_field() {
    printf '%s' "$PAYLOAD" \
        | grep -o '"'"$1"'"[[:space:]]*:[[:space:]]*"[^"]*"' 2>/dev/null \
        | head -1 \
        | sed 's/^"[^"]*"[[:space:]]*:[[:space:]]*"\(.*\)"$/\1/'
}

# Both fields must actually parse. No defaults: a truncated or junk payload
# (empty stdin, "not json", "{}") must mean SILENCE, not "classify whatever
# $PWD happens to be" -- a wrong-cwd fallback could re-ask in a repo the
# human already settled.
SOURCE="$(json_str_field source)"
CWD="$(json_str_field cwd)"
[ -n "$SOURCE" ] && [ -n "$CWD" ] || quiet_exit "payload-unparsed"

# Only a fresh start. A resume or a post-compact restart is the same session
# continuing; re-asking there would be the nag this design exists to avoid.
case "$SOURCE" in
    startup) ;;
    *) quiet_exit "source-not-handled source=$SOURCE" ;;
esac

[ -d "$CWD" ] || quiet_exit "cwd-missing"

# Cheapest possible check first, before any git invocation: a stat, not a
# fork+exec. Most sessions on this machine are NOT in a repo mid-decision, so
# this ordering matters for the common case, not just the opted-out one.
[ -f "$NO_ASK" ] && quiet_exit "opted-out-globally"

if ! mc_repo_key "$CWD"; then
    quiet_exit "not-a-repo"
fi
REPO="$MC_REPO"
KEY="$MC_REPO_KEY"

# A recorded row is authoritative regardless of current wiring (F5): never
# re-ask a repo a human already answered, even if its wiring later broke.
if mc_registry_lookup "$DECISIONS" "$KEY"; then
    quiet_exit "decided key=$KEY decision=$MC_LOOKUP_DECISION"
fi

# No row. Grandfather a fully-wired repo (installs that predate this
# registry) but ask about a partial one -- partial is the repair path, and
# silence there would leave a half-wired repo with no route back to health.
mc_wiring_scan "$REPO/.claude/settings.local.json" "$REPO/.claude/settings.json"
[ "$MC_WIRING" = "full" ] && quiet_exit "wired-full-no-row repo=$REPO"

detect_log "undecided key=$KEY wiring=$MC_WIRING"

# One fact line and one question, matching the wording rule the other hooks
# follow: state what is true, ask once, never issue an imperative and never
# act. The assistant asks the human; the `memcontinuum` skill records whatever
# they answer. No path or URL is interpolated into this string -- it is a
# fixed ASCII literal, so it needs no JSON escaping.
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"MemContinuum is installed on this machine but no decision has been recorded for this repository: it is neither wired nor declined. Ask the user, once and plainly, whether this repo should keep a decision store (MemContinuum). Do not run anything before they answer. On any answer -- yes or no -- invoke the memcontinuum skill, which is what initializes the repo or records the decline so this is never asked again. If they want to think about it, say nothing further this session."}}'
exit 0
