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
#     so it must cost near-nothing and depend on nothing. Pure bash + git,
#     bash 3.2 compatible (no `timeout`, no `flock`, no bash-4 syntax).
#   * No logging by default. A silent no-op in an unrelated repo should leave
#     no trace; set $MEMCONTINUUM_DETECT_LOG=1 to trace decisions into
#     hook.log while debugging.
#
# Fails open, always exit 0: any error, any missing tool, any unparseable
# payload means "say nothing", never "block the session".
#
# States, in the order they are checked (first match wins, all but the last
# are silent):
#
#   not-a-repo   cwd is not inside a git working tree -- nothing to wire.
#   opted-out    $MEMCONTINUUM_HOME/no-ask exists -- the machine-wide "never
#                ask me in any repo" switch.
#   wired        the repo's own .claude settings already reference one of the
#                installed hook scripts -- MemContinuum serves this repo.
#   decided      the repo's key appears in $MEMCONTINUUM_HOME/decisions.tsv --
#                a human already answered, either way. Never ask twice.
#   undecided    none of the above -> emit ONE additionalContext line.
#
# Repo key: the `origin` remote URL when there is one, else the working
# tree's absolute path. Remote-keyed on purpose -- a path key silently
# evaporates the moment a repo is moved on disk, and then a settled decision
# looks unmade. On a path-key miss after a move the repo simply reads as
# undecided and the question is asked once more, which is the honest failure
# direction: re-ask, never assume.

set -u

exec 2>/dev/null

MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"
DECISIONS="$MEMCONTINUUM_HOME/decisions.tsv"
NO_ASK="$MEMCONTINUUM_HOME/no-ask"

# "Wired" means the five ALWAYS-wired write-side hooks appear on "command"
# lines of the repo's .claude settings -- same rule as memcontinuum-decide.sh
# and -state.sh. The two PreToolUse hooks (pre-edit-chain, newfile-nudge) are
# deliberately absent: a rationale-only install omits them, and a stale
# PreToolUse-only fragment must not silence the detector (regate finding 4 --
# an earlier version of this edit no-opped because its match text had been
# rewritten by an unrelated rename sweep; hence the assert-style comment).
HOOK_BASENAMES="ledger-post-edit.sh precompact-persist.sh sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh"

detect_log() {
    [ -n "${MEMCONTINUUM_DETECT_LOG:-}" ] || return 0
    mkdir -p "$MEMCONTINUUM_HOME" 2>/dev/null
    printf '%s detect: %s\n' "$(date -Iseconds 2>/dev/null || date)" "$1" \
        >>"$MEMCONTINUUM_HOME/hook.log" 2>/dev/null
}

quiet_exit() {
    detect_log "$1"
    exit 0
}

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

command -v git >/dev/null 2>&1 || quiet_exit "no-git"
REPO="$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null)"
[ -n "$REPO" ] || quiet_exit "not-a-repo"

[ -f "$NO_ASK" ] && quiet_exit "opted-out-globally"

for settings in "$REPO/.claude/settings.local.json" "$REPO/.claude/settings.json"; do
    [ -f "$settings" ] || continue
    for base in $HOOK_BASENAMES; do
        if grep '"command"' "$settings" 2>/dev/null | grep -qF "$base"; then
            quiet_exit "wired repo=$REPO"
        fi
    done
done

REMOTE="$(git -C "$REPO" config --get remote.origin.url 2>/dev/null)"
if [ -n "$REMOTE" ]; then
    KEY="$REMOTE"
else
    KEY="$REPO"
fi
# The registry is line-oriented TSV; a key containing a tab or newline (legal
# in both paths and git config values) must not be able to fake columns or
# extra rows. Same mapping in memcontinuum-decide.sh/-state.sh -- all three
# must agree or a sanitized key never matches its row.
KEY="$(printf '%s' "$KEY" | tr '\t\n' '__')"

if [ -f "$DECISIONS" ]; then
    # Exact match on the first TAB-separated field only -- a substring match
    # would let one repo's key mask another's.
    while IFS="$(printf '\t')" read -r k rest; do
        case "$k" in
            \#*|"") continue ;;
        esac
        if [ "$k" = "$KEY" ]; then
            quiet_exit "decided key=$KEY"
        fi
    done < "$DECISIONS"
fi

detect_log "undecided key=$KEY"

# One fact line and one question, matching the wording rule the other hooks
# follow: state what is true, ask once, never issue an imperative and never
# act. The assistant asks the human; the `memcontinuum` skill records whatever
# they answer. No path or URL is interpolated into this string -- it is a
# fixed ASCII literal, so it needs no JSON escaping.
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"MemContinuum is installed on this machine but no decision has been recorded for this repository: it is neither wired nor declined. Ask the user, once and plainly, whether this repo should keep a decision store (MemContinuum). Do not run anything before they answer. On any answer -- yes or no -- invoke the memcontinuum skill, which is what initializes the repo or records the decline so this is never asked again. If they want to think about it, say nothing further this session."}}'
exit 0
