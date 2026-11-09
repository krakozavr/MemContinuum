"""The coverage sentence, built in one place (TOP-0137 L29).

Imported by the inline python of hooks/userprompt-remind.sh and
hooks/sessionstart-remind.sh. Stdlib only and no memidx import: the prompt hook
is the hot path and `import memidx` costs ~100-170 ms there.
"""


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


def coverage_question(store_root):
    return (
        "Any ruling, incident, or rejected alternative from this session that "
        "the MemContinuum store should hold? Store: " + store_root + " — a ruling "
        "is a new link in topics/<area>/<topic>.md, an incident is a file in "
        "incidents/ (see docs/SCHEMA.md); NOT Claude Code auto-memory. If none, "
        "say so once."
    )
