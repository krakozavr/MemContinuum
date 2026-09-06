#!/usr/bin/env bash
# PostToolUse hook (no settings-level matcher -- fires for EVERY tool,
# design R6, audit MC-P1-04, TOP-0123 L6): silently appends every edited
# file_path under any configured code root or the store root to this
# session's ledger ($MEMCONTINUUM_HOME/sessions/<project>/<session_id>.json).
# Never prints anything (PostToolUse additionalContext exists but this hook
# never uses it -- it is pure evidence-gathering, not a reminder point --
# docs/DESIGN.md ruling A/E). Runs identically inside a subagent
# (agent_id set): a subagent's edits are still real edits worth tracking,
# even though subagents never get injected reminders (that gate lives in
# userprompt-remind.sh / sessionstart-remind.sh, not here).
#
# Mutation-surface honesty (design R6): retrieval (pre-edit-chain.sh) and
# this ledger both only ever SAW the Edit/Write/NotebookEdit tools before
# this task -- a file changed from the shell (Bash, a script, `git mv`)
# never got a ledger row at all, and the settings-level matcher meant an
# unknown/future mutation tool was unobservable in principle, not just
# unhandled. Now:
#   - a cheap, bash-only prefilter (below, BEFORE the watchdog) exits
#     silently for the read-only built-ins -- no python, no watchdog
#     launcher, one bash start;
#   - Edit/Write/MultiEdit/NotebookEdit still ledger the tool's own
#     file_path (row `source: "tool"`), exactly as before;
#   - Bash, and any tool this hook has no dedicated branch for (an MCP
#     tool, a future built-in, or a payload with no `tool_name` at all),
#     fall through to a `git status` tree-diff against every configured
#     root (row `source: "shell-diff"`) -- best effort, computed AFTER the
#     fact, never pre-retrieved; a tool this hook cannot name is also
#     logged as `outcome=unsupported-mutation-surface` so the gap is
#     observable, not silent. Strict mutation coverage (refusing an
#     undeclared shell mutation) is NOT provided -- see docs/INTERNALS.md.
#
# Contract: read the PostToolUse JSON payload on stdin; never write anything
# under MEMCONTINUUM_ROOT or any configured code root; always exit 0
# (fail-open); never emit any stdout; append outcome line(s) to hook.log.
# Never reads the payload's own command text or the tool's captured
# response (a real payload's arbitrary shell command and its output) --
# only `tool_name` and the few already-established scalar fields
# (docs/DESIGN.md ruling B); the
# shell-diff branch below learns what actually changed from `git status`
# alone, never from the command text or its captured output.
#
# Look-back addendum (docs/DESIGN.md 2026-08-30): this is
# also the single place that advances state.last_growth_turn/last_growth_ts
# -- the "edit ledger grew" half of userprompt-remind.sh's T-thin formula.
# Growth means a genuinely NEW (path, content_sha256) pair enters the
# ledger: a brand-new path, or an existing path whose content changed.
# Re-touching a path with byte-identical content is NOT growth (matches the
# fingerprint semantics userprompt-remind.sh already uses for its own
# coverage-injection cooldown). Stamped at the ledger's own idea of "now"
# (turn = the session's current user_turn_count, which only
# userprompt-remind.sh ever advances -- this hook never bumps it).
#
# Env: see hooks/memlib.sh's own header comment (MEMCONTINUUM_HOME,
# MEMCONTINUUM_PROJECT, MEMCONTINUUM_ROOT, MEMCONTINUUM_CODE_ROOT,
# MEMCONTINUUM_CODE_ROOTS, MEMCONTINUUM_PYTHON), plus two new, optional
# ones the shell-diff branch reads directly (no default rendered by the
# installer): MEMCONTINUUM_SHELL_DIFF_BUDGET (total `git status` wall-clock
# budget across every root, seconds, default 1.2) and
# MEMCONTINUUM_SHELL_DIFF_ROOT_BUDGET (per-root cap within that total,
# default 0.8).

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Cheap prefilter (design R6): pure bash, no python, no watchdog -- must run
# BEFORE sourcing mc-watchdog.sh below, or a read-only tool call (by far the
# common case: Read/Grep/Glob/... vastly outnumber real edits in a normal
# session) pays for a whole extra python process (the watchdog launcher)
# for nothing. Reads the payload ONCE, here; the re-exec'd child (the
# watchdog-guarded path below) reads it again itself once the payload is
# piped into it -- see the re-exec line further down.
#
# Bounded to a 4096-byte PREFIX of the payload, never the whole string: a
# real PostToolUse payload can carry the tool's own captured output --
# multiple megabytes of it (e.g. a Read of a large file) -- after
# `tool_name`, and a
# full-payload bash scan was measured at 10-15 SECONDS on a 1 MB payload --
# not from the substring search itself (a single `case`/`${v%%pat}` pass
# over the full string costs tens of milliseconds even at that size) but
# from a since-removed `while` loop that used to re-strip a leading space
# one character at a time against the REST of the string on every
# iteration. `tool_name` sits near the front of every real payload shape
# (well before the tool's own input and captured output), so a bounded
# prefix costs the
# same few milliseconds regardless of total payload size; when the key is
# not found within it (a payload shape this hook has never seen), MC_TOOL_
# NAME simply stays empty and this call falls through to the full
# watchdog-guarded path below, which resolves it properly via
# mc_extract_fields -- a missed fast path costs latency, never a wrongly
# skipped mutation.
PAYLOAD="$(cat)"
MC_PREFIX="${PAYLOAD:0:4096}"
MC_TOOL_NAME=""
case "$MC_PREFIX" in
    *'"tool_name"'*)
        MC_TOOL_NAME="${MC_PREFIX#*\"tool_name\"}"
        MC_TOOL_NAME="${MC_TOOL_NAME#*:}"
        case "$MC_TOOL_NAME" in
            " "*) MC_TOOL_NAME="${MC_TOOL_NAME# }" ;;
        esac
        case "$MC_TOOL_NAME" in
            \"*)
                MC_TOOL_NAME="${MC_TOOL_NAME#\"}"
                MC_TOOL_NAME="${MC_TOOL_NAME%%\"*}"
                ;;
            *) MC_TOOL_NAME="" ;;
        esac
        ;;
