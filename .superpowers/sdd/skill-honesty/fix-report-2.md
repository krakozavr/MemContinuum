# Fix round 3 (skill-honesty re-gate) -- report

Brief: `.superpowers/sdd/skill-honesty/fix-brief-2.md`. Read `codex-regate.md`
(BLOCK) and `grok-regate.md` (MERGE AFTER FIXES) in full first; both agree on
the substance.

Worktree: `/home/krakozavr/dev/memcontinuum-skill`, branch `skill-honesty`,
starting tip `511c6fe`.

## Commits

- `2debfdf` fix(skill): stop denying a capability the product has (C1, C3)
- `01c58be` docs(readme): make Uninstall's project-level steps complete and honest (C2)
- `ae72bdc` test(skill-honesty): mutation tests run the real production assertion (C4)

## C1 -- the false "no safe automated path" claim

`SKILL.md:102` (in the `wired` state's prompt section) and `SKILL.md:248`
(step 4's reversal section) said moving a store or changing its code-root
set had "no safe automated path" and that uninstall was "the only safe
path". False: `repo-init.sh --adopt-only` wires an EXISTING store
(including one already relocated by `mv`/`git mv`) without creating one,
and `repo-init.sh` always replaces prior wiring with the invocation's
COMPLETE set. Both reviewers proved this end to end under isolation.

New wording (step 4, the primary fix; step 2's `wired` note points here):

> - wired → a different store location, or a different code-root set: **this
>   skill does not drive it** — it has no read-back it can trust for the
>   repo's complete current parameter set, and handing `repo-init.sh` an
>   incomplete one silently drops existing wiring (the exact failure mode the
>   removed relocation/add-retrieval options hit). The product itself
>   supports both changes: `repo-init.sh` always replaces prior wiring with
>   the invocation's COMPLETE set, and `--adopt-only` wires an EXISTING
>   store without creating one — including a store already relocated by hand
>   (`mv`/`git mv` it first, then point `--store` at the new path). The
>   honest, human-directed path: read the repo's current `--project`/`--store`
>   off step 1's own `project=`/`store=` lines, and its current code roots and
>   languages off the `MEMCONTINUUM_CODE_ROOTS=`/`MEMCONTINUUM_LANG_EXTS=`
>   values already in `<claude-dir>/settings.local.json`'s `PreToolUse`
>   command lines, then re-run `bash "$ENGINE/scripts/repo-init.sh"
>   --adopt-only --store DIR --project NAME --code-root ... --langs ...`
>   naming that complete set plus whichever root or store path is changing —
>   dry-run first, per step 3. This is a deliberate command run at the
>   human's direction, not a step 2 prompt option.

What did NOT change: step 2 still does not offer relocation/add-retrieval
as a prompt option (the brief said that part stands -- there is genuinely
no read-back the skill can trust to reconstruct a complete parameter set on
its own). What changed is that the skill now says *why* it won't drive this
itself, and names the actual supported command instead of denying the
product can do it.

Also fixed the Rules section (`## 5. Rules`), which said "this skill has no
flow that relocates a store" -- now directly contradicted by step 4
documenting exactly that flow. Reworded to what is actually true: the skill
never decides or automates a relocation on its own judgment; step 4's path
is a deliberate, human-directed command.

Also fixed a stale restatement (Grok finding 3, `SKILL.md:74`): the prose
said the dry-run names a `code roots :` line for retrieval -- live output
only ever prints that plural line for a rationale-only (no code root)
install. A retrieval install prints one `code root   :` line per root.
Confirmed against a live dry-run in the final verification probe below.

## C2 -- README's uninstall didn't contain what SKILL.md said it did

`SKILL.md:154` called README's Uninstall step 1 a list of hook basenames
applicable in reverse; `README.md:529` had no such list. `SKILL.md:250`
said the README's uninstall covers the rules file and store git hooks;
README omitted `rules/memcontinuum.md` and the store's `pre-commit` hook
(only `post-commit` was listed).

`repo-init.sh`'s own completion output ("Next steps" step 3) is the honest
source -- confirmed live in the verification probe below, it lists all
seven hook basenames, the rules file, and both store git hooks. README's
project-level Uninstall now matches it:

- Step 1 names the seven basenames explicitly (`pre-edit-chain.sh
  newfile-nudge.sh ledger-post-edit.sh precompact-persist.sh
  sessionstart-remind.sh userprompt-remind.sh sessionend-stamp.sh`).
- New step 3 deletes `<claude-dir>/rules/memcontinuum.md`.
- Step 4 (was step 3) deletes both `post-commit` and `pre-commit`.

SKILL.md's own reference to "README.md 'Uninstall' step 1" (for the
`partial-wired` "Remove what is there" path) still points at step 1, whose
number did not change -- left as is. The "hand-adding ... README.md
'Uninstall' step 1's list, in reverse" phrasing at the old C3 location was
removed entirely (see C3 below) rather than kept accurate, since step 4's
complete-set path is the honest fix for that case regardless of what
README's step 1 says.

## C3 -- the stranded half-wired state

`SKILL.md:152-157` (step 3, "Complete the wiring" on a `partial-wired`
repo when retrieval already exists) refused the bare repair correctly
(omitting existing `--code-root`/`--langs` would erase retrieval) but then
said "there is no safe automated repair for that case today" and pointed at
a nonexistent README list -- a dead end.

Fixed to give it the same route as C1: read the existing
`--code-root`/`--langs` values off the `PreToolUse` command lines in
`settings.local.json` (they carry `MEMCONTINUUM_CODE_ROOTS=`/
`MEMCONTINUUM_LANG_EXTS=` verbatim), then take step 4's complete-set
`repo-init.sh --adopt-only` path. Same fix applied to the parallel case in
step 2's `wired` section (`decision=wired` + `wiring=partial`, previously
"there is no third option that fixes it" with no pointer anywhere) --
now explicitly says step 4's path repairs it too.

## C4 -- the mutation-test class proved fixture coverage, not production proof

Codex 5 / Grok 5: `TestSkillHonestyMutations` mostly re-implemented each
assertion inline instead of calling the `TestSkillHonesty` method it
claimed to prove. Codex demonstrated this by replacing every production
honesty test with a no-op in memory and rerunning the class: all eight
still passed.

Rewrote every test in that class to use a new `_mutated_skill()` context
manager, which points the shared module-level `SKILL` global at a temp
file holding the mutated text, then calls the actual bound
`TestSkillHonesty` test method by name -- the same method the real suite
runs, which reads `SKILL.read_text()` itself. A future edit that weakens
or deletes the underlying production assertion now fails the corresponding
mutation test too, because there is no second copy of the logic left to
keep passing on its own.

Also widened two `FORBIDDEN_PATTERNS` entries that a live mutation run
(per the regate reports) showed missing plausible rewordings:

- the `--store`/`--claude-dir` pairing rule: only `requires?` was caught;
  "must be paired with" was not. Widened to
  `requires?|must\s+(?:be\s+)?(?:paired|accompanied)|needs?`.
- the hardcoded hook-count rule: only the literal `five hooks` was caught;
  "five write-side hooks" was not. Widened to allow up to two words between
  "five" and "hooks".

Two new mutation tests (`test_widened_hook_count_rewording_is_caught`,
`test_rewording_the_store_claude_dir_pairing_without_requires_is_caught`)
exercise both through the real production assertion.

What I narrowed rather than fixed: the class's docstring already discloses
that `FORBIDDEN_PATTERNS` remains a finite pattern list, not a semantic
diff against the tool's own source -- that limitation stands. I did not
attempt to catch every plausible rewording (e.g. the nested-repo refusal
message reworded without "inside an existing git repo", Grok's N5) --
that is exactly the class of rewording a finite pattern list cannot
practically close, and the brief said to disclose that rather than chase
it.

## Verify

```
$ PYTHONPATH= $MEMCONTINUUM_PYTHON -m unittest tests.test_docs tests.test_repo_init tests.test_update tests.test_setup
Ran 425 tests in 200.422s
OK (skipped=2)

$ PYTHONPATH= $MEMCONTINUUM_PYTHON -m unittest discover -s tests
Ran 1657 tests in 409.129s
OK (skipped=4)
```

1657 = the coordinator's measured 1655 passing on tip `511c6fe`, plus the
two new mutation tests added in C4. Exit 0, foreground, no failures.

```
$ bash tests/run_bash32.sh
Ran 665 tests in 351.519s
OK
== CI SUMMARY: bash 3.2.57 (...) -- PASS ==
```

### Reproducing the reviewers' own probe (C1)

Under fully isolated `HOME`, `MEMCONTINUUM_HOME`, and `TMPDIR` (all under a
scratch dir; `memcontinuum-setup.sh` was never run -- `MEMCONTINUUM_HOME`'s
`config.sh` was hand-written to match its documented format, pointing
`MEMCONTINUUM_ENGINE` at this checkout):

1. Installed a fresh repo wired to code root A only (`--record-decision`).
2. Added a second root (`--adopt-only --code-root A --code-root B`,
   dry-run then real) -- dry-run's plan printed `code root   :` once per
   root, confirming the C1 wording fix.
3. `mv`'d the store to a new path, then re-ran `--adopt-only --store
   <new path> --code-root A --code-root B` to re-wire it there.
4. Removed root A (`--adopt-only --store <new path> --code-root B` only --
   the complete desired set, naming just B).

Final `memcontinuum-state.sh` output:

```
engine=/home/krakozavr/dev/memcontinuum-skill
python=/home/krakozavr/dev/mem-venv/bin/python
global_ask=on
repo=<scratch>/repoA
key=<scratch>/repoA
wiring=full
decision=wired
decided_at=2026-09-09
settings=<scratch>/repoA/.claude/settings.local.json
store=<scratch>/storeA-moved
project=probeA
project_source=registry
state=wired
```

`settings.local.json`'s `PreToolUse` lines carried
`MEMCONTINUUM_CODE_ROOTS='["<scratch>/repoA/codeB"]'` only (root A cleanly
dropped, root B retained -- no data loss), and all seven always-wired hook
basenames were present. `repo-init.sh`'s own "Next steps" completion output
matched the README fix exactly: seven basenames, the rules file, both store
git hooks.

One thing this probe surfaces but is out of this round's scope: the
registry's `decisions.tsv` note field (written by the initial
`--record-decision` call) still showed the *original* store/code-root, since
none of steps 2-4 re-ran `memcontinuum-decide.sh` to update it -- only
`settings.local.json` stayed current. This is exactly why the C1/C3 fixes
point at `settings.local.json`'s `PreToolUse` lines as the read-back
source, never the registry.

Real machine layer (`~/.claude/settings.json`,
`~/.claude/skills/memcontinuum/SKILL.md`, `~/.memcontinuum/config.sh`,
`~/.memcontinuum/decisions.tsv`) hashed unchanged before/after; zero
matches for the scratch probe's key in the real `decisions.tsv`. Scratch
directory removed after the probe.

## Follow-up (advisor review, commit `7caecec`)

A pre-completion advisor pass on this report caught two followability
defects in the C1/C3 template, neither of which the verification probe
above had exercised (every probe command happened to pass `--claude-dir`
explicitly already):

- `repo-init.sh --help` is explicit that an EXPLICIT `--store` with no
  `--claude-dir` is a hard error. Step 4's command template omitted
  `--claude-dir` entirely. Fixed: added it to the template, and named
  where a human finds it (step 1's own `settings=` line's directory).
- The C3 branch (repairing a `partial-wired` repo that already has
  retrieval) pointed at step 4's path and stopped there; step 4 ends at
  "dry-run first, per step 3" and never mentions recording the decision --
  correct for an already-`wired` repo (step 4's usual case) but wrong for
  `partial-wired`, which starts at `decision=none`. Fixed: both the C3
  branch and step 4 now say to record the result, matching step 3's "Yes
  -> initialize" flow.

Also corrected "mv/git mv it first" to plain "mv" -- the store is its own
git repository, not something `git mv` from the code repo's working tree
would touch.

Re-ran `tests.test_docs` only (75 tests, OK, skipped=2) -- the widened
`FORBIDDEN_PATTERNS` entries from C4 do not false-positive on `--claude-dir`
written as a plain flag in the corrected template. The full suite and
bash-3.2 harness were not re-run for this follow-up: it changes SKILL.md
prose only, covered entirely by `tests.test_docs`.
