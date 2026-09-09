# skill-honesty — report

Worktree: `~/dev/memcontinuum-skill`, branch `skill-honesty`, off `main` at `b1d6ac5`.
`fixtures/records` symlinked in from the primary checkout (untracked by design — gitignore pattern
`fixtures/records/` only matches a real directory, a symlink shows as untracked; never `git add -A`
here, only explicit paths).

Trailer note: the brief specifies `Co-Authored-By: Claude Opus 5 (1M context)`, but this session's
runtime identifies as Sonnet 5. Used the brief's trailer as instructed ("follow it exactly"); flagging
the discrepancy here rather than silently resolving it either way.

## S1 + S2 + S3 + S5 — commit `f21d877`

(S5 is an owner follow-up mid-task that generalizes S2 to every state; done in the same commit
since it's the same file, same judgement pass.)

### S1 — rules deleted (restated computed values) vs kept

Deleted, each replaced with either nothing or a pointer at the tool's own output:

1. **`--project` character class** (`[A-Za-z0-9._-]+`, literal regex copied from
   `scripts/repo-init.sh:653`) — deleted. `repo-init.sh`'s own refusal message names the allowed
   characters when it fires; nothing before running needs this literal.
2. **Store-must-be-its-own-git-repo / nested-repo refusal / foreign-repo-markers refusal** —
   deleted entirely (SKILL.md's `--store DIR` bullet now says only the naming convention and the
   dry-run-verbatim instruction, nothing about this refusal at all). Each of these refusals names
   its own fix when it fires (`--force`, or "point --store at a location that does not exist yet,
   or at an existing MemContinuum store"), and the dry-run reaches every one of them, so there is
   nothing here an agent needs before running that it cannot get from the tool's own output.
   (An earlier draft of this report said the refusal's *existence* was kept as one line — that was
   wrong; corrected after a review pass caught it.)
3. **The default store location algorithm** (sibling rule, WSL-disk redirect, exact paths
   `$HOME/dev/<repo>-MemContinuum-Store` / `$HOME/<repo>-MemContinuum-Store`, "prints why") —
   deleted entirely. This is the exact paragraph INC-0117 was about. Replaced with: always dry-run
   first, read the `store :` line and any `note:` line above it out of that dry-run's own output,
   verbatim, to the human — never predict it on any platform, because the rule lives in
   `scripts/mc-registry-lib.sh`'s `mc_default_store_for` and can change without this skill knowing.
4. **Explicit `--store` requires explicit `--claude-dir`** — deleted. `repo-init.sh` refuses this
   immediately (before python resolution), naming the fix; the dry-run reaches it too.
5. **`decide.sh wired` refuses anything short of `wiring=full`** — deleted the refusal clause, kept
   the repair *procedure* (re-run `repo-init.sh` with the same `--store`/`--project`, then record).
   The order is agent workflow knowledge; the refusal mechanism is code's.

Kept, with reasons:

1. **`MemContinuum-Store` naming convention** — kept. Verified `mc_is_marked_store`
   (`scripts/mc-registry-lib.sh:437-449`) only checks for a `topics`/`incidents`/`concepts`
   directory or a README mentioning "MemContinuum" — `repo-init.sh` does **not** enforce the
   `*-MemContinuum-Store` name on an explicit `--store`. This is the one genuine precondition in
   the block: the tool will not catch a badly-named store, so the agent must know the convention
   before proposing a name.
2. **`--repo REPO` required for `wired`/`declined`/`forget`** — kept (unchanged). This is a stable
   interface fact (matches `memcontinuum-decide.sh`'s own header), not a computed value at risk of
   silent drift the way the store-default algorithm was, and it directs the agent away from a
   dangerous default (relying on `$PWD`) rather than merely restating an internal check.
3. **Section 1's `decision=`/`wiring=`/`state=` vocabulary** — kept, out of the audited pattern
   ("repo-init.sh refuses X" / "defaults to Y" / "must match Z"). This explains what
   `memcontinuum-state.sh`'s own terse `key=value` output *means* — necessary to interpret output
   correctly, not a restatement of a repo-init.sh-owned computed value. (It does closely track
   `memcontinuum-state.sh --help`'s own text, so it's lower drift-risk than the store-location case:
   both live in the same script and would need updating together anyway.)
4. **The `[ -t 0 ]` / no-tty driven-flow guidance** — kept (unchanged). This is operational hazard
   knowledge (the Bash tool has no tty) that changes which command sequence the agent runs, not a
   restated computed value.
5. Trimmed but not deleted, in two places (found on the first pass and a third on review): "five
   hooks" softened to "hooks wired into the project's `.claude` settings (the dry-run's plan lists
   exactly which, and how many)" in Section 2's cost paragraph, and Section 1's "only SOME of the
   five hooks are present" softened to "only SOME of the always-wired hooks are present" — both
   remove a count `scripts/mc-registry-lib.sh` owns (`the five ALWAYS-wired write-side hooks`) from
   places where the number is either printed live by the dry-run or never printed by
   `memcontinuum-state.sh --help` at all (only its header comments say "five" — text an agent
   running the command never sees).

### S2 — consent prompt pinned (owner-corrected mid-task)

`## 2. Ask` now specifies the interface's **structured multiple-choice prompt, never prose**, with
the four options in order (owner's correction: "Not now" is a visible fourth option, not a hidden
gesture):
1. Yes, with code retrieval
2. Yes, rationale only
3. No — record the decline; this repo is never asked again
4. Not now — nothing is recorded; you will be asked again next session

Deleted the advocacy paragraph ("It earns its keep on a codebase with contested history... Say
which of those you think this repo is, and let them decide.") per the correction and the original
brief's "no reading of whether this particular repo deserves one."

### S5 — coordinator follow-up: every state gets a prompt, never destructive by accident

Extended `## 2. Ask` to cover every reachable state, not just `undecided`:
- `wired` — report facts first (`decision`, `wiring`, `decided_at`, `store`, `project`, `settings`
  — verified all six are real fields `memcontinuum-state.sh` prints), then 4 options, "Keep as is"
  first.
- `declined` — 2 options, "Keep declined" first.
- `partial-wired` — 3 options, "Complete the wiring" first.
- `not-a-repo` / `no-config` — no prompt, stated explicitly as the only states where one would be
  theatre.

Two new mechanisms in Section 4 back the `wired` prompt's options 2-3, both verified against real,
existing tool behaviour (nothing invented):
- **"Change where the store lives"** — move the directory (a plain git working tree), then
  `repo-init.sh --store NEWDIR --claude-dir CLAUDE_DIR --adopt-only`, then `decide.sh wired --store
  NEWDIR ...`. Grounded in `--adopt-only`'s documented purpose (wire an existing store, refuse to
  create one) and the `store-missing`/"renamed or moved, point --store at where it lives now"
  pattern already used identically in `scripts/memcontinuum-update.sh` (lines ~1191, 1318, 1392).
- **"Add or remove code retrieval"** — add via the existing driven `--code-root` flow; remove has
  **no automated flag** (verified: `repo-init.sh`'s `--record-decision` unions code-roots, "never
  dropped" — there is no remove path), so it's a hand-edit per README "Uninstall" step 1, then
  `decide.sh wired` re-run naming only the code-roots that remain (verified `memcontinuum-decide.sh`
  rewrites-then-appends the row — replace, not union, unlike `--record-decision`'s own union). Text
  says "that code-root's hook entries," not "the PreToolUse hook" (singular) — a `--code-root`
  actually wires TWO PreToolUse hooks (`pre-edit-chain.sh` for retrieval, `newfile-nudge.sh` for the
  new-file reminder; verified in `scripts/repo-init.sh`), and the dry-run's plan is what actually
  names them, so the skill should not itself claim a specific count or list.

Also resolved a self-contradiction the new "change where the store lives" flow created against the
existing absolute rule "Never delete or move an existing store": narrowed it in Section 5 to exempt
only that one human-chosen flow, matching the pattern the other absolute rules already use ("never
initialize *without an explicit yes*").

Section 5 gained two explicit rules: "one question, no advocacy" now stated as governing every
state (not just `undecided`), and a corrected statement of the safety invariant (below).

**A judgment call, found in a review pass and corrected before this task counted as done:** the
coordinator's S5 message stated the invariant as "the FIRST option is always the safe no-change
one," but its own worked example for `partial-wired` puts "Complete the wiring" (which writes
hooks) first, and the owner's S2 correction fixes `undecided`'s order as "Yes, with code retrieval"
first (an install) -- both outrank the general "always" as written. The literal "first option is
always safe" claim is false for `undecided` and `partial-wired`; only `wired` and `declined`
(states with an existing recorded answer) actually satisfy it. Resolution: kept every explicit
option order exactly as specified (owner-verbatim for `undecided`, coordinator-specified for the
other three), and rewrote the invariant itself to what is actually true everywhere: no option ever
deletes a store (absolute, on its own), and where a repo already has a recorded answer the first
option always keeps it -- where it doesn't, `Not now` is always present and always records nothing.
This is stated in both Section 2's preamble and Section 5 (`skills/memcontinuum/SKILL.md` lines
55-64 and ~262-264).

### S3 — tests (`tests/test_docs.py`, class `TestSkillHonesty`, 8 tests)

Added to the existing `tests/test_docs.py` (the doctrine-pinning home per its own docstring),
following the module's established style (see `TestDocsRound7`). All 8 pass; slicing is done
between `## 2. Ask` / `## 3. Act on the answer` markers specifically to avoid the vacuous-test trap
the module itself calls out (`test_schema_incidents_section_names_every_field_actually_used`'s
comment) — an unsliced `assertIn` for e.g. "No —" would pass on unedited text since Section 3
already contains "No → record the decline".

- `test_no_computed_store_rules_restated` — asserts 5 exact removed phrases (`Windows-mounted`,
  `$HOME/dev/<repo>-MemContinuum-Store`, `beside the git repo the cwd is in`,
  `[A-Za-z0-9._-]+`, `earns its keep`) are absent from SKILL.md.
- `test_store_location_points_at_the_dry_run_verbatim` — asserts the replacement language exists.
- `test_consent_section_names_the_structured_prompt` — asserts "structured multiple-choice prompt"
  and "never prose" in Section 2 (whitespace-normalized, since markdown hard-wraps the phrase
  across a line).
- `test_undecided_names_all_four_options_in_order` — asserts all 4 options present and strictly
  ordered.
- `test_partial_wired_declined_wired_each_get_a_prompt` — asserts each state names its own prompt
  and options, in the order specified.
- `test_wired_and_declined_first_option_keeps_the_recorded_answer` — asserts the actual invariant
  (below), not the looser "first option is always safe" the coordinator's message first stated.
- `test_no_option_ever_deletes_a_store_is_a_stated_rule` — asserts Section 5 states the corrected
  invariant.
- `test_not_a_repo_and_no_config_get_no_prompt` — asserts explicit "no prompt" language.

Checked the other three test modules (`test_repo_init.py`, `test_update.py`, `test_setup.py`) for
any of the five removed phrases or `earns its keep`/`MemContinuum-Store` pins on **SKILL.md
specifically** — all hits found are `test_repo_init.py`'s own tests of `repo-init.sh`'s stdout, not
of the skill file, so no second edit was needed in this commit.

Full `tests/test_docs.py`: 60 tests, 58 passed, 2 skipped (both pre-existing, environment-caused:
no `.claude/skills/*/SKILL.md` render target in this worktree, no `memory/incidents` copy — this
engine repo's own gitignored store, not part of the task).

## S4 — `templates/memcontinuum-rules.md` audit — no change

Read the full template (27 lines). It contains no restated repo-init.sh/mc-registry-lib.sh-computed
value in the audited pattern ("refuses X" / "defaults to Y" / "must match Z") — no store location,
no refusal logic, no project-name regex, nothing from the installer's decision-making at all. Its
content is:
- where to write what (store vs. Claude Code auto-memory) — a *policy* choice, not a code-computed
  fact;
- the authority-label vocabulary (owner-verbatim / owner-ratified / agent-inference /
  reviewer-finding / code-derived) — this is `docs/SCHEMA.md`'s vocabulary, a stable schema
  contract, not something repo-init.sh computes;
- "committing the store reindexes it" — describes the store's own post-commit hook's behaviour
  (a real, stable fact about this system, referenced identically in README.md/INTERNALS.md), not a
  value repo-init.sh derives that could point somewhere else tomorrow.

No edit made. Confirmed via `mc_render_fingerprint` (`scripts/mc-registry-lib.sh:982-1075`) that
`templates/*` is a **`repo`**-scope render input (not `machine`) — so even if this file had needed
an edit, it would only ever flip the *per-repo* fingerprint (the row a repo's own
`memcontinuum-update.sh` walk checks), never the machine-level one. Since it is unmodified, and
`scripts/repo-init.sh` itself is unmodified by this task, the repo-scope fingerprint is unaffected
by this change entirely.

The `skills/memcontinuum/SKILL.md` edits (S1/S2/S3/S5, commit f21d877) **are** a `machine`-scope
render input (`mc_render_fingerprint`'s `machine` branch lists
`skills/memcontinuum/SKILL.md` explicitly, alongside `memcontinuum-setup.sh` and
`scripts/mc_settings_merge.py`). So this task's real effect on the fingerprint machinery is: the
one machine-wide render row goes stale (fixed by `memcontinuum-update.sh --apply --machine`);
per-repo rows are untouched. This corrects the brief's S4 note ("it will flip every wired row to
stale") — that would only be true had `templates/memcontinuum-rules.md` or `repo-init.sh` itself
been edited, which they were not.

## Verify (re-run on the final shipped state, after the review-pass fixes in `4ee6e25`)

All run natively (`PYTHONPATH=` cleared, `MEMCONTINUUM_PYTHON` the pinned venv), foreground,
bounded timeouts, on the `skill-honesty` branch in `~/dev/memcontinuum-skill`. Each command was run
twice across this task -- once after `f21d877`, once after the final commit -- both times clean;
only the final numbers are kept below.

- `python -m unittest tests.test_docs tests.test_repo_init tests.test_update tests.test_setup`:
  **Ran 409 tests in 201.649s -- OK (skipped=2)** (`tests.test_docs` alone: 60 tests, 58 passed, 2
  skipped, of which 8 are `TestSkillHonesty`).
- Full suite, `python -m unittest discover -s tests`: **Ran 1642 tests in 392.713s -- OK
  (skipped=4)**, exit 0. (The `ERROR:`/`WARNING:` lines inside the log are memlint's own
  deliberate-malformed-fixture output, printed by the tests under test, not failures -- the
  summary line is the actual result.)
- `bash tests/run_bash32.sh`: **Ran 665 tests in 327.660s -- OK**, exit 0, closing line
  `== CI SUMMARY: bash 3.2.57 (~/.cache/bash32/bin/bash) -- PASS ==`.

### Two dry runs -- same command, two checkout locations, `skill-honesty` branch's `repo-init.sh`

`/mnt/c` checkout (Windows-mounted -- a throwaway `mc-probeC-<ts>` git repo under
`/mnt/c/Users/User/AppData/Local/Temp`, removed after):
```
note: store defaults to ~/dev/mc-probeC-1788964066-MemContinuum-Store: the checkout is on a Windows-mounted drive, where a store walk costs seconds
note: no --store given -- defaulting to ~/dev/mc-probeC-1788964066-MemContinuum-Store
note: hooks will merge into /mnt/c/Users/User/AppData/Local/Temp/mc-probeC-1788964066/.claude
  store       : ~/dev/mc-probeC-1788964066-MemContinuum-Store
```

`$HOME/dev` checkout (native WSL disk -- a throwaway `mc-probeD-<ts>` git repo under `~/dev`,
removed after):
```
note: no --store given -- defaulting to ~/dev/mc-probeD-1788964066-MemContinuum-Store
note: hooks will merge into ~/dev/mc-probeD-1788964066/.claude
  store       : ~/dev/mc-probeD-1788964066-MemContinuum-Store
```

The `$HOME/dev` run carries no `Windows-mounted` note and no WSL-specific language at all -- just
the plain "no --store given -- defaulting to..." line -- so a Mac- or Linux-shaped checkout (never
on `/mnt/*`, `mc_is_windows_mounted_checkout` false) sees exactly this plain form. Behaviour for
that user is unchanged by this task: repo-init.sh itself was not touched, only the skill's prose
about it, and the skill no longer says anything platform-specific to contradict what a non-WSL
user's own dry-run prints.