esac

# Design R6: the read-only built-ins never mutate anything, so a ledger
# row (or even a shell-diff pass) for one of them is pure waste -- exit 0
# silently, no python spawned, no watchdog, no hook.log line at all (the
# absence of a line here is itself the fast path; nothing here would be
# worth logging on top of it).
case "$MC_TOOL_NAME" in
    Read|Grep|Glob|LS|NotebookRead|WebFetch|WebSearch|TodoWrite|TodoRead|\
Task|Agent|Skill|ToolSearch|AskUserQuestion|BashOutput|KillShell|\
EnterPlanMode|ExitPlanMode|ListMcpResourcesTool|ReadMcpResourceTool)
        exit 0
        ;;
esac

# Watchdog guard (macOS port, docs/DESIGN.md SS8 port note, 2026-08-30;
# deduped into hooks/mc-watchdog.sh, finding 1, 2026-08-31): must be the
# literal first thing after resolving SCRIPT_DIR and sourcing
# mc-watchdog.sh (see its own header for what
# running it costs), strictly BEFORE sourcing memlib.sh (which
# does its own mkdir -p work) -- see
# hooks/userprompt-remind.sh's test_outer_deadline_covers_memlib_sourcing
# for why this ordering matters. A tiny python launcher
# (mc-watchdog.sh's MC_WATCHDOG_LAUNCHER_PY) starts this same script as a
# child in its own process group and kills the WHOLE group on a
# wall-clock budget (2s here; see hooks/mc-watchdog.sh), so an orphaned
# grandchild (e.g. a hung python call several layers deep, or the
# launcher itself being killed -- finding 1's SIGTERM/SIGINT/atexit fix)
# cannot outlive the deadline. Every python call this script and
# memlib.sh's helpers make therefore needs no timeout of its own -- this
# one watchdog bounds the entire run. Always exits 0; stdin/stdout/stderr
# are the real, inherited file descriptors (never piped through python),
# so passthrough is unbuffered. If MEMCONTINUUM_PYTHON (or the venv
# fallback) does not resolve to an executable, or mc-watchdog.sh failed
# to source (MC_WATCHDOG_LAUNCHER_PY unset), this falls through
# UNGUARDED instead of exec-ing a dead path -- memlib.sh's own "no python
# resolved" detection then fires exactly as it would with no guard at
# all.
# shellcheck source=mc-watchdog.sh
source "${MC_WATCHDOG_LIB_PATH:-$SCRIPT_DIR/mc-watchdog.sh}" 2>/dev/null
if [ -z "${MC_UNDER_TIMEOUT:-}" ]; then
    export MC_UNDER_TIMEOUT=1
    # MC_GUARD_PY is set by mc-watchdog.sh above (F6 fix, round 4: env ->
    # config.sh -> engine venv, same order memlib.sh uses for MC_PY).
    if [ -x "${MC_GUARD_PY:-}" ] && [ -n "${MC_WATCHDOG_LAUNCHER_PY:-}" ]; then
        # The payload was already consumed by the prefilter above -- pipe
        # it back in so the guarded child's own `PAYLOAD="$(cat)"` below
        # sees the exact same bytes. The launcher's own child process
        # (mc-watchdog.sh's subprocess.Popen) inherits ITS stdin with no
        # redirect of its own, so this pipe reaches the re-exec'd script
        # unchanged.
        printf '%s' "$PAYLOAD" | "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "${BASH:-bash}" "${BASH_SOURCE[0]}" "$@"
        exit 0
    fi
