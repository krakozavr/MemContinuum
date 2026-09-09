---
type: topic
id: TOP-117
title: Filesystem events debounce over a 750ms window
area: watch
project: driftwood
current: L1
code_refs:
  - src/watch/debounce.py
tags: [driftwood, watch, debounce]
links:
  - link: L1
    date: 2025-04-22
    status: active
    kind: adopted
    ruling:
      text: "Filesystem change events on the same path arriving within 750ms of each other are collapsed into a single change notification, rather than queuing an upload per raw event."
      authority: agent-inference
      source: "post-incident design review 2025-04-22"
    rationale:
      text: "Many ordinary operations -- a text editor's autosave, a build tool writing a file in stages -- fire several write events for what a user thinks of as one change. Without debouncing, each of those becomes its own upload attempt; 750ms was chosen as comfortably longer than the gap between an editor's own internal writes but still short enough that a genuinely separate edit a second later is treated as its own change."
      authority: agent-inference
    evidence:
      - "INC-203: a window shorter than a large git checkout's write bursts queued a duplicate upload per intermediate file write"
    revisit_if:
      - "a common workflow's write bursts turn out to routinely exceed 750ms and still get treated as separate changes"
    recorded_by: agent
    recorded_at: 2025-04-22
---
# Debounce window

Before a raw filesystem event ever reaches the sync engine, it passes
through this collapsing window. Too short, and a burst of writes for one
logical save turns into many uploads; too long, and genuinely separate
edits get merged into one notification.

Not this topic: how a rename is told apart from a delete-and-recreate
(TOP-118) -- a different judgment the watcher makes on the same raw
events.
