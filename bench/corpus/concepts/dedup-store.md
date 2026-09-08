---
type: concept
id: CON-301
title: Dedup Store
owner_boundary: "src/storage/dedup -- the content-addressed chunk store: writing a chunk, reading it back, and garbage-collecting the chunks nothing references any more."
implemented_by:
  - src/storage/dedup/store.py#put_chunk
  - src/storage/dedup/store.py#get_chunk
  - src/storage/dedup/store.py#gc_sweep
tested_by:
  - tests/test_dedup.py
governed_by: [TOP-101, TOP-104]
involved_in: [INC-201]
tags: [driftwood, storage]
---
# Dedup Store

The dedup store owns exactly one job: given a chunk's bytes, decide where
it lives on disk, keyed by content so identical bytes are only ever stored
once, and reclaim that space once nothing points at a chunk any more.

This is NOT this concept: computing a chunk's hash (that is `storage/checksum.py`,
governed by TOP-102) or deciding where a file's chunk boundaries fall in
the first place (that is `storage/chunker.py`, governed by TOP-103) -- both
are called BY the dedup store's callers, not owned by this concept.
