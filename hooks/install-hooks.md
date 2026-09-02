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
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_STRIP_PREFIX=<code-root>/ MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/pre-edit-chain.sh"
          },
          {
            "type": "command",
            "if": "Write(/<code-root>/**)",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_STRIP_PREFIX=<code-root>/ MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/pre-edit-chain.sh"
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
- `MEMCONTINUUM_HOME` is deliberately omitted here so the hook falls back to memidx.py's own default
  (`~/.memcontinuum`) — set it explicitly only if the derived index should live somewhere else. It
  must never point at a synced/cloud-backed filesystem — SQLite locking is not reliable there.
- The command line, not a JSON `env` block, carries the env vars — Claude Code hook `command`
  entries run through a shell, so `VAR=value ... command` works directly.
- `if` filter paths use Claude Code's permission-rule syntax, where a single leading slash
  anchors at the settings source, not the filesystem root (docs: `Edit(//Users/alice/file)` =
  absolute `/Users/alice/file`). `<code-root>` above is always an absolute path (already starting
  with `/`), so the rendered pattern needs a SECOND leading slash — `Edit(//home/…/**)` — to match
  anything at all. With only one leading slash the hook never fires in a real session, so
  `code-root-filter-pair.json.tmpl` and `newfile-nudge-filter-pair.json.tmpl` compose `if` as
  `Edit(/{{CODE_ROOT}}/**)` / `Write(/{{CODE_ROOT}}/**)` — the rendered value always has exactly
  two leading slashes.

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
legacy nudge entry carrying no `MEMCONTINUUM_PROJECT` at all stays sweepable by ANY project's
re-run until that project re-runs `repo-init.sh` — see the migration note at the end of this file.

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
re-installing. A failed reindex must never block the commit — the script
always exits 0 (see its own comments) and logs failures to
`$MEMCONTINUUM_HOME/hook.log` instead.

## 4. Write-side reminder hooks — five more Claude Code hooks in the target project

Live in this repo too (`hooks/{memlib.sh,mc-watchdog.sh,ledger-post-edit.sh,
precompact-persist.sh,sessionstart-remind.sh,userprompt-remind.sh,sessionend-stamp.sh}`),
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
        "matcher": "Edit|Write|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/ledger-post-edit.sh"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/precompact-persist.sh"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/sessionstart-remind.sh"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/userprompt-remind.sh"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> bash <this-repo>/hooks/sessionend-stamp.sh"
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
  section 1 above; the other four scripts gate on payload fields (`trigger`, `agent_id`/
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
- `MEMCONTINUUM_CODE_ROOT` is new here (not used by `pre-edit-chain.sh`/`post-commit-reindex.sh`):
  it is the code root `ledger-post-edit.sh` scopes edits to. The five write-side hooks only
  support **one** `MEMCONTINUUM_CODE_ROOT` each — with multiple `--code-root`s given to
  `scripts/repo-init.sh`, the first one given is what they get.
- `MEMCONTINUUM_HOME` is deliberately omitted here for the same reason as section 1: falls back
  to `~/.memcontinuum` unless overridden, and must never point at a synced/cloud drive.
- These five scripts' only writable surface is `$MEMCONTINUUM_HOME/sessions/**/*.json[.lock]` and
  `$MEMCONTINUUM_HOME/hook.log` — never the store, never the code tree. `git diff`/`git status`
  in either root staying empty across every hook invocation is a permanent regression test
  (`tests/test_write_hooks.py`).


> Invocation note: every example above runs a hook as `bash <path>` rather than
> by the path alone, matching what the templates render -- the executable bit is
> not required anywhere (a zip download or a core.filemode=false clone drops it
> silently).

> Migration note: `scripts/repo-init.sh`'s merge step identifies its own hook entries by script
> basename, further scoped by the `MEMCONTINUUM_PROJECT=` marker every one of the seven commands
> carries -- `newfile-nudge.sh` (section 2 above) included. Two projects sharing one
> `--claude-dir`: a project's re-run can only sweep entries marked for ITS OWN `--project`, or
> entries with no marker at all (legacy, pre-identity wiring). A legacy entry carrying no marker
> stays sweepable by ANY project's re-run until the project that owns it re-runs
> `scripts/repo-init.sh` -- there is no separate migration step; a normal re-run closes the hole.