fi

# shellcheck source=memlib.sh
source "$SCRIPT_DIR/memlib.sh"

START_TS=$(date +%s 2>/dev/null || echo 0)

finish() {
    local outcome="$1"
    local now elapsed
    now=$(date +%s 2>/dev/null || echo "$START_TS")
    elapsed=$(( now - START_TS ))
    mc_log "ledger outcome=$outcome elapsed=${elapsed}s session=${SESSION_ID:-} file=${FILE_PATH:-}"
    exit 0
}

# PAYLOAD was already read once, at the very top of this script, by the
# prefilter -- the SAME variable is reused here (never a second `cat`):
# the guarded child is a brand-new process (re-exec'd via the watchdog
# launcher, payload piped into ITS stdin), so its own top-of-script read
# already consumed exactly this run's payload; a second read here would
# find stdin already at EOF and silently see an empty string instead.
[ -z "$PAYLOAD" ] && finish "empty-payload"

eval "$(mc_extract_fields "$PAYLOAD" session_id tool_input.file_path tool_input.notebook_path tool_name agent_id)" 2>/dev/null

[ -z "${SESSION_ID:-}" ] && finish "no-session-id"

# Design R6 (audit MC-P1-04, TOP-0123 L6): branch on the tool name itself
# now that a settings-level matcher can no longer do this filtering.
MC_SOURCE_TAG=""
case "${TOOL_NAME:-}" in
    Edit|Write|MultiEdit)
        [ -z "${FILE_PATH:-}" ] && finish "no-file-path"
        MC_SOURCE_TAG="tool"
        ;;
    NotebookEdit)
        # A NotebookEdit payload carries notebook_path, not file_path --
        # fall back to it only when file_path is genuinely absent (a
        # future Claude Code version that starts sending both keeps using
        # file_path, unchanged).
        if [ -z "${FILE_PATH:-}" ]; then
            FILE_PATH="${NOTEBOOK_PATH:-}"
        fi
        [ -z "${FILE_PATH:-}" ] && finish "no-file-path"
        MC_SOURCE_TAG="tool"
        ;;
    Bash)
        MC_SOURCE_TAG="shell-diff"
        ;;
    "")
        mc_log "ledger outcome=unsupported-mutation-surface tool=unknown session=${SESSION_ID:-}"
        MC_SOURCE_TAG="shell-diff"
        ;;
    *)
        # Any other tool name (an MCP tool, a future built-in) -- best
        # effort: named as unsupported AND still caught by the tree diff
        # below if it actually mutated something.
        mc_log "ledger outcome=unsupported-mutation-surface tool=${TOOL_NAME} session=${SESSION_ID:-}"
        MC_SOURCE_TAG="shell-diff"
        ;;
