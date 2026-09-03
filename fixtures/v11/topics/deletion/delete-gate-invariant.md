---
type: topic
id: TOP-0100
title: Deletion goes through one gate
area: deletion/gate
project: default
current: L2
code_refs:
  - Sources/Delete/DeleteGate.swift#delete
links:
  - link: L2
    date: 2026-08-29
    status: active
    kind: amended
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "all file removal must go through DeleteGate; no direct FileManager calls elsewhere"
      authority: owner-verbatim
      source: "session 2026-08-29"
    rationale:
      text: "a direct removeItem call bypassed the gate's safety checks in QuickCleanup"
      authority: agent-inference
    edges:
      - {rel: supersedes, to: "TOP-0100/L1"}
      - {rel: challenged_by, to: "INC-900"}
      - {rel: abandons, to: "assumption:A1"}
    assumptions:
      - {id: A1, text: "every deletion call site is easy to find by reading the code", status: broken, since: 2026-08-29}
      - {id: A2, text: "DeleteGate is the only type that imports FileManager for removal", status: holds}
    invariant:
      kind: pattern-absent
      pattern: "FileManager\\.default\\.removeItem"
      allowed: ["Sources/Delete/DeleteGate.swift"]
      checked_by: "Tests/DeleteGateTests.swift"
    evidence: [commit fake0001]
    revisit_if:
      - "a second sanctioned deletion path is introduced"
    recorded_by: agent
    recorded_at: 2026-08-29
  - link: L1
    date: 2026-08-01
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "file removal happens through DeleteGate"
      authority: agent-inference
    evidence: []
    recorded_by: agent
    recorded_at: 2026-08-29
---

DeleteGate is the single call site for removing files from disk; nothing else should call
FileManager directly to delete.
