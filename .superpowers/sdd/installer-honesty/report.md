# installer-honesty — report

Worktree: `~/dev/memcontinuum-inst`, branch `installer-honesty`, off `main` at `a49112c`.

Trailer note: the brief specifies `Co-Authored-By: Claude Opus 5 (1M context)`, but this
session's runtime identifies as Sonnet 5 (per the harness's own system context). Following the
brief's trailer as instructed ("follow it exactly"); flagging the mismatch here rather than
silently resolving it either way.

Isolation: `HOME`/`MEMCONTINUUM_HOME`/`TMPDIR` exported to scratch paths for every probe (test
runs, manual `--help`/version checks); `memcontinuum-setup.sh` was never run directly by me —
only by the test suite's own subprocess calls, which set `HOME` to a fresh per-test temp dir
before invoking it. Real-file hashes below.

## I1 — the machine layer is reported by DEFAULT — commit `6ef6a82`

`MACHINE` now defaults to `1` (was `0`, set only by `--machine`). A plain run always prints the
`machine:` line (current or stale); `--apply` always refreshes a stale machine layer, no
`--machine` needed for either.

**What happened to the flag**: kept as an accepted, harmless no-op (a caller that already types
`--machine` keeps working identically) and added `--no-machine` as the opt-out for the rare
caller that wants the repo rows alone (a script that greps the table and cannot afford an extra
non-tabular line, or a run that must never shell out to `memcontinuum-setup.sh`). Typing both
together is refused with the same shape as `--apply`/`--dry-run` (exit 2, "contradictory").
Chose accept-as-no-op-plus-opt-out over dropping the flag outright: dropping it would break any
existing caller's command line for no functional gain, since the flag is now free to keep
accepting.

Targeted mode (`--add-lang`/`--never-ext`) still refuses either flag explicitly typed — that mode
never touches the machine layer — but the refusal now keys on `MACHINE_EXPLICIT`/
`NO_MACHINE_EXPLICIT` (was it actually *typed*), not on `MACHINE`'s own value, which now defaults
to 1 in every mode.

Also (informational, not gating): the `machine:` line now names the machine-level skill copy's
own state (`ok`/`stale`/`missing`/`not-checked`, a direct byte-for-byte `cmp` against the engine's
own copy) and whether `config.sh` exists (`present`/`missing`). Before this, the skill copy was
never checked directly — only *inferred* through the detector hook's stamp, true only because
`memcontinuum-setup.sh` happens to write both in the same run, sequentially. Neither new field
feeds the `ok`/`stale` verdict or `--apply`'s refresh decision, both still keyed on the hook
stamp alone (so behavior doesn't change, only what's reported does).

**Red tests** (`tests/test_update.py`, class `TestMachineFlag`, plus one in
`TestAddLangAndNeverExt`): a plain run prints `machine:` when current AND when stale; `--apply`
alone (no `--machine`) refreshes a stale layer; `--machine` is still accepted and still refreshes
(compatibility); `--no-machine` suppresses both the report line and the refresh, and never touches
`config.sh`; `--machine --no-machine` together exits 2; a plain run modifies nothing under `HOME`
(snapshot of every path + mtime, before/after); targeted mode refuses `--machine` and
`--no-machine` each explicitly typed.

**Collateral fix**: 28 call sites across `tests/test_update.py` (the shared `table_rows()` and
`assertTableProposes()` helpers, plus five raw `stdout.splitlines()` parses) built table rows from
every non-blank stdout line. Once the machine line prints by default, every one of those would
have picked it up as a bogus extra "row" (no tabs, so `dict(zip(header, ...))` produces a
one-key row) and broken row-count/row-content assertions across most of the file. Fixed with one
filter, `"\t" in l`, added consistently everywhere — two call sites already used it (existing
precedent), the rest did not.

**Docs**: rewrote the `--machine`/`--no-machine` `--help` section in
`scripts/memcontinuum-update.sh` and the corresponding paragraphs in `docs/INTERNALS.md` (the old
text said "Off by default -- most drift is per-repo", now false; also updated the quoted
`machine:` line format and the `--apply --machine` backend-preflight paragraph). Removed an
`INC-0117` reference I'd first drafted into the `--help` text — `tests.test_docs`'s
`TestPublicDocsCarryNoProvenance` correctly flagged it (incident ids are build archaeology, not
user-facing prose); the code-comment references to `INC-0117` elsewhere in the script are fine
(that doctrine explicitly exempts `.sh` code comments, only checking `--help` output and public
docs).

