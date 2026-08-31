---
name: memory-search
description: Check whether a decision, incident, or investigation already exists before implementing something, reversing a behavior, or repeating an approach. Use before implementing X, when asking "does a decision exist about...", "why do we...", "has this been tried before", or when entering an unfamiliar subsystem.
---

# memory-search

Store D (`memory/topics/`, `memory/incidents/`, `memory/investigations/`) is queried through
`memidx.py`, never by reading the markdown tree directly. Run these from the project's memcontinuum
checkout (or wherever `MEMCONTINUUM_HOME`/`--project` are configured for this project).

## Commands

**Semantic search with filters** — the default entry point:
```
memidx.py search "QUERY" --mode vector --project PROJECT --status active [--area AREA] [--type topic] --json
```
Drop `--status active` only when you deliberately want superseded/historical/declined records
too (§G5 of docs/SCHEMA.md: default retrieval excludes them). Add `--authority owner-verbatim` or
`--authority owner-ratified` to find only rulings that can be cited as CONSTRAINT.

**A specific topic's full chain**, once you have its id or slug:
```
memidx.py chain TOP-0042 --project PROJECT --json
```

**What a file is governed by**, before editing it (this is also what the PreToolUse hook runs
automatically — use it by hand when working outside an edit, e.g. while planning):
```
memidx.py for-path path/to/file.ext --project PROJECT --json
```

Drop `--json` for any of the three to get the human-readable compressed chain view instead.

## Reading the output — three tiers (docs/SCHEMA.md §4)

- **CONSTRAINT** — may refuse, block, or reverse work. Only a link whose `ruling.authority` is
  `owner-verbatim` or `owner-ratified` **and** `status: active`. Code and tests are a separate
  constraint channel and need no record.
- **HOLD** — must pause and revalidate against current source. An incident/constraint with
  reproducible evidence (repro, commit, failing test), any authority. A confirmed current-source
  safety violation may block on its own; green tests alone don't clear a HOLD unless they
  discriminate against the failure.
- **CONTEXT** — informs only. `agent-inference` never overrides a fix that code and tests accept
  by itself.

A topic's `current` link is whatever is newest with `status: active` — read the whole chain
(newest first) when a reversal or amendment is present; the reason a ruling changed
(`reason_for_change`) matters as much as the ruling itself.

## The rule

**Name what you found in your report, or state that nothing was found.** Don't silently absorb
a chain into your own reasoning without citing it, and don't silently skip the search because a
task "feels" novel — that's exactly when a prior HOLD or declined approach is most likely to be
missed. If `search`/`chain`/`for-path` return nothing, say so explicitly ("no existing decision
found on X") rather than proceeding as if the question had never been asked.

## Before writing a new helper or file

Before adding a new function, type, or file to a codebase this project indexes, search the
**code** index too — a near-duplicate of what you're about to write may already exist:

```
memidx.py code-search "INTENT PHRASE" --project PROJECT --mode hybrid --json
```

Phrase the query as the intent ("write a debug PNG", "embed a view in a scroll box", "hash
a file's contents"), not as a symbol name — the index is built to match on that. A hit whose
`concept_id` is set means a decision record governs that code; check it (`memidx.py why <path>`)
before working around or duplicating it.

The `--json` output carries a `state` field: `uninitialized` (exit non-zero — `code-reindex`
was never run for this project; a bare `[]` here is a refusal, not a real "nothing found"),
`stale` (source changed since the last `code-reindex` — a warning, not a block), or `current`.
**Confirm the code index is initialized/current (or stale with eyes open) before trusting a
"nothing found" — then run `code-search`; name relevant hits in your report, or say none** —
the same rule as the markdown search above: silently skipping this check is exactly how a
second, slightly different `embedInScrollBox` gets written next to the first one.
