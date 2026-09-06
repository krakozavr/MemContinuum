<!-- memcontinuum-rules v1 — rendered by MemContinuum repo-init; do not hand-edit -->
# What goes where

MemContinuum store ({{STORE}}) — write these as they happen, or at the
latest when a coverage/look-back nudge asks; never deferred to notes for a
later cleanup pass:
- Decisions/rulings — a new link in topics/<area>/<topic>.md, tagged with
  its authority label (owner-verbatim / owner-ratified / agent-inference /
  reviewer-finding / code-derived — see docs/SCHEMA.md) and a source. Only
  owner-verbatim/owner-ratified may quote the owner's exact words.
- Incidents — a file in incidents/.
- Rejected alternatives, "not now" rulings.

Claude Code auto-memory (~/.claude/projects/…/memory/):
- Session state, open questions not yet ruled.
- Machine facts and recovery recipes.
- One-line POINTERS to store records: `ruling: <title> → TOP-xxxx`.

Never both; never copy content across layers.

How to write a store record: docs/SCHEMA.md in the engine; lint it, then commit
the store (committing reindexes it). Constraint and hold links may be mirrored
by a `decision: TOP-xxxx Ln` comment at the bound symbol; memlint checks both
ways. Name the decision in commit messages too (`TOP-xxxx Ln`).

A coverage or look-back nudge is an ACTION ITEM, not a notice: answer it with
records or an explicit "none".
