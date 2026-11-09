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

A decision must still be true tomorrow. A take approval, a progress note, today's
status or a step completed is session state, not a ruling; it goes to auto-memory
or nowhere.

Claude Code auto-memory (~/.claude/projects/…/memory/):
- Session state, open questions not yet ruled.
- Machine facts and recovery recipes.
- One-line POINTERS to store records: `ruling: <title> → TOP-xxxx`.

Never both; never copy content across layers.

How to write a store record: `memidx.py record|append|promote` (shape: docs/SCHEMA.md in
the engine); it prints the diff, you commit the store (committing reindexes it). Constraint and
hold links may be mirrored by a `decision: TOP-xxxx Ln` comment at the bound symbol;
memlint checks both ways. Name the decision in commit messages too (`TOP-xxxx Ln`).

A brief for a reviewer or a dispatched agent opens with the decision records, not a
summary of them: paste the output of `memidx.py context-pack --project {{PROJECT}} <files the brief touches>`
(engine README, "context pack") at its top; the closing `pack sha256` line names what
the reader saw.

A brief for a dispatched agent says "find the existing implementation and name it
(code-search first)"; a gate prompt asks "did this duplicate anything?"; both
answers go in the gate record.

A coverage or look-back nudge is an ACTION ITEM, not a notice: answer it with
records or an explicit "none".
