---
type: incident
id: INC-205
title: Background sync bypassed the metered-connection check and drained a data plan overnight
area: network
date: '2025-08-28'
status: active
authority: agent-inference
source: "user report 2025-08-28"
evidence:
  - "a user's phone switched to mobile data overnight while a large folder was mid-sync"
  - "background sync had its own code path that called the upload pipeline directly, skipping the metered-connection check the foreground app performed"
  - "the user's mobile carrier reported roughly 1.8GB transferred overnight against a plan with no such headroom"
code_refs:
  - src/network/mobile_throttle.py
---
# Background sync skipped the metered check

The foreground app checked whether the connection was metered before
starting a large transfer, but the background sync path -- which runs
without the user watching -- called into the upload pipeline directly and
never made the same check. When a user's phone switched from Wi-Fi to
mobile data overnight mid-sync, background sync kept transferring at full
speed against a metered connection, with nothing to stop it until the
user's data plan noticed.

TOP-112's ruling makes the metered check and the resulting daily cap apply
to every transfer path, background included, not only the one the
foreground app happens to call.
