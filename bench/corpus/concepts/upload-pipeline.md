---
type: concept
id: CON-303
title: Upload Pipeline
owner_boundary: "src/network/upload plus the streaming helper it calls for large files -- turning a chunk into bytes successfully stored on the server, including retrying a failed attempt."
implemented_by:
  - src/network/upload/pipeline.py#upload_chunk
  - src/network/upload/pipeline.py#retry_upload
  - src/network/streaming.py#stream_upload
tested_by:
  - tests/test_upload.py
governed_by: [TOP-110, TOP-113]
involved_in: []
tags: [driftwood, network]
---
# Upload Pipeline

The upload pipeline is the last leg between a chunk that needs to leave
the device and that chunk existing on the server: sending it, retrying it
on failure, and switching to a streamed transfer once a file is large
enough that reading it whole first would be wasteful.

This is NOT this concept: how fast the pipeline is ALLOWED to send (the bandwidth
throttle, `network/throttle.py`, governed by TOP-111, called by this
pipeline but owned separately) or the merge-retry logic for a failed
conflict resolution (`sync/merge_queue.py`, governed by TOP-109, a
different queue for a different kind of retry).
