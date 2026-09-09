---
type: topic
id: TOP-121
title: Files are encrypted on the client before they ever leave the device
area: security
project: driftwood
current: L1
code_refs:
  - src/security/encryption.py
tags: [driftwood, security, encryption]
links:
  - link: L1
    date: 2025-06-30
    status: active
    kind: adopted
    ruling:
      text: "Every chunk is encrypted on the client, with a key the server never receives, before it is handed to the upload pipeline; the server only ever stores ciphertext."
      authority: agent-inference
      source: "security review follow-up 2025-06-30"
    rationale:
      text: "A server that can decrypt user files is a single point of both compromise and legal exposure -- a breach of the server, or a request the operator is compelled to answer, would expose file content directly. Encrypting client-side, with a key the server never holds, means the server has nothing to leak."
      authority: agent-inference
    evidence:
      - "external security review 2025-06-30"
    recorded_by: agent
    recorded_at: 2025-06-30
---
# Client-side encryption

This is about file CONTENT, encrypted before upload so the server never
sees plaintext. It is a separate mechanism from where auth tokens are
stored (TOP-116) -- one protects the data being synced, the other protects
the credential used to sync it.

Not this topic: how auth tokens (not file content) are stored (TOP-116).
