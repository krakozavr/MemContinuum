---
type: topic
id: TOP-113
title: Files over 256MB are streamed, never loaded fully into memory
area: network
project: driftwood
current: L1
code_refs:
  - src/network/streaming.py
tags: [driftwood, network, upload, streaming]
links:
  - link: L1
    date: 2025-05-27
    status: active
    kind: adopted
    ruling:
      text: "A file at or above 256MB is uploaded in bounded-memory streamed chunks; smaller files are read and uploaded whole."
      authority: agent-inference
      source: "design note 2025-05-27"
    rationale:
      text: "Reading a multi-gigabyte video file fully into memory before uploading it competed for RAM with everything else running on the user's machine and, on lower-memory laptops, occasionally triggered the OS's own memory pressure handling mid-upload. Streaming keeps peak memory bounded regardless of file size, at the cost of a little extra bookkeeping the whole-file path doesn't need -- not worth paying for every small file, hence the threshold."
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2025-05-27
---
# Large file streaming

Below the threshold, uploading a file is the simple path: read it, hand it
to the upload pipeline. Above it, the same pipeline is fed from a stream
instead, so a multi-gigabyte file never sits fully in memory at once.

Not this topic: how a failed chunk upload is retried (TOP-110) or how fast
an upload is allowed to go (TOP-111) -- both apply to a streamed upload
exactly as they do to a whole-file one.
