"""The coverage sentence, built in one place (TOP-0137 L29), and the strict
foreign-project check (foreign_verdict) of the nested-projects detector.

Imported by the inline python of hooks/userprompt-remind.sh and
hooks/sessionstart-remind.sh. Stdlib only and no memidx import: the prompt hook
is the hot path and `import memidx` costs ~100-170 ms there.
"""
import json
import math
import os
import sys


def yn(v):
    """yes / no, and `unknown` for None (a compact with no recorded HEAD move)."""
    if v is None:
        return "unknown"
    return "yes" if v else "no"


def coverage_fact(unmapped, coverage_status, code_changed, store_changed):
    """`(fact_line, has_evidence)`. `unmapped` already excludes git-ignored paths
    (`memidx.py unmapped` reports those under `ignored`)."""
    if coverage_status != "ok":
        fact_line = (
            "Coverage signal — decision-topic coverage unknown "
            f"(store index {coverage_status}); code HEAD changed: {yn(code_changed)}; "
            f"store HEAD changed: {yn(store_changed)}"
        )
        return fact_line, bool(code_changed) or bool(store_changed)
    n = len(unmapped)
    shown = unmapped[:8]
    extra = n - len(shown)
    paths_line = ", ".join(shown) if shown else "(none)"
    if extra > 0:
        paths_line += f", +{extra}"
    fact_line = (
        f"Coverage signal — {n} edited file(s) with no decision topic: {paths_line}; "
        f"code HEAD changed: {yn(code_changed)}; store HEAD changed: {yn(store_changed)}"
    )
    return fact_line, (n > 0) or bool(code_changed) or bool(store_changed)


def coverage_question(store_root, project):
    return (
        "Any ruling, incident, or rejected alternative from this session that "
        "the MemContinuum store of project " + project + " should hold? This "
        "asks about " + project + " work only. Store: " + store_root + " — a ruling "
        "is a new link in topics/<area>/<topic>.md, an incident is a file in "
        "incidents/ (see docs/SCHEMA.md); NOT Claude Code auto-memory. If none, "
        "say so once."
    )


def lookback_question(store_root, project):
    q = chr(39)
    return (
        "Did the conversation since then establish any ruling, incident, "
        "rejected alternative, priority, wording choice, money decision, or "
        + q + "not now" + q + " that the MemContinuum store of project " + project
        + " should hold? This asks about " + project + " work only. Store: "
        + store_root + " — a ruling is a new link in topics/<area>/<topic>.md, "
        "an incident is a file in incidents/ (see docs/SCHEMA.md); NOT Claude "
        "Code auto-memory. If none, say so once."
    )


def session_initialised(state_path, session_id):
    """True only when the state file at STATE_PATH is what a session-start hook
    writes for SESSION_ID (hooks/sessionstart-remind.sh): a JSON object with a
    top-level non-empty `start_store_sha` string and a top-level numeric
    `created_at`, and, when it records a `session_id`, that id equals SESSION_ID.
    A file the ledger hook made, truncated JSON, the keys nested elsewhere or
    another session's state are all False."""
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict) or not session_id:
        return False
    sha = state.get("start_store_sha")
    created = state.get("created_at")
    if not isinstance(sha, str) or not sha:
        return False
    if isinstance(created, bool) or not isinstance(created, (int, float)) or not math.isfinite(created) or created <= 0:
        return False
    if "session_id" in state and state["session_id"] != session_id:
        return False
    return True


def foreign_verdict(wired_dir, own_project, candidate, basenames, state_path, session_id):
    """`foreign-present`, `foreign-absent` or `unknown` for the nearest wired
    directory WIRED_DIR found by the shell walk (hooks/memlib.sh,
    mc_foreign_cwd_nearest). Foreign only when both settings files of that
    directory parse and name exactly ONE project, it is CANDIDATE (the shell's
    reading, whose state file STATE_PATH is) and it is not OWN_PROJECT; the
    first word then says whether that project's session-start initialised
    SESSION_ID. Anything else is `unknown`."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
    try:
        from mc_settings_merge import wired_projects
        projects = wired_projects(
            [os.path.join(wired_dir, ".claude", "settings.local.json"),
             os.path.join(wired_dir, ".claude", "settings.json")],
            basenames.split(),
        )
    except (ImportError, ValueError, OSError):
        return "unknown"
    if projects != {candidate} or candidate == own_project:
        return "unknown"
    return "foreign-present" if session_initialised(state_path, session_id) else "foreign-absent"
