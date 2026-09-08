---
type: incident
id: INC-202
title: Synchronized token refresh across many clients tripped the identity provider's rate limit
area: auth
date: '2025-09-09'
status: active
authority: agent-inference
source: "on-call postmortem 2025-09-09"
evidence:
  - "traffic graph: refresh requests spiked to 40x baseline in a single second, coinciding with a product-launch signup wave twelve hours earlier"
  - "identity provider returned 429 to roughly a third of refresh attempts during the spike, logging those clients out"
code_refs:
  - src/auth/tokens.py
---
# Refresh stampede

A large batch of users signed up within the same few minutes during a
product launch, which meant a large batch of access tokens were all
issued with nearly the same expiry time. Twelve hours later, every one of
those clients tried to refresh within the same one-second window, and the
identity provider's own rate limit rejected roughly a third of them,
logging those users out with no warning.

The fix (TOP-115) adds a randomized jitter window to when each client
refreshes, so a shared issue time no longer produces a shared refresh
time.