esac

if [ "$MC_SOURCE_TAG" = "shell-diff" ]; then
    # ---- Bash / unknown-tool branch: diff every configured root's tree
    # against its own lazily-established baseline (design R6). ------------
    STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"
    CODE_ROOTS_TEXT="$(mc_code_roots)"

    export MC_CODE_ROOTS_LINES="$CODE_ROOTS_TEXT"
    export MC_SESSION_ID="$SESSION_ID"
    export MC_PROJECT_ENV="$MC_PROJECT"
    export MC_NOW="$(date +%s 2>/dev/null || echo 0)"
    export MC_LOG_PATH="$MC_LOG"

    mc_update_state_json "$STATE_FILE" '
import hashlib, os, subprocess, time


def now_ts():
    import datetime
    try:
        return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
    except Exception:
        return ""


def sha_of(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return ""


project = os.environ.get("MC_PROJECT_ENV") or ""
session_id = os.environ.get("MC_SESSION_ID") or ""
log_path = os.environ.get("MC_LOG_PATH") or ""


def log_line(text):
    if not log_path:
        return
    try:
        with open(log_path, "a") as fh:
            fh.write(now_ts() + " " + text + " project=" + project + "\n")
    except OSError:
        pass


try:
    now = float(os.environ.get("MC_NOW") or time.time())
except Exception:
    now = time.time()

roots = []
for _line in (os.environ.get("MC_CODE_ROOTS_LINES") or "").splitlines():
    if _line:
        roots.append((_line, "code"))
store_root = os.environ.get("MEMCONTINUUM_ROOT") or ""
if store_root:
    roots.append((store_root, "store"))

try:
    total_budget = float(os.environ.get("MEMCONTINUUM_SHELL_DIFF_BUDGET") or "1.2")
except Exception:
    total_budget = 1.2
try:
    per_root_budget = float(os.environ.get("MEMCONTINUUM_SHELL_DIFF_ROOT_BUDGET") or "0.8")
except Exception:
    per_root_budget = 0.8

start_all = time.monotonic()
appended = 0
timeouts = 0
non_git = 0
baseline_too_large = 0

shell_baseline = state.get("shell_baseline")
if not isinstance(shell_baseline, dict):
    shell_baseline = {}
state["shell_baseline"] = shell_baseline

ledger = state.setdefault("ledger", [])
existing_pairs = set()
for _e in ledger:
    existing_pairs.add((_e.get("path"), _e.get("content_sha256")))

for root, kind in roots:
    # os.path.exists, not isdir: a git WORKTREE has a ".git" FILE (a
    # "gitdir: <path>" pointer), not a directory -- mc_git_head elsewhere
    # in this codebase already delegates repo-ness entirely to git itself
    # (git -C dir rev-parse HEAD) for exactly this reason; this check only
    # needs to rule out "no .git entry at all" before spending a
    # subprocess on it.
    if not os.path.exists(os.path.join(root, ".git")):
        non_git += 1
        continue

    remaining = total_budget - (time.monotonic() - start_all)
    if remaining <= 0:
        timeouts += 1
        continue

    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", root, "status", "--porcelain", "-z",
             "--untracked-files=all"],
            capture_output=True, text=True, timeout=min(per_root_budget, remaining),
        )
    except (subprocess.TimeoutExpired, OSError):
        timeouts += 1
        continue
    if result.returncode != 0:
        timeouts += 1
        continue

    entries = result.stdout.split("\x00")
    if entries and entries[-1] == "":
        entries.pop()
    dirty = []
    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        idx += 1
        if len(entry) < 4:
            continue
        xy = entry[:2]
        path = entry[3:]
        dirty.append(path)
        if "R" in xy or "C" in xy:
            if idx < len(entries):
                old_path = entries[idx]
                idx += 1
                if old_path:
                    dirty.append(old_path)
    dirty = sorted(set(dirty))

    if root not in shell_baseline:
        # First successful status for this root (or a retry after an
        # earlier timeout, which never touched shell_baseline at all):
        # establish the baseline, append nothing yet.
        if len(dirty) > 500:
            shell_baseline[root] = None
            baseline_too_large += 1
        else:
            baseline_map = {}
            budget_hit = False
            for p in dirty:
                if time.monotonic() - start_all > total_budget:
                    # Hashing every dirty path can itself run long on a
                    # large tree. Do not leave a partial baseline behind:
                    # treat this root as a timeout so the next call
                    # retries the baseline from scratch instead of
                    # silently comparing against an incomplete map.
                    budget_hit = True
                    break
                full = os.path.normpath(os.path.join(root, p))
                baseline_map[p] = sha_of(full)
            if budget_hit:
                timeouts += 1
                continue
            shell_baseline[root] = baseline_map
        continue

    root_baseline = shell_baseline[root]
    if root_baseline is None:
        # A permanently-too-large root: skipped every call, never retried
        # (unlike a timeout, which leaves no baseline key at all).
        baseline_too_large += 1
        continue
    if not isinstance(root_baseline, dict):
        root_baseline = {}
        shell_baseline[root] = root_baseline

    for p in dirty:
        full = os.path.normpath(os.path.join(root, p))
        sha = sha_of(full)
        if root_baseline.get(p) == sha:
            continue
        pair = (full, sha)
        if pair in existing_pairs:
            continue
        ledger.append({
            "path": full,
            "kind": kind,
            "content_sha256": sha,
            "seen_at": now,
            "root": root if kind == "code" else "",
            "source": "shell-diff",
        })
        existing_pairs.add(pair)
        appended += 1
        state["last_growth_turn"] = state.get("user_turn_count", 0)
        state["last_growth_ts"] = now
        log_line("ledger outcome=appended kind=" + kind + " source=shell-diff file=" + full)

