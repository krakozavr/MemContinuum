---
type: incident
id: INC-203
title: A large git checkout's write burst overwhelmed the sync queue
area: watch
date: '2025-04-20'
status: active
authority: agent-inference
source: "user report and reproduction 2025-04-20"
evidence:
  - "checking out a branch that touched 6,000 files produced roughly 14,000 raw filesystem events within under 200ms"
  - "each event queued its own upload attempt under the then-current 100ms debounce window, saturating the upload queue for several minutes"
code_refs:
  - src/watch/debounce.py
---
# Upload storm from a fast write burst

A `git checkout` touching thousands of files writes them all in a very
short span. With the debounce window at the time (100ms), most of those
writes were still treated as separate change events, each queuing its own
upload attempt, and the upload queue backed up for several minutes
processing redundant work for files whose content hadn't meaningfully
changed since the last collapsed event.

Widening the debounce window to 750ms (TOP-117) collapses a burst like
this into far fewer notifications.
