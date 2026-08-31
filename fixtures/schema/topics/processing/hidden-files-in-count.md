---
type: topic
id: TOP-0042
title: Hidden files in the processed count
area: processing/status
project: default
current: L4
code_refs:
  - src/core/scan/scan_plan.py#hidden_count
  - src/app/summary/summary_card.py#appendix
tags: [hidden-files, appendix, summary-card]
links:
  - link: L4
    date: 2024-04-15
    status: active
    kind: restored
    reverses: L3
    reason_for_change: new-evidence
    ruling:
      text: "the count and the explanatory appendix are one pair"
      authority: agent-inference
    rationale:
      text: "the earlier deletion happened because both reviewers were blind to the rationale"
      authority: agent-inference
    evidence: []
    revisit_if:
      - "the count and the appendix stop being shown together"
      - "a user reports the appendix as noise"
    recorded_by: agent
    recorded_at: 2024-04-15
  - link: L3
    date: 2024-04-01
    status: superseded
    superseded_by: L4
    kind: amended
    ruling:
      text: "the appendix was removed while the count stayed: half of the pair deleted"
      authority: reviewer-finding
    evidence: []
    recorded_by: agent
    recorded_at: 2024-04-01
  - link: L2
    date: 2024-03-20
    status: superseded
    superseded_by: L3
    kind: adopted
    ruling:
      text: "hidden (dot) files are excluded from denominators and shown only on the summary card, with an appendix explaining the exclusion"
      authority: agent-inference
    rationale:
      text: "keeps the number the user sees accurate; the appendix explains removals separately"
      authority: agent-inference
    evidence: []
    recorded_by: agent
    recorded_at: 2024-03-20
  - link: L1
    date: 2024-03-02
    status: declined
    kind: declined
    rationale:
      text: "counting hidden files in the main total was rejected because it inflates the number the user sees"
      authority: agent-inference
    evidence: []
    recorded_by: agent
    recorded_at: 2024-03-02
---

Dotfiles and other hidden (dot) files are left out of the processed count the user sees.
Invisible system files are excluded from the denominator; the summary card shows them, if at
all, only on a removed-items appendix, with a short note explaining why they were excluded from
the main total.

This is a synthetic fixture for a fictional file-organizing app; it is not drawn from any real
project's history.
