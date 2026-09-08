---
type: topic
id: TOP-110
title: A failed chunk upload retries with exponential backoff, capped at six tries
area: network
project: driftwood
current: L1
code_refs:
  - src/network/upload/pipeline.py
tags: [driftwood, network, retry, upload]
links:
  - link: L1
    date: 2025-03-01
    status: active
    kind: adopted
    ruling:
      text: "A chunk upload that fails (timeout, 5xx, connection reset) is retried with exponential backoff starting at 1 second and doubling each attempt, up to 6 attempts, before the chunk is marked failed and surfaced to the user."
      authority: agent-inference
      source: "design note 2025-03-01"
    rationale:
      text: "A transient network blip or a brief server-side hiccup usually clears within a few seconds; retrying immediately just repeats the same failure and adds load to a server that may already be struggling, while backing off gives the transient condition time to pass. Six attempts caps how long a single stuck chunk can block the rest of the upload queue."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-03-01
---
# Upload retry policy

This is the retry schedule for one specific thing: a chunk upload request
that failed. It is not the schedule used when a merge into the server's
conflict state fails (TOP-109, a different queue answering a different
question) even though the shape -- exponential backoff, a cap on attempts
-- looks the same.

Not this topic: the retry schedule for a failed MERGE (TOP-109), or how
much bandwidth an upload is allowed to use once it does go out (TOP-111).
