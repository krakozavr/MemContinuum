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
existing file is `pre-edit-chain.sh`'s job, not this one's). Deliberately minimal: no
`MEMCONTINUUM_ROOT`/`STRIP_PREFIX`, since this hook never calls `memidx.py` or reads the index at
all — it only checks that the write target is new, under a configured code root, and has an
indexed source extension, then injects one reminder line. It DOES carry `MEMCONTINUUM_PROJECT`
— identity only, so `scripts/repo-init.sh`'s merge step can tell this project's nudge entry apart
from a different project's sharing the same `--claude-dir`; the hook itself never reads it. A
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

## 3. `post-commit-reindex.sh` — git hook in the STORE repo

Lives in this repo too, but runs as a `post-commit` hook inside the **store**
repo — not this tool repo, and not the code repo it describes.

`scripts/repo-init.sh` writes this wrapper as `<store>/.git/hooks/post-commit`
(not a bare symlink — a symlinked git hook carries no environment of its
own, and `post-commit-reindex.sh` silently no-ops without `MEMCONTINUUM_ROOT`
set):

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


> Invocation note: every example above runs a hook as `bash <path>` rather than
> by the path alone, matching what the templates render -- the executable bit is
> not required anywhere (a zip download or a core.filemode=false clone drops it
> silently).

> Two projects sharing one claude-dir: `scripts/repo-init.sh`'s merge step identifies its own
> hook entries by script basename, further scoped by the `MEMCONTINUUM_PROJECT=` marker every one
> of the seven commands carries -- `newfile-nudge.sh` (section 2 above) included. A project's
> re-run can only sweep entries marked for ITS OWN `--project`, or entries with no marker at all
> (wired before this marker existed). An entry carrying no marker stays sweepable by ANY
> project's re-run until the project that owns it re-runs `scripts/repo-init.sh` -- there is no
> separate step for this; a normal re-run closes the hole.
