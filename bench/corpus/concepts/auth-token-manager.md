---
type: concept
id: CON-304
title: Auth Token Manager
owner_boundary: "src/auth -- obtaining, refreshing and storing the credentials that authenticate the sync client to the server."
implemented_by:
  - src/auth/oauth.py#device_code_flow
  - src/auth/tokens.py#refresh_token
  - src/auth/tokens.py#store_token
tested_by:
  - tests/test_auth.py
governed_by: [TOP-114, TOP-115, TOP-116]
involved_in: [INC-202]
tags: [driftwood, auth]
---
# Auth Token Manager

Everything about the client's own credentials -- getting them for the
first time, refreshing them before they expire, and keeping them at rest
between runs -- is owned here.

Not this concept: what the credentials are allowed to DO once presented to
the server (authorization/permission checks are a server-side concern, not
represented anywhere in this client-side codebase) -- this concept only
covers obtaining and holding the credential, not this concept's use once
sent.
