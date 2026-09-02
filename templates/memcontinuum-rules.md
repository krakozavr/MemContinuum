<!-- memcontinuum-rules v1 — rendered by MemContinuum repo-init; do not hand-edit -->
# What goes where

MemContinuum store ({{STORE}}) — all of these written AT THE MOMENT they
happen, never later from notes:
- Decisions/rulings — a new link in topics/<area>/<topic>.md, the owner's
  exact words + source.
- Incidents — a file in incidents/.
- Rejected alternatives, "not now" rulings.

Claude Code auto-memory (~/.claude/projects/…/memory/):
- Session state, open questions not yet ruled.
- Machine facts and recovery recipes.
- One-line POINTERS to store records: `ruling: <title> → TOP-xxxx`.

Never both; never copy content across layers.

How to write a store record: docs/SCHEMA.md in the engine; lint it, then commit
the store (committing reindexes it).

A coverage or look-back nudge is an ACTION ITEM, not a notice: answer it with
records or an explicit "none".
