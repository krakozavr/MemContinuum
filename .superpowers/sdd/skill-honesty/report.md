# skill-honesty — report (in progress)

Worktree: `/home/krakozavr/dev/memcontinuum-skill`, branch `skill-honesty`, off `main` at `b1d6ac5`.
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
2. **Store-must-be-its-own-git-repo / nested-repo refusal / foreign-repo-markers refusal** — the
   refusal's *existence* is kept (one line: it's real context a human choosing a custom `--store`
   benefits from), but the *mechanism* (which markers `mc_is_marked_store` checks, the `--force`
   detail) is dropped — the refusal message names its own fix.
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
5. Trimmed but not deleted: "five hooks" language softened to "hooks wired into the project's
   `.claude` settings (the dry-run's plan lists exactly which, and how many)" — removes a count that
   `scripts/mc-registry-lib.sh` owns (`the five ALWAYS-wired write-side hooks`) from a place where
   the dry-run's plan already prints the same information.

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

### S5 — owner follow-up: every state gets a prompt, safe option first

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
  rewrites-then-appends the row — replace, not union, unlike `--record-decision`'s own union).

Also resolved a self-contradiction the new "change where the store lives" flow created against the
existing absolute rule "Never delete or move an existing store": narrowed it in Section 5 to exempt
only that one human-chosen flow, matching the pattern the other absolute rules already use ("never
initialize *without an explicit yes*").

Section 5 gained two explicit rules: "one question, no advocacy" now stated as governing every
state (not just `undecided`), and "the first option is always the one that changes nothing" as its
own rule.

### S3 — tests (`tests/test_docs.py`, class `TestSkillHonesty`, 7 tests)

Added to the existing `tests/test_docs.py` (the doctrine-pinning home per its own docstring),
following the module's established style (see `TestDocsRound7`). All 7 pass; slicing is done
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
- `test_partial_wired_declined_wired_each_get_a_prompt_with_safe_option_first` — asserts each
  state's first option text.
- `test_first_option_never_changes_anything_is_a_stated_rule` — asserts Section 5 states the
  invariant.
- `test_not_a_repo_and_no_config_get_no_prompt` — asserts explicit "no prompt" language.

Checked the other three test modules (`test_repo_init.py`, `test_update.py`, `test_setup.py`) for
any of the five removed phrases or `earns its keep`/`MemContinuum-Store` pins on **SKILL.md
specifically** — all hits found are `test_repo_init.py`'s own tests of `repo-init.sh`'s stdout, not
of the skill file, so no second edit was needed in this commit.

Full `tests/test_docs.py`: 59 tests, 57 passed, 2 skipped (both pre-existing, environment-caused:
no `.claude/skills/*/SKILL.md` render target in this worktree, no `memory/incidents` copy — this
engine repo's own gitignored store, not part of the task).

## Still to do
- S4 audit of `templates/memcontinuum-rules.md`
- `tests.test_docs tests.test_repo_init tests.test_update tests.test_setup` natively
- Full suite in foreground (600000 ms timeout)
- `bash tests/run_bash32.sh`
- Two dry-run verifications (`/mnt/c` path and `$HOME` path)

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
