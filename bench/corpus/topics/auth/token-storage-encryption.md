---
type: topic
id: TOP-116
title: Tokens live in the OS keychain, never in a plain file
area: auth
project: driftwood
current: L2
code_refs:
  - src/auth/tokens.py
tags: [driftwood, auth, tokens, security]
links:
  - link: L2
    date: 2025-06-30
    status: active
    kind: reversed
    reverses: L1
    reason_for_change: new-evidence
    ruling:
      text: "Access and refresh tokens are stored in the operating system's own credential store (Keychain on macOS, Credential Manager on Windows, Secret Service on Linux), never written to a plain file on disk."
      authority: agent-inference
      source: "security review follow-up 2025-06-30"
    rationale:
      text: "A file under the user's home directory is readable by any other process running as that same user, with no additional barrier -- the OS keychain requires the requesting process to be authorized, which is a real access-control boundary a plain file does not have."
      authority: agent-inference
    evidence:
      - "external security review 2025-06-30: flagged plaintext token storage as a finding"
    recorded_by: agent
    recorded_at: 2025-06-30
  - link: L1
    date: 2025-02-20
    status: superseded
    superseded_by: L2
    kind: adopted
    ruling:
      text: "Access and refresh tokens are stored in a config file under the user's home directory."
      authority: agent-inference
      source: "design note 2025-02-20"
    rationale:
      text: "A plain file needed no platform-specific keychain integration and was the fastest path to a working first version across all three target operating systems."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-02-20
---
# Token storage at rest

Once a token exists (TOP-114) and gets refreshed on schedule (TOP-115), it
has to be kept somewhere between runs of the CLI. This topic is where --
originally a plain file, since replaced by the OS keychain after a
security review.

Not this topic: how the token was first obtained (TOP-114) or when it gets
refreshed (TOP-115).
