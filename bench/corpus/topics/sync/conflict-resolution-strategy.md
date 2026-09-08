---
type: topic
id: TOP-107
title: A conflicting edit keeps both copies, never picks a winner
area: sync
project: driftwood
current: L2
code_refs:
  - src/sync/conflict/resolver.py
tags: [driftwood, sync, conflict]
links:
  - link: L2
    date: 2025-07-14
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "When two devices edit the same file concurrently, Driftwood keeps both edits as sibling files -- 'name.md' and 'name (conflicted copy, device-b).md' -- instead of silently choosing one and discarding the other."
      authority: agent-inference
      source: "post-incident design review 2025-07-14"
    rationale:
      text: "Picking a winner by timestamp means the loser's edit vanishes with no warning the user ever sees, and a wrong clock on one device makes the wrong edit win. Keeping both costs a little extra storage (mitigated by chunk-level dedup, TOP-101) and never silently drops a user's work."
      authority: agent-inference
    alternatives:
      - {option: "merge the two versions automatically (three-way text merge)", rejected_because: "most synced files are not line-oriented text a merge algorithm can reason about (spreadsheets, PDFs, images), so a general-purpose merge would only work for a minority of files and behave inconsistently across the rest", authority: agent-inference}
    evidence:
      - "support tickets: three users lost edits to last-write-wins when a laptop's system clock was wrong after waking from sleep"
    recorded_by: agent
    recorded_at: 2025-07-14
  - link: L1
    date: 2025-02-10
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "When two devices edit the same file concurrently, the version with the later timestamp wins and the other is discarded."
      authority: agent-inference
      source: "design note 2025-02-10"
    rationale:
      text: "Last-write-wins is the simplest possible policy and was enough to ship sync between two devices for the first release."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-02-10
---
# Conflict resolution

Detecting that two edits actually conflict is a separate question (TOP-108,
vector clocks); this topic is about what happens once a conflict is
confirmed. The policy changed once from a timestamp-based winner-take-all
rule to keeping both edits, after the timestamp rule silently ate a user's
work.

Not this topic: how a conflict is DETECTED in the first place (TOP-108).
