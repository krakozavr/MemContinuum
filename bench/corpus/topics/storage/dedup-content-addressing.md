---
type: topic
id: TOP-101
title: Chunks are stored by content hash, not by file
area: storage
project: driftwood
current: L1
code_refs:
  - src/storage/dedup/
  - src/storage/checksum.py
tags: [driftwood, storage, dedup]
links:
  - link: L1
    date: 2025-02-03
    status: active
    kind: adopted
    ruling:
      text: "The chunk store is content-addressed: a chunk's storage key is the hash of its own bytes, so two chunks with identical content are always the same on-disk object, however many files or versions reference them."
      authority: agent-inference
      source: "design note 2025-02-03, storage layer kickoff"
    rationale:
      text: "Users routinely keep several near-duplicate copies of the same material -- an exported PDF alongside its source, a renamed backup folder, a project directory copied for a client. Content addressing means those copies cost storage once, not once per copy, without any special-case duplicate detection."
      authority: agent-inference
    alternatives:
      - {option: "dedupe at the whole-file level only (hash the file, not each chunk)", rejected_because: "a single edited paragraph in an otherwise-identical file would then block dedup for the entire file, defeating the point for exactly the case (small edits to a large file) users hit most", authority: agent-inference}
    recorded_by: agent
    recorded_at: 2025-02-03
---
# Chunk store: content-addressed

The dedup store never asks "which file does this belong to" when deciding
where to write a chunk -- only "have I seen these bytes before". That is
what lets an unchanged region of a file survive an edit without being
re-stored, and what lets two users' copies of the same file collapse to
one set of chunks on disk.

Not this topic: which hash function computes the content key (TOP-102) or
how a file gets split into chunks in the first place (TOP-103).