state["session_id"] = session_id or state.get("session_id")
state["project"] = project or state.get("project")
state.setdefault("user_turn_count", 0)
state.setdefault("last_inject_turn", -999)
state.setdefault("last_inject_time", 0)
state.setdefault("last_inject_ts", 0)
state.setdefault("last_injected_pairs", [])
state.setdefault("last_growth_turn", 0)
state.setdefault("lookback_count", 0)

log_line(
    "ledger outcome=shell-diff appended=" + str(appended) + " roots=" + str(len(roots))
    + " timeouts=" + str(timeouts) + " non-git=" + str(non_git)
    + " baseline-too-large=" + str(baseline_too_large)
)

print(json.dumps(state))
'
    RC=$?
    if [ $RC -eq 0 ]; then
        exit 0
    fi
    finish "update-failed rc=$RC"
fi

# ---- Edit / Write / MultiEdit / NotebookEdit branch: today's path
#      (row source: "tool"). ---------------------------------------------

# Symlink-safe containment (mc_path_under_root, hooks/memlib.sh -- shared
# with hooks/newfile-nudge.sh's own 2026-08-31 fix for this same class): a
# plain lexical prefix match here is fooled by a literal `/../` traversal
# segment or a symlinked ancestor directory the same way newfile-nudge.sh's
# was, silently classifying a real edit as out-of-scope and losing the
# growth signal (last_growth_turn/last_growth_ts) this hook alone advances.
#
# Ruling 131 (coordinator, folded from task-7-review.md NIT-1): checked
# against EVERY configured code root (mc_code_roots, one python spawn
# regardless of root count) -- the LONGEST matching root wins for
# kind=code (nested roots: the innermost/most-specific one), the SAME rule
# memidx.py's `unmapped --code-root` uses (`_unmapped_best_root`), so the
# ledger's own `root` annotation always names the same root a later
# `unmapped` classification would use for the same path. The store root
# check stays a single check.
UNDER_CODE=0
UNDER_STORE=0
MC_MATCHED_ROOT=""
while IFS= read -r ROOT_CANDIDATE; do
    [ -n "$ROOT_CANDIDATE" ] || continue
    mc_path_under_root "$FILE_PATH" "$ROOT_CANDIDATE"
    if [ $? -eq 0 ]; then
        UNDER_CODE=1
        if [ -z "$MC_MATCHED_ROOT" ] || [ "${#ROOT_CANDIDATE}" -gt "${#MC_MATCHED_ROOT}" ]; then
            MC_MATCHED_ROOT="$ROOT_CANDIDATE"
        fi
    fi
