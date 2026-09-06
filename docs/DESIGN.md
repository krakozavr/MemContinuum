# Design notes

Why this engine looks the way it does. Four decisions, each with the
alternative it was chosen over.

## Chains, not notes

A single note answers "what did we decide?" but not "did we already try the
other thing, and why didn't it work?" — that history usually lives, if it
lives anywhere, scattered across chat transcripts nobody re-reads. This
engine makes a topic an **append-only chain of links**: every ruling on one
question, newest first, each one dated and never edited after the fact. A
changed mind is a new link with `reverses:` and `reason_for_change:`, not an
edit to the old one. The result is that "we tried X, it didn't work because
Y, so we do Z instead" is a fact the store can hand back on request — instead
of relying on an agent (or a person) to remember it, or to re-derive it by
reading code and guessing.

Per-field **authority** exists for the same reason a chain does: "we decided
X" and "we decided X because Y" are different claims with different weight.
The decision itself might be the project owner's own words (`owner-verbatim`)
while the reasoning behind it is an agent's best guess (`agent-inference`).
Collapsing those into one undifferentiated "note" loses exactly the
distinction that matters when something is being second-guessed later.

## Markdown canonical, SQLite derived

The markdown files are the store; the SQLite index is a cache that can be
deleted and rebuilt from them at any time (`reindex`), never the other way
around. This keeps the actual content readable and diffable in an ordinary
git history, greppable without any tooling, and mergeable the way plain text
merges — while the index still gets full-text and vector search where a
database earns its keep. A store that treated the database as canonical
would tie its history to whatever that database's own migration story turns
out to be; a store that treats markdown as canonical only has to keep parsing
markdown.

## Hooks force retrieval, not agent discipline

Telling an agent "check the decision history before you edit this file" and
hoping it remembers to do so is exactly the failure mode this engine exists
to route around — the same failure mode that "remember to write things down"
already suffers from at the note-taking end. Instead, a `PreToolUse` hook
looks up whatever the file being edited is governed by and injects it as
context *at the moment of the edit*, before any code gets written — for
edits made through the Edit and Write tools. That is structured
pre-retrieval, not a universal guarantee: a file changed from the shell (a
script, a formatter, `git apply`, anything run as a Bash command) gets no
such lookup beforehand. The PostToolUse ledger hook still sees it, but only
afterwards, from a tree diff against the last state it saw — best effort,
never called pre-retrieval, because it is not one. The ledger entry it
writes is bookkeeping (a record that some path changed), not a decision
record, and it is not proof anything governing that path was looked up
first. Retrieval that depends on being remembered eventually isn't
retrieval; it's one more thing to forget. The same logic runs the other
direction: write-side hooks
remind a session that a decision it just made might be worth recording —
again, a nudge at the moment it matters, not a hope that someone remembers to
write the note later. The triggers are deliberately blind to what was typed:
one fires when edited files are covered by no decision topic and the edit
ledger has grown since the last nudge, the other when the conversation has
moved on — several turns or tens of minutes — with no new edits at all. A
nudge pending at a compaction is relayed into the session on the other side.

## No auto-capture

Nothing in this engine watches a session and silently writes records from
what it observes. Every record is something an agent deliberately composed,
and every record that claims to speak for the project owner passes through
an explicit promotion step (§5 of `SCHEMA.md`) where the owner sees the exact
text before it can be cited as a constraint. Auto-capture is tempting because
it is less work, but it also means nobody ever chose the wording, chose what
counted as decision-worthy, or confirmed authority — and a store whose
authority can't be trusted is worse than no store, because it gets cited with
the same confidence as one that can.
