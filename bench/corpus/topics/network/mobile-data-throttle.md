---
type: topic
id: TOP-112
title: A metered connection gets its own daily data cap
area: network
project: driftwood
current: L1
code_refs:
  - src/network/mobile_throttle.py
tags: [driftwood, network, throttle, mobile]
links:
  - link: L1
    date: 2025-09-02
    status: active
    kind: adopted
    ruling:
      text: "Before any transfer, Driftwood checks whether the OS reports the active connection as metered; on a metered connection, a separate daily data cap (default 50MB) applies on top of the ordinary bandwidth throttle, and background sync must check this before touching the network at all."
      authority: agent-inference
      source: "post-incident design review 2025-09-02"
    rationale:
      text: "A rate limit (TOP-111) controls how FAST data moves, not how MUCH moves in a day -- on an unmetered connection that's fine, but on a phone's mobile data plan the total volume is what costs money, and a background process is the least visible place for that cost to accumulate unnoticed."
      authority: agent-inference
    evidence:
      - "INC-205: background sync ignored the metered check and drained a user's mobile data plan overnight"
    recorded_by: agent
    recorded_at: 2025-09-02
---
# Mobile data cap

Separate from the general bandwidth throttle (TOP-111), this is a daily
volume cap that only applies once the OS says the current connection is
metered -- covering the case a rate limit alone does not: a slow trickle
that adds up to real money over a mobile data plan by the end of the day.

Not this topic: the general upload rate limit that applies regardless of
connection type (TOP-111).
