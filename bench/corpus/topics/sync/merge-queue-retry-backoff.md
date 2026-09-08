---
type: topic
id: TOP-109
title: Merge-queue retries back off exponentially, capped at five tries
area: sync
project: driftwood
current: L1
code_refs:
  - src/sync/merge_queue.py
tags: [driftwood, sync, retry]
links:
  - link: L1
    date: 2025-08-19
    status: active
    kind: adopted
    ruling:
      text: "A conflicting edit that fails to merge into the current server state is retried with exponential backoff (2^attempt seconds, capped at 60 seconds), up to 5 attempts, before giving up and surfacing a conflict copy to the user."
      authority: agent-inference
      source: "post-incident design review 2025-08-19"
    rationale:
      text: "Re-attempting a merge re-runs conflict detection against whatever the server's current state has become, which keeps moving during an outage or a burst of activity. Retrying too fast just re-checks a target that hasn't changed yet and wastes a request; this schedule is deliberately its own policy, separate from the upload pipeline's retry schedule (TOP-110), because a tight merge-retry loop amplifies load in exactly the way an upload retry does not."
      authority: agent-inference
    evidence:
      - "INC-206: merge-queue retries amplified load during a multi-hour outage by resubmitting the same conflicting edits on a short fixed interval"
    recorded_by: agent
    recorded_at: 2025-08-19
---
# Merge-queue retry policy

When a merge attempt into the latest server state fails, the merge queue
does not just try again immediately. This topic is that retry schedule --
deliberately separate from the upload pipeline's own retry logic even
though both back off exponentially, because the two retries are attempting
different things against different targets.

Not this topic: how a chunk UPLOAD is retried after a failed request
(TOP-110) -- a different queue with its own schedule.
