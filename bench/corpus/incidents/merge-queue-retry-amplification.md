---
type: incident
id: INC-206
title: A fixed-interval merge retry amplified load during a multi-hour outage
area: sync
date: '2025-08-15'
status: active
authority: agent-inference
source: "on-call postmortem 2025-08-15"
evidence:
  - "the merge queue retried every failed merge on a fixed 2-second interval regardless of how many prior attempts had failed"
  - "during a 3-hour server outage, every client's merge queue kept resubmitting the same conflicting edits every 2 seconds the entire time"
  - "when the server came back, it received the full backlog of retries from every client simultaneously, extending the outage's recovery time"
code_refs:
  - src/sync/merge_queue.py
---
# Retry amplification during an outage

The merge queue's retry logic re-attempted a failed merge every 2 seconds,
with no growing delay and no cap on attempts. During a 3-hour outage, this
meant every client with a pending conflicting edit kept resubmitting the
same request every 2 seconds for the full 3 hours. When the server
recovered, it was hit by the accumulated retry traffic from every client
at once, which measurably slowed the server's own recovery.

TOP-109's exponential backoff (capped at 60 seconds, up to 5 attempts)
both reduces load during the outage itself and spreads the reconnection
traffic out once the server returns, instead of concentrating it in the
first instant.