done < <(mc_code_roots)
if [ -n "${MEMCONTINUUM_ROOT:-}" ]; then
    mc_path_under_root "$FILE_PATH" "$MEMCONTINUUM_ROOT"
    [ $? -eq 0 ] && UNDER_STORE=1
fi

if [ "$UNDER_CODE" -eq 0 ] && [ "$UNDER_STORE" -eq 0 ]; then
    finish "out-of-scope"
fi

KIND="code"
[ "$UNDER_STORE" -eq 1 ] && KIND="store"

STATE_FILE="$(mc_state_file_for "$MC_PROJECT" "$SESSION_ID")"

export MC_FILE_PATH="$FILE_PATH"
export MC_KIND="$KIND"
export MC_ROOT="$MC_MATCHED_ROOT"
export MC_SESSION_ID="$SESSION_ID"
export MC_PROJECT_ENV="$MC_PROJECT"
export MC_NOW="$(date +%s 2>/dev/null || echo 0)"

mc_update_state_json "$STATE_FILE" '
import hashlib, os, time

path = os.environ.get("MC_FILE_PATH", "")
kind = os.environ.get("MC_KIND", "code")
# Design R5 (audit MC-P1-05, TOP-0123 L5): the physical root this path
# matched, "" for a store row (MC_ROOT is only ever set when the code-root
# containment loop found a match, and a store-kind row must not carry a
# code root even if one also happened to match).
root = os.environ.get("MC_ROOT", "") if kind == "code" else ""
try:
    now = float(os.environ.get("MC_NOW") or time.time())
except Exception:
    now = time.time()
if path:
    try:
        with open(path, "rb") as f:
            content_sha = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        content_sha = ""

    ledger = state.setdefault("ledger", [])
    found = False
    previous_sha = None
    for entry in ledger:
        if entry.get("path") == path:
            found = True
            previous_sha = entry.get("content_sha256")
            entry["kind"] = kind
            entry["content_sha256"] = content_sha
            entry["seen_at"] = now
            entry["root"] = root
            entry["source"] = "tool"
            break
    if not found:
        ledger.append({
            "path": path,
            "kind": kind,
            "content_sha256": content_sha,
            "seen_at": now,
            "root": root,
            "source": "tool",
        })

    is_new_pair = (not found) or (previous_sha != content_sha)
    if is_new_pair:
        state["last_growth_turn"] = state.get("user_turn_count", 0)
        state["last_growth_ts"] = now

    state["session_id"] = os.environ.get("MC_SESSION_ID") or state.get("session_id")
    state["project"] = os.environ.get("MC_PROJECT_ENV") or state.get("project")
    state.setdefault("user_turn_count", 0)
    state.setdefault("last_inject_turn", -999)
    state.setdefault("last_inject_time", 0)
    state.setdefault("last_inject_ts", 0)
    state.setdefault("last_injected_pairs", [])
    state.setdefault("last_growth_turn", 0)
    state.setdefault("lookback_count", 0)

print(json.dumps(state))
'
RC=$?

if [ $RC -eq 0 ]; then
    finish "appended kind=$KIND"
else
    finish "update-failed rc=$RC"
fi
