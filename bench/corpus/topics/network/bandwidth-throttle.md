---
type: topic
id: TOP-111
title: Uploads are rate-limited by a token bucket, default 5MB/s
area: network
project: driftwood
current: L1
code_refs:
  - src/network/throttle.py
tags: [driftwood, network, throttle]
links:
  - link: L1
    date: 2025-03-15
    status: active
    kind: adopted
    ruling:
      text: "Upload bandwidth is capped by a token-bucket rate limiter, defaulting to 5MB/s and configurable by the user, so a large sync never saturates the connection for everything else on the network."
      authority: agent-inference
      source: "design note 2025-03-15"
    rationale:
      text: "Early testers reported video calls and web browsing stalling whenever a large folder started syncing in the background. A token bucket smooths the upload rate over time rather than sending in short full-speed bursts, and the cap gives the rest of the network headroom by default."
      authority: agent-inference
    revisit_if:
      - "the default cap turns out too conservative on connections faster than early testers had"
    recorded_by: agent
    recorded_at: 2025-03-15
---
# Bandwidth throttle

This is the general upload rate limit that applies on an ordinary,
unmetered connection. It is a separate, additional policy from the cap
Driftwood applies specifically on a metered mobile connection (TOP-112) --
the two stack rather than one replacing the other.

Not this topic: the extra cap applied on a connection the OS reports as
metered (TOP-112).
