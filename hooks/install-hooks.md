# Wiring the hooks

`scripts/repo-init.sh` renders and merges all of this automatically (see the README's
"Install → Once per repository") — this document explains what it wires and
why, for anyone reading the generated `settings.local.json`, adapting it for
a harness other than Claude Code, or wiring by hand instead of using the
installer.

## 1. `pre-edit-chain.sh` — Claude Code `PreToolUse` hook

Lives in this repo (`hooks/pre-edit-chain.sh`) and is wired into a *project's*
Claude Code settings (`.claude/settings.json` or `.claude/settings.local.json`),
never into the tool repo itself — the tool stays project-agnostic; the env
values below are what make one instance of it specific to a project.

Rendered from `templates/pre-edit-hook.json.tmpl` + one
`templates/code-root-filter-pair.json.tmpl` pair per `--code-root`, merged
into the target `hooks.PreToolUse` array:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "if": "Edit(/<code-root>/**)",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_STRIP_PREFIX=<code-root>/ MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/pre-edit-chain.sh",
            "timeout": 5
          },
          {
            "type": "command",
            "if": "Write(/<code-root>/**)",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_STRIP_PREFIX=<code-root>/ MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/pre-edit-chain.sh",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

Notes:
- `MEMCONTINUUM_STRIP_PREFIX` is required whenever a topic's `code_refs` are written relative to a
  checkout that the edited file's absolute path doesn't share a `cwd` with (a real `PreToolUse`
  payload's `file_path` is always absolute, while `code_refs` are written repo-relative) — see the
  "engine request" comment at the top of `pre-edit-chain.sh` for why this is needed at all.
- `MEMCONTINUUM_HOME` is deliberately omitted here so the hook resolves it the same way
  `config.sh` does: default to `~/.memcontinuum`, then follow the pointer written there into the
  real home if `memcontinuum-setup.sh` was run with a custom one (see `docs/INTERNALS.md`
  "Python resolution and `config.sh`" — the pointer case) — set it explicitly only if the derived
  index should live somewhere else. It must never point at a synced/cloud-backed filesystem —
  SQLite locking is not reliable there.
- The command line, not a JSON `env` block, carries the env vars — Claude Code hook `command`
  entries run through a shell, so `VAR=value ... command` works directly.
- `"timeout": 5` is Claude Code's own per-hook backstop (default 600s when unset); the mechanism
  meant to fire is the INNER watchdog `hooks/mc-watchdog.sh` wraps this hook in (see
  `docs/INTERNALS.md` "The watchdog"), which times out well before this outer value and, unlike
  the outer one, still returns a real `additionalContext` explaining that retrieval timed out.
- `if` filter paths use Claude Code's permission-rule syntax, where a single leading slash
  anchors at the settings source, not the filesystem root (docs: `Edit(//Users/alice/file)` =
  absolute `/Users/alice/file`). `<code-root>` above is always an absolute path (already starting
  with `/`), so the rendered pattern needs a SECOND leading slash — `Edit(//home/…/**)` — to match
  anything at all. With only one leading slash the hook never fires in a real session, so
  `code-root-filter-pair.json.tmpl` and `newfile-nudge-filter-pair.json.tmpl` compose `if` as
  `Edit(/{{CODE_ROOT}}/**)` / `Write(/{{CODE_ROOT}}/**)` — the rendered value always has exactly
  two leading slashes.
- There is no `Bash` matcher here, and none is added: an `if` condition like `Edit(...)`/
  `Write(...)` names a file path a tool is about to touch, and an arbitrary shell command has no
  single such path to filter on before it runs. A file changed from the shell gets no pre-edit
  lookup at all — section 4's `ledger-post-edit.sh` is what records it, but only afterwards, from
  a tree diff, never before the change the way this hook works for `Edit`/`Write`.

## 2. `newfile-nudge.sh` — Claude Code `PreToolUse` hook (Write only)

Lives in this repo (`hooks/newfile-nudge.sh`), wired into the same project settings as
`pre-edit-chain.sh` above but as a SEPARATE `PreToolUse` matcher group (`"Write"`, never
`"Edit|Write"` — this hook only ever fires on a path that does not exist yet; an edit to an
existing file is `pre-edit-chain.sh`'s job, not this one's). Deliberately minimal wiring: no
`MEMCONTINUUM_ROOT`/`STRIP_PREFIX` is rendered. The hook checks that the write target is new, under a
configured code root, and has an indexed source extension, then injects one reminder line. When the path yields a query it also makes one read-only `memidx.py search` against the project's index (found
from `MEMCONTINUUM_PROJECT` and `MEMCONTINUUM_HOME`) on the path's own words and appends the nearest
decisions as a guess; because `MEMCONTINUUM_ROOT` is not exported, that call carries no `--root` and
reports no staleness. It DOES carry `MEMCONTINUUM_PROJECT`
— identity (and the index file name for that search), so `scripts/repo-init.sh`'s merge step can tell this project's nudge entry apart
from a different project's sharing the same `--claude-dir`; the hook reads it only to locate that index. A
nudge entry carrying no `MEMCONTINUUM_PROJECT` at all (wired before this marker existed) stays
sweepable by ANY project's re-run until the project that owns it re-runs `repo-init.sh` — see the
note at the end of this file.

Rendered from `templates/newfile-nudge-hook.json.tmpl` + one
`templates/newfile-nudge-filter-pair.json.tmpl` pair per `--code-root`, merged into the SAME
target `hooks.PreToolUse` array as `pre-edit-chain.sh` (a second group, not a second array):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write",
        "hooks": [
          {
            "type": "command",
            "if": "Write(/<code-root>/**)",
            "command": "MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/newfile-nudge.sh"
          }
        ]
      }
    ]
  }
}
```

Notes:
- `MEMCONTINUUM_HOME` is deliberately omitted here, same reason as `pre-edit-chain.sh` above.
- Runs under the shared `hooks/mc-watchdog.sh` wall-clock watchdog like the five write-side hooks
  below, even though its own logic never calls python for real work — one shared mechanism, not a
  second bespoke timeout story for the one hook that happens to be fast.

## 3. Git hooks in the STORE repo — `post-commit-reindex.sh` and `pre-commit-append-only.sh`

Lives in this repo too, but runs as a `post-commit` hook inside the **store**
repo — not this tool repo, and not the code repo it describes.

`scripts/repo-init.sh` writes this wrapper as `<store>/.git/hooks/post-commit`
(not a bare symlink — a symlinked git hook carries no environment of its
own, and `post-commit-reindex.sh` silently no-ops without `MEMCONTINUUM_ROOT`
set). A `post-commit` this installer did not render is left untouched and
reported, never silently overwritten — the same foreign-hook refusal §3b
describes for `pre-commit`, identity anchored on the complete five-line
wrapper this installer renders (the shebang, the three `MEMCONTINUUM_*`
exports, and the `exec` line), never the `exec` line alone:

```bash
#!/usr/bin/env bash
export MEMCONTINUUM_ROOT="<store>"
export MEMCONTINUUM_PROJECT="<project>"
export MEMCONTINUUM_PYTHON="<python>"
exec bash "<this-repo>/hooks/post-commit-reindex.sh"
```

Since it `exec`s the canonical script by absolute path rather than copying it,
edits to `post-commit-reindex.sh` are picked up automatically without
re-installing — an existing install therefore picks up this bounded-pass/
embed-worker redesign on its very next commit, with no re-install step. A
failed reindex must never block the commit — the script
always exits 0 (see its own comments) and logs failures to
`$MEMCONTINUUM_HOME/hook.log` instead.

The script itself now runs its content pass (`reindex --root … --project …
--db … --no-embed --auto`) through the same watchdog launcher the write-side
hooks use (`hooks/mc-watchdog.sh`), under its own budget
(`MEMCONTINUUM_POST_COMMIT_BUDGET`, default 30 seconds) — a hung or slow
embedding backend can never delay the commit, because this pass never calls
the embedding backend at all. Its own log line gains one more token,
`embed=pending|clean|skipped`: `pending` means the reindex left one or more
records without a fresh vector, in which case the script also touches
`$MEMCONTINUUM_HOME/<project>.embed-pending` (this script always builds its
own database path as `$MEMCONTINUUM_HOME/<project>.sqlite`, so this is the
same "beside the database" location `docs/INTERNALS.md`'s writable-surface
section describes -- a diverging custom `--db` is only possible calling
`memidx.py embed-worker` directly, not through this hook) and spawns
`memidx.py embed-worker` detached (a Python `subprocess.Popen(
start_new_session=True)` — never bash `&`, never a `setsid` binary, which
macOS does not ship) to backfill embeddings in the background; `clean` means
nothing was left to embed; `skipped` means the content pass returned non-zero
within budget (e.g. an integrity failure). A genuine watchdog kill never
reaches this script's own final line at all — it produces only the
launcher's own `outcome=watchdog-killed hook=post-commit-reindex.sh` line in
`hook.log`, with no `embed=` token for that invocation.
`MEMCONTINUUM_EMBED_WORKER=0` disables the spawn
(the marker is still touched) — set it wherever a detached background
process must not be left running (tests, CI). The embed-worker coalesces
repeated commits: a second worker finding the first one's `<project>.embed.lock`
already held exits 0 immediately, and the marker is removed only after a
pass whose content it actually reflects (a commit landing mid-pass retouches
the marker, and the worker loops again rather than declaring victory early).
See docs/INTERNALS.md's watchdog and embedding-lifecycle sections for the
full contract, including the crash/retry behavior.

#### Post-commit re-lint

`git commit --no-verify` skips `pre-commit` but never `post-commit`, so after
the reindex this hook runs the same two checks `pre-commit` runs (the schema
pass, and the append-only comparison of `HEAD~1` against the index; a root
commit gets the schema pass only). The checks live in one sourced file,
`hooks/mc-store-lint.sh`, shared by both hooks. A finding never blocks — the
commit already exists. Instead the hook prints `MemContinuum: commit <sha> in
store <project> skipped the commit checks and would have been refused:` and
memlint's output on stderr, logs `relint=failed:schema|append-only|both`, and
writes `$MEMCONTINUUM_HOME/<project>.relint-failed` (sha, date, summary). The
next session start names the marker in one line (with the store root it came
from, the marker's first line `root=<physical path>`); the next clean commit
of that same store removes it (another store sharing the project name leaves
it alone and logs `relint=ok marker=foreign`; a finding in such a second store
leaves the first store's marker in place and logs `relint=failed:<kind>
marker=foreign`, the banner still printing). An engine crash, or a memlint run
that exits 0 without printing its summary line, is
`relint=skipped:engine-failure`, never a finding, and never clears the marker.
A renamed project leaves its marker under the old name. With no watchdog
python (`MC_GUARD_PY` unresolved) the re-lint and backup log
`relint=skipped:no-watchdog` / `backup=skipped:no-watchdog` and do nothing; a
missing `mc-store-lint.sh` is `relint=skipped:no-lint-lib` (and the
`pre-commit` hook fails open with `skipped=no-lint-lib`). The `pre-commit` hook's documented gaps (the schema pass reads the
working tree, not the index) are inherited unchanged.

#### Store backup bundle

Off unless a backup directory is configured. Settings, environment first, then
`$MEMCONTINUUM_HOME/config.sh` (which `memcontinuum-setup.sh --backup-dir DIR
--backup-keep N` writes, and keeps on later runs without the flags). They are
deliberately not exported by the installed wrapper:

- `MEMCONTINUUM_BACKUP_DIR` — an absolute directory that **already exists**.
  The hook never creates it (on WSL an unmounted `/mnt/d` would otherwise
  silently receive bundles on the Linux root); a missing directory logs
  `backup=skipped:dir-missing`.
- `MEMCONTINUUM_BACKUP_KEEP` — bundles to keep per store (default 10). A
  positive decimal; leading zeros are stripped (`08` is 8, never octal). An
  unusable value (`0`, `00`, `abc`) logs `backup=failed:bad-keep` and prunes
  nothing; the bundle is still written. `memcontinuum-setup.sh` rejects it at
  the flag.
- A relative directory, or one containing a newline or other control character,
  logs `backup=skipped:bad-dir`; a project name that is `.`, `..` or has
  anything outside `[A-Za-z0-9._-]` logs `relint=skipped:bad-project
  backup=skipped:bad-project` and writes no marker (a name with a slash or
  control character skips the whole hook, reindex included). A
  `<dir>/<project>` symlink that resolves outside `<dir>` logs
  `backup=failed:bad-project`. Only files named exactly
  `<project>-YYYYMMDD-HHMM.bundle` count as bundles. An empty
  `MEMCONTINUUM_BACKUP_DIR` in the environment switches backups off even when
  `config.sh` records a directory. A `MANIFEST.txt` that is a symlink or not a
  regular file is not appended to (`backup=ok-manifest-refused`, bundle kept).

Layout: `<dir>/<project>/<project>-YYYYMMDD-HHMM.bundle` plus one
`MANIFEST.txt` line per bundle (`<project> | source=… | HEAD=… | commits=… |
bundle=… | size=… | sha256=…`). The folder name is the project name verbatim;
on a case-insensitive drive (drvfs) `IceKEY-OpenRouter` lands in an existing
`icekey-openrouter` folder. Each bundle is written as `<name>.part.<pid>` (unique per hook run; created
exclusively, the write itself being the exclusive open, so a planted symlink is
never followed; the bundle goes through stdout so git makes no `.lock`), verified with `git bundle
verify`, then renamed with a refusal (`backup=failed:dest`) when the final name
is a symlink or a directory. A bundle is a snapshot of the refs at
commit time (not the reflog, so an `--amend` or reset drops the old commit
from later bundles); two commits in the same minute share a name and the
later one replaces the earlier (the second atomic rename wins). After a verified rename the hook keeps
the bundle it just wrote plus at most N-1 others matching
`<project>-YYYYMMDD-HHMM.bundle` (oldest names removed first; a manual
bundle with that name pattern counts, and a future-dated name cannot displace
the new bundle) and deletes the rest; a failed delete logs
`backup=ok-prune-failed`. A leftover `<name>.part.<digits>` (or an old `.part`, or a `.part.lock` from installs that ran the earlier named
`git bundle create` form) from a killed hook is removed at the start of a later run only when its
modification time is older than twice `MEMCONTINUUM_POST_COMMIT_BUDGET` (default 30, so 60 s); younger
ones may belong to a live overlapping hook (`part-swept=N` is logged only when N>0). For `git commit --amend`, post-commit compares against the amended
commit's parent, so a link introduced by the amended commit and rewritten in
the amend reads as an add (pre-commit would have compared against the
replaced commit); links that existed in the parent are still caught. Amending a
store's root commit is not supported (schema-checked only). Manifest lines are never
pruned. Any failure logs `backup=failed:<reason>`, removes its `.part.<pid>`, and
leaves the exit code and the commit alone.

Restore: `git clone <dir>/<project>/<file>.bundle <new-store-path>`.

The hook's single log line gains `relint=… backup=…` after `embed=…`.

### 3b. `pre-commit-append-only.sh` — the store's `pre-commit`

Enforces the append-only invariant docs/SCHEMA.md section 7 describes —
editing a recorded link's body, or removing or renaming it, is refused (its
three lifecycle fields — status, superseded_by, promoted_by — may each move
forward once); a changed mind is always a new link.

`scripts/repo-init.sh` writes this wrapper as `<store>/.git/hooks/pre-commit`,
same shape as `post-commit` above (same three exports, `exec`s the canonical
script by absolute path so an edit to it needs no reinstall) — with the same
foreign-hook refusal `post-commit` now has too: a `pre-commit` this
installer did not render is left untouched and reported, never silently
overwritten. Identity is the complete five-line wrapper this installer
renders — the shebang, the three `MEMCONTINUUM_*` exports, and the `exec`
line naming the canonical script — never the `exec` line alone, and never a
substring match anywhere in the file.

```bash
#!/usr/bin/env bash
export MEMCONTINUUM_ROOT="<store>"
export MEMCONTINUUM_PROJECT="<project>"
export MEMCONTINUUM_PYTHON="<python>"
exec bash "<this-repo>/hooks/pre-commit-append-only.sh"
```

The script runs `memlint.py --against-ref HEAD --staged <store>` (`--staged`
so it judges what is actually about to be committed — the index, not
whatever else sits in the working tree) and, unlike every other hook here,
BLOCKS the commit (exit 1) when that check finds an append-only violation —
the errors on stderr, one `hook.log` line
(`pre-commit-append-only: rc=1 changed=<n> project=…`). A clean check exits 0
with `rc=0 changed=<n> project=…`; an unborn HEAD, a missing python,
memlint itself failing to produce a recognizable result, or (fix wave 1 G1)
the hook's own cwd not matching `MEMCONTINUUM_ROOT` (`skipped=not-the-store`
— a wrapper invoked from another repository, e.g. through a mistakenly
shared `core.hooksPath`, must never judge that repository's commit) all
exit 0 with `skipped=<reason> project=…` instead — fail-open for
infrastructure, fail-closed only for a genuine history edit. See docs/INTERNALS.md's memlint
section (`--against-ref`) for the full rule table, and its "Fail-open is the
contract" and "The watchdog" sections for why this one hook blocks and why it
carries no timeout guard. `git commit --no-verify` bypasses this hook
entirely, same as any git hook — for a store other machines or CI also
commit to, the real guarantee is the same check run again in CI plus a
protected branch, not this hook alone.

## 4. Write-side reminder hooks — five more Claude Code hooks in the target project

Live in this repo too (`hooks/{memlib.sh,mc-path-lib.sh,mc-watchdog.sh,
ledger-post-edit.sh,precompact-persist.sh,sessionstart-remind.sh,
userprompt-remind.sh,sessionend-stamp.sh}`),
wired into a project's Claude Code settings exactly like `pre-edit-chain.sh`
above. They gather evidence (an edit ledger, git HEAD movement) and, at most
a few times per session, ask ONE question — they never write a record, never
draft one, never classify anything as a ruling. See `docs/INTERNALS.md`'s
"Hooks and the fail-open contract" table for what each one does.

Rendered from `templates/write-hooks.json.tmpl`, the project-agnostic shape
merged into `.claude/settings.json` or `.claude/settings.local.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_CODE_ROOTS=<code-roots-json> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/ledger-post-edit.sh"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_CODE_ROOTS=<code-roots-json> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/precompact-persist.sh"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_CODE_ROOTS=<code-roots-json> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/sessionstart-remind.sh"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_CODE_ROOTS=<code-roots-json> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/userprompt-remind.sh"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_CODE_ROOTS=<code-roots-json> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/sessionend-stamp.sh"
          }
        ]
      }
    ]
  }
}
```

Notes:
- **`if` works only on the `PostToolUse` entry above** — per Claude Code's own hook contract,
  `if` conditions are evaluated only for tool-matched events (`PreToolUse`/`PostToolUse`); there
  is no settings-level `if` for `PreCompact`, `SessionStart`, `UserPromptSubmit`, or `SessionEnd`.
  If you want `ledger-post-edit.sh` scoped the same way `pre-edit-chain.sh` is (e.g. only a
  specific subtree), add `"if": "Edit(...)"` / `"if": "Write(...)"` entries the same way as
  section 1 above — but `if` cannot scope a `Bash` entry the same way: there is no path-shaped
  condition for an arbitrary shell command, so any such filter only ever narrows the Edit/Write
  side of this hook, never the shell-diff branch that watches `Bash`. The other four scripts gate on payload fields (`trigger`, `agent_id`/
  `agent_type`) in-script instead, since they have no `if` to lean on. `source` is a
  `SessionStart`-only field (see the next note) — `precompact-persist.sh` gates on `trigger`
  (PreCompact's own field, `manual`/`auto`), and `userprompt-remind.sh` gates on `agent_id`/
  `agent_type` (UserPromptSubmit carries neither `source` nor `trigger` at all — a
  `source == "user"` gate on this event matches no real payload; see that script's own header
  comment).
- `SessionStart` fires with several `source` values (`startup`, `resume`, `clear`, `compact`,
  `fork`); `sessionstart-remind.sh` branches on all of them itself — wire it unconditionally
  (no settings-level source filter needed, though one is supported if you want to narrow it).
  `hooks/memcontinuum-detect.sh` (a separate, user-level `~/.claude/settings.json` `SessionStart`
  hook — not rendered by this installer, see its own header comment) also gates on `source` for
  the same reason — both are the correct, documented use of that field; `UserPromptSubmit` is the
  one event that never carries it.
- `MEMCONTINUUM_CODE_ROOT`/`MEMCONTINUUM_CODE_ROOTS` are new here (not used by
  `pre-edit-chain.sh`/`post-commit-reindex.sh`): every configured `--code-root` reaches the five
  write-side hooks, not just one. `MEMCONTINUUM_CODE_ROOT` carries the first (kept for a reader
  that only ever looks at one root); `MEMCONTINUUM_CODE_ROOTS` carries the complete JSON list of
  physical paths, read by `hooks/memlib.sh`'s `mc_code_roots` (falling back to the single variable
  when the list is absent — old-shape wiring, or a hand-written config). `ledger-post-edit.sh`
  checks the edited path against every root; `userprompt-remind.sh`/`precompact-persist.sh` pass
  every root to `unmapped --code-root` (repeatable) in one call.
- `MEMCONTINUUM_HOME` is deliberately omitted here, same reason and same resolution as section 1
  above (default, then the pointer, then a custom value only if given) — and it must never point
  at a synced/cloud drive.
- These five scripts write `$MEMCONTINUUM_HOME/sessions/**/*.json[.lock]` and
  `$MEMCONTINUUM_HOME/hook.log` — never the store, never the code tree. One carve-out:
  `userprompt-remind.sh`'s coverage check and `precompact-persist.sh` call `memidx.py unmapped`,
  which self-heals a drifted decision index with a `reindex --no-embed --auto` (mode-preserving —
  it never embeds, and never claims a fuller embedding mode than the index already had, inside a
  hook's own time budget), so the decision index's own SQLite cache is written too.
  `ledger-post-edit.sh`'s shell-diff branch additionally runs `git status --porcelain -z` (via
  `--no-optional-locks`) in every code root and the store root on a `Bash` or unrecognized-tool
  payload — that call is read-only, so `git diff`/`git status` in either root staying empty across
  every hook invocation remains a permanent regression test (`tests/test_write_hooks.py`).
  `post-commit-reindex.sh` (section 3 above, not one of these five) additionally writes
  `$MEMCONTINUUM_HOME/<project>.embed-pending`, `<project>.embed.lock` and `<project>.embed.log`
  when its content pass leaves rows without a fresh vector.

## 5. `code-refresh.sh` — code-index freshness on `Stop` and `SessionStart`

Rendered from `templates/code-refresh-hook.json.tmpl` for a project that has at least one
`--code-root` (a store-only project gets nothing). Same env block as the ledger line:
`MEMCONTINUUM_ROOT`, `MEMCONTINUUM_CODE_ROOT`, `MEMCONTINUUM_CODE_ROOTS`, `MEMCONTINUUM_PROJECT`,
`MEMCONTINUUM_PYTHON`. One script, two entries:

| Event | What it does | Budget |
|---|---|---|
| `Stop` | the existing preflight (`code_index_report`: stat signals, sha on drift, git HEAD trigger), then the same incremental text heal `code-search` makes (`heal_code_index`, text-only, never embeds) | `MEMCONTINUUM_CODE_REFRESH_BUDGET`, default 8 s |
| `SessionStart` | preflight only, never a reindex; a stale report writes the marker | 2 s |

- **Silent.** The script prints nothing on any path (a `Stop` hook's output is fed back to the
  model) and always exits 0. The one place its result goes is `hook.log`:
  `<ts> code-refresh outcome=<refreshed|skip|failed> reason=… project=P roots=N unrecorded=N
  changed=N removed=N healed=N dropped=N elapsed=Xs event=<Stop|SessionStart> session=S
  cwd=<the session working directory, percent-encoded to one token; - when the payload has none or the line is shell-composed> embed=<spawned|clean|off|failed|unknown> [marker=write-failed] [detail=…]`.
  Skip reasons: `uninitialized` (no code db yet, or no recorded root), `current`, `degraded`
  (nothing left to repair), `locked`, `stale-marked` (SessionStart found the index behind).
- **Embeddings are a background job (`embed=`).** On BOTH events, once the preflight has found an
  initialised index, the hook looks at `awaiting_embedding` (chunks without a vector at the current
  model fingerprint) and, when it is above 0, raises `$MEMCONTINUUM_HOME/<project>.code-embed-pending`
  and starts a detached `memidx.py code-embed-worker` (python `subprocess.Popen` in a new session,
  stdio to `<project>.code-embed.log`; milliseconds, no change to the hook's budget), independent of
  whether the index is current. `embed=spawned` started one, `clean` nothing awaits a vector, `off` is `MEMCONTINUUM_EMBED_WORKER=0`
  (the marker is still raised), `failed` a marker or spawn error, `unknown` a run that ended earlier. The worker is handed the hook's wired roots (`--root` each); a hook that names none spawns nothing (`embed=off reason=no-wired-roots`). On `Stop`, a recorded root the wiring no longer names is dropped (`dropped=<n>`, `detail=dropped-roots:…`); `SessionStart` only writes the marker `stale-root=<n>`.
  On `Stop` it looks again after the heal, which adds unembedded chunks. The worker fills them in
  batches of 128; `code-search` ranks over the vectors present meanwhile and says how many are missing.
  This is also how an index created before this release, with no vectors, fills itself.
- **First index stays explicit.** A root with no recorded index is counted (`unrecorded=`) and
  left alone: `memidx.py code-reindex --lang …` is still how a root is first indexed.
- **Marker.** `$MEMCONTINUUM_HOME/<project>.code-stale` holds `reason=<r> since=<iso> roots=<n>`.
  It is written `refreshing` before the heal and removed after a clean one; a failed heal, a
  `--heal-limit` refusal (`MEMCONTINUUM_CODE_REFRESH_LIMIT`, default 500) or a fail-open
  exception rewrites it (`reindex-failed rc=N`, `over-limit changed=N`, `heal-error <Type>`); a
  watchdog kill leaves `refreshing`, which is true (per-file savepoints, nothing corrupt).
  `sessionstart-remind.sh` shows it in one line next to the re-lint marker. Hook entries of one
  event run in parallel, so a marker written by this session's own `SessionStart` run is shown at
  the next session start, if the first `Stop` has not repaired it by then.
- **One writer.** A non-blocking `flock` on `$MEMCONTINUUM_HOME/<project>.code-refresh.lock`
  excludes a second refresh; `code-search`'s own heal takes the same lock and skips with a
  one-line notice when it is held.
- **No git post-commit hook in code repos, on purpose.** Installing git hooks into your project
  repos is the invasive path `repo-init.sh` refuses for foreign hooks, and a commit nobody is
  working on costs nothing stale. A commit made inside a session is caught by the next `Stop`
  (before your next prompt); one made outside any session (another tool, another machine) by the
  next `SessionStart`. The bound: the index can lag by the commits made between two sessions, and
  it says so at session start.
- **Ignored files.** The code walk lists a git work tree with `git ls-files --cached --others
  --exclude-standard`, so `.gitignore`d content (a cloned corpus under `docs/`, build output) is
  not indexed; the first refresh after upgrading drops anything an older walk let in. Outside a
  work tree, or without git, it falls back to a plain directory walk and says so once on stderr.


> Invocation note: every example above runs a hook as `bash <path>` rather than
> by the path alone, matching what the templates render -- the executable bit is
> not required anywhere (a zip download or a core.filemode=false clone drops it
> silently).

> Two projects sharing one claude-dir: `scripts/repo-init.sh`'s merge step identifies its own
> hook entries by script basename, further scoped by the `MEMCONTINUUM_PROJECT=` marker every one
> of the eight commands carries -- `newfile-nudge.sh` (section 2 above) included. A project's
> re-run can only sweep entries marked for ITS OWN `--project`, or entries with no marker at all
> (wired before this marker existed). An entry carrying no marker stays sweepable by ANY
> project's re-run until the project that owns it re-runs `scripts/repo-init.sh` -- there is no
> separate step for this; a normal re-run closes the hole.