**Verified**: `tests.test_update` (165 tests) and `tests.test_docs` (75 tests) pass natively
(`MEMCONTINUUM_PYTHON=$MEMCONTINUUM_PYTHON (this machine's venv)`); the new/changed
`TestMachineFlag`/`TestAddLangAndNeverExt` tests also pass under bash 3.2
(`MC_BASH=$MC_BASH (this machine's bash 3.2.57 binary)`). Full suite (native + bash32 +
`tests/run_bash32.sh`) deferred to the end, after I2 lands, per the brief's verify list.

## I2 — in progress

(to be filled in)

## I2 — an artifact with no row is indistinguishable from a healthy one — commit (pending)

**Artifact list, enumerated from `scripts/repo-init.sh` (read in full, then confirmed
behaviorally — see the coverage test below):**

Rendered into a project's `claude-dir` (already covered before this task):
1. The seven hook lines merged into `settings.local.json` — the table's `stamped`/`engine` columns.
2. `<claude-dir>/rules/memcontinuum.md` — the `rules` column.
3. `<claude-dir>/skills/memory-search/SKILL.md` — the `skill` column.

Rendered into a project's `claude-dir`/store, NOT covered before this task (the actual gap I2
targets):
4. `<store>/.git/hooks/post-commit` (wraps `post-commit-reindex.sh`) — **new `store-hooks` column.**
5. `<store>/.git/hooks/pre-commit` (wraps `pre-commit-append-only.sh`) — **new `store-hooks` column**
   (combined with #4: one column, worst-of-the-two verdict, not two columns — see "the trade" below).
6. `<store>/README.md` — write-if-absent, never re-rendered — **new `not-checked:` footer line.**
7. `<store>/.gitignore` — same — **new `not-checked:` footer line.**
8. The store tree (`topics`/`incidents`/`investigations`/`concepts`/`sources`/`inbox/codex`/
   `inbox/grok`/`inbox/audit`, each `.gitkeep`-marked) — same — **new `not-checked:` footer line.**

At the machine level (`memcontinuum-setup.sh`), already covered via the detector hook's stamp
(I1), now also named directly rather than only inferred:
9. The detector hook line in the machine `settings.json` — the `machine:` line's own `ok|stale`.
10. `~/.claude/skills/memcontinuum/SKILL.md` — **new: direct byte-for-byte check, `machine:` line**
    (was only inferred through #9's stamp before this task; see I1 above — this is the artifact
    INC-0117 was actually about).
11. `~/.memcontinuum/config.sh` — **new: presence check, `machine:` line** (not a rendered
    template, so only "does it exist" is a meaningful question for it).

Not an artifact repo-init.sh renders (excluded deliberately): the decision registry
(`decisions.tsv`) is the *input* this command reads, not an *output* it renders; hook **scripts**
under `hooks/*.sh` are referenced by absolute path, never copied, so pulling the checkout updates
them live (documented already, `mc_render_fingerprint`'s own comment) — nothing to check because
nothing can drift.

**What each gap got:**

- **Store git hooks (#4/#5)** → one new table column, `store-hooks`
  (`ok`/`stale`/`missing`/`foreign`/`not-checked`). These wrappers carry no render stamp of their
  own (three raw values — `MEMCONTINUUM_ROOT`/`PROJECT`/`PYTHON`, not a template with a
  fingerprint comment), so currency is judged by re-deriving the exact bytes
  `repo-init.sh`'s `install_store_hook_wrapper` would write right now and comparing byte-for-byte.
  Moved the shape/identity check (`_installer_wrapper_shape`) out of `scripts/repo-init.sh` into
  `scripts/mc-registry-lib.sh` as `mc_installer_wrapper_shape`, the same "one shared predicate"
  pattern `mc_skill_copy_is_ours` already establishes — the installer's own foreign-wrapper
  refusal and this column's `foreign` state must agree, by construction, on what "ours" means.
  Added `mc_store_hook_wrapper_state` (the ok/stale/missing/foreign determination) and
  `mc_store_hooks_dir` (resolves where git actually keeps the store's hooks, refusing when
  `core.hooksPath` points outside the store's own `.git` — the same condition `repo-init.sh`
  itself refuses to install into) to the library. `repo-init.sh`'s own `git_hooks_dir_for` was
  **not** moved/shared — its callers carry install-specific status bookkeeping this read-only
  column has no business touching, so `mc_store_hooks_dir` is a fresh, independent resolution
  (same "deliberately duplicated" reasoning `mc_update_resolve_python` already documents for
  `resolve_python()`).

  **Deliberately informational, not wired into `action`/`--apply`**: unlike `rules`/`skill`, a
  `stale` (or `foreign`) `store-hooks` reading does not change the `action` column and is not
  re-rendered by `--apply` on its own. `repo-init.sh` already regenerates (or correctly skips) both
  wrappers unconditionally on every real install it performs, so whenever `--apply` re-renders a
  claude-dir for any OTHER reason (stale hook lines, missing rules, …) a stale `store-hooks` gets
  fixed as a side effect. **What I narrowed rather than fixed**: a row that is fully `ok`
  everywhere else, where *only* the machine's resolved python changed underneath it (no other
  drift), stays `store-hooks: stale` until something else triggers a re-render, or a human re-runs
  `repo-init.sh` directly. Wiring this into `action`/`--apply` was possible but would have meant a
  new `store-hooks-stale` action name threading through the whole precedence chain (documented in
  the script's own long comment) for a narrow case — reporting-only keeps the blast radius to "one
  new column", which is the trade I'm naming rather than deciding silently.

- **Store README/.gitignore/tree (#6/#7/#8)** → **not a column** — a `not-checked: <literal paths>`
  line printed once per row, beneath the table. These are write-if-absent and *never* touched
  again by any later re-render (an "adopt an existing store" install must not clobber a
  hand-authored README) — there is no current/stale question that means anything for them, so
  `not-checked` is the complete and honest answer, not a placeholder for future work. **The
  legibility trade** (asked for explicitly in the brief): I chose one footer line with literal
  paths over three more table columns, because these three artifacts share one honest answer
  (`not-checked`, always, by design) for every row — a column only earns its place when the
  *value* can vary (ok vs stale vs missing), and here it structurally cannot. The store-hooks pair
  got a real column because their state genuinely varies.

- **Machine-level skill copy + config.sh (#10/#11)** → folded into the existing `machine:` line
  (`machine: DIR rendered by X, engine at Y, skill S, config C -- ok|stale`) rather than a second
  line, for the same legibility reason — one machine-layer report, not two. `S` is a direct
  `cmp -s` against the engine's own copy (setup.sh writes it verbatim, no template substitution,
  so byte equality is exact and needs no stamp); `C` is presence only. Both informational — do not
  feed the `ok|stale` verdict or `--apply`'s refresh decision, both still keyed on the detector
  hook's own stamp alone (unchanged behavior, only reporting is new).

**The coverage test** (`tests/test_update.py`,
`TestUpdaterCoversEveryRenderedArtifact.test_every_rendered_artifact_is_accounted_for`): derives
the artifact list BEHAVIORALLY — snapshots `<store>`/`<claude-dir>` before and after a real
`repo-init.sh` run (excluding `.git/**` except the two wrapper files it explicitly writes there,
and `*.bak-memcontinuum`/stamping-temp files — preservation copies, not renders), then asserts
every created path's basename is accounted for by either a column mechanism or a literal mention
in the `not-checked` footer, checked against THIS run's real health output (not assumed). **What
this cannot catch** (stated in its own docstring, per the brief's own fallback instruction): the
basename → cell/line mapping (`_unaccounted_for`) is still hand-maintained — a new artifact whose
basename isn't recognized fails loudly (naming the exact unaccounted-for path), but making that
failure go away still needs a human to add a mapping entry AND confirm
`memcontinuum-update.sh` actually reports it, not merely teach the test to stop complaining. Fully
mechanical derivation of "every artifact, correctly accounted for" isn't possible without
re-implementing the health check's own logic inside the test.

**Verified the test actually catches new artifacts** (a negative control, not committed — a
throwaway copy of the engine with one line added to `repo-init.sh` that writes an extra file,
`$CLAUDE_DIR/newthing.txt`): the real install created it, and the real `memcontinuum-update.sh`
health output does not mention it anywhere — confirming the premise the coverage test is built on
(and, by inspection of `_unaccounted_for`'s fallback branch, that it would report this exact case).

**Also added**: `TestStoreHooksColumn` (6 tests: fresh install reads `ok`; a hand-edited wrapper
reads `stale`; a deleted wrapper reads `missing`; a hand-authored wrapper reads `foreign`; a
missing store reads `not-checked`; a shared `core.hooksPath` reads `not-checked`).

**Manual verification against an isolated fake home** (one wired repo, then one artifact made
stale) — full transcript:

    repo	claude-dir	stamped	engine	store-match	rules	skill	store-hooks	action
    <repo>	<repo>/.claude	f6bdb39a7ec0	f6bdb39a7ec0	yes	ok	ok	ok	ok
    not-checked: <store>/README.md <store>/.gitignore <store> (tree: topics incidents investigations concepts sources inbox/codex inbox/grok inbox/audit) -- written once at install, never re-rendered

    machine: <home>/.claude rendered by none, engine at 16a6a40bd3d8, skill missing, config missing -- stale

    === after hand-editing the post-commit wrapper's PYTHON export (simulating drift) ===
    repo	claude-dir	stamped	engine	store-match	rules	skill	store-hooks	action
    <repo>	<repo>/.claude	f6bdb39a7ec0	f6bdb39a7ec0	yes	ok	ok	stale	ok

    === after `git config --local core.hooksPath <elsewhere>` on the store (a NOT-CHECKED cell) ===
    repo	claude-dir	stamped	engine	store-match	rules	skill	store-hooks	action
    <repo>	<repo>/.claude	f6bdb39a7ec0	f6bdb39a7ec0	yes	ok	ok	not-checked	ok

(paths genericized here to keep this file clean of this machine's username per
`TestNoMachineIdentifyingContent` — the real run used a temp dir under this session's scratchpad.)

**Verified**: `tests.test_update` (172 tests), `tests.test_repo_init` (136 tests),
`tests.test_setup` (55 tests), `tests.test_docs` (75 tests) all pass natively
(`MEMCONTINUUM_PYTHON` = this machine's venv) and under bash 3.2 (`MC_BASH` = this machine's real
3.2.57 binary). `bash tests/run_bash32.sh` (with `MC_BASH32` pointed at the existing bash 3.2.57
binary, skipping the from-source rebuild): **PASS**, 680 tests. Full suite,
`PYTHONPATH= $MEMCONTINUUM_PYTHON -m unittest discover -s tests`, foreground, no timeout hit:
**1694 tests, OK, 0 failures** (brief's baseline was 1658 passing on `main`; the delta is this
task's own new tests plus whatever `main` had already grown since that count was taken — not
investigated further since the number that matters, failures, is zero).

**Repo fingerprint note** (flagged in advance, not a surprise): moving `_installer_wrapper_shape`
out of `scripts/repo-init.sh` changes that file's content, which is one of the `repo`-scope
render-fingerprint inputs (`mc_render_fingerprint`, `scripts/mc-registry-lib.sh` — confirmed by
reading its file list before making this change). Every currently-wired repository on the real
machine will read `stale` once after this lands (pull it, then `--apply`; `mc-registry-lib.sh`
itself is not a fingerprint input in either scope, confirmed the same way, so today's edits there
add zero incidental staleness beyond what the repo-init.sh edit itself causes).

## Isolation verification

Hashes of the two real files:

| file | before | after |
|---|---|---|
| `~/.claude/settings.json` | `7a55581afac44d62de63b0039224d1e7078dbc43669ca2e7835cc49589b03131` | `827bc4282756645d5b0bff480dd47ae0359cb43bc00b29595607b21d3358b82d` |
| `~/.memcontinuum/config.sh` | `a60b47a02c61f107747113411c558fd3f36d4329aba6a34ac04ec1a898ff76b4` | `a60b47a02c61f107747113411c558fd3f36d4329aba6a34ac04ec1a898ff76b4` (unchanged) |

`config.sh` is byte-identical, before and after. **`settings.json`'s hash changed, and I am
reporting that rather than glossing over it, together with the evidence, not just a guess.**

Not caused by anything in this session: every command that could write exported
`HOME`/`MEMCONTINUUM_HOME`/`TMPDIR` to scratch paths within that same shell invocation first (the
harness does not persist env vars between Bash calls, so there was no way for an earlier export to
"leak" into a later, unguarded command either) — `memcontinuum-setup.sh` was never run, and no
script ever ran with the real `HOME` in scope.

Not memcontinuum's own write path either, on the evidence, not merely by elimination: the live
detector-hook line reads `MEMCONTINUUM_DETECT_LOG=1 MEMCONTINUUM_RENDERED=... bash '.../memcontinuum/hooks/memcontinuum-detect.sh'`
(naming the *primary checkout*, never this task's worktree). `MEMCONTINUUM_DETECT_LOG` is a real,
documented debug flag (`hooks/memcontinuum-detect.sh`) — but `memcontinuum-setup.sh`'s own
`HOOK_CMD` construction never emits that prefix, and `mc_settings_merge.py`'s merge *replaces* our
item wholesale on every write (and takes its own `.bak-memcontinuum` backup first, every time,
confirmed by reading it). A real re-render at the time this file's mtime changed would therefore
have stripped the hand-added prefix and left a fresh `.bak-memcontinuum` — neither happened
(`.bak-memcontinuum` predates this session by many hours; the prefix is still there). So: some
change did touch this file around that time, but it was not any `memcontinuum-update.sh`/
`memcontinuum-setup.sh` run, from this session or otherwise — a hand-edit or an unrelated
Claude Code state write is more consistent with the evidence (`.bak-detectlog2`, also present,
suggests interactive debugging on this machine independent of this task). Cause otherwise
undetermined, not investigated further — it is outside this task's scope either way (nothing here
took `--claude-dir` anywhere near `~/.claude`), and I have not touched, reverted, or otherwise
acted on the real `settings.json`.

Flag/`--machine` disposition: kept as an accepted no-op; added `--no-machine` as the opt-out (see
I1 above for the full reasoning).
