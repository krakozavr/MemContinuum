---
type: topic
id: TOP-115
title: Tokens refresh early, with random jitter to avoid a stampede
area: auth
project: driftwood
current: L1
code_refs:
  - src/auth/tokens.py
tags: [driftwood, auth, tokens]
links:
  - link: L1
    date: 2025-09-10
    status: active
    kind: adopted
    ruling:
      text: "Driftwood refreshes an access token proactively, ahead of its expiry, and adds a randomized jitter window to the refresh time so that clients which authenticated around the same moment do not all refresh in the same second."
      authority: agent-inference
      source: "post-incident design review 2025-09-10"
    rationale:
      text: "Waiting for a token to actually expire before refreshing means a request fails first and retries second, which is a worse user experience than refreshing early enough that expiry never happens on the request path. The jitter exists specifically because a large number of clients that logged in during the same product-launch window would otherwise refresh in lockstep and hit the identity provider all at once."
      authority: agent-inference
    evidence:
      - "INC-202: a synchronized refresh across many clients at once tripped the identity provider's own rate limit"
    recorded_by: agent
    recorded_at: 2025-09-10
---
# Token refresh timing

This topic is about WHEN a token gets refreshed, not where the resulting
token is kept (TOP-116) or how the very first token was obtained
(TOP-114).

Not this topic: where tokens are stored at rest (TOP-116).
