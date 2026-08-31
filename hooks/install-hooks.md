# Wiring the hooks

`install.sh` renders and merges all of this automatically (see the README's
"Installing into a new project") — this document explains what it wires and
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
            "if": "Edit(<code-root>/**)",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_STRIP_PREFIX=<code-root>/ MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/pre-edit-chain.sh"
          },
          {
            "type": "command",
            "if": "Write(<code-root>/**)",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_STRIP_PREFIX=<code-root>/ MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/pre-edit-chain.sh"
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
  must never point at a synced/cloud-backed filesystem (README: "never index on a synced/cloud
  drive").
- The command line, not a JSON `env` block, carries the env vars — Claude Code hook `command`
  entries run through a shell, so `VAR=value ... command` works directly.

## 2. `newfile-nudge.sh` — Claude Code `PreToolUse` hook (Write only)

Lives in this repo (`hooks/newfile-nudge.sh`), wired into the same project settings as
`pre-edit-chain.sh` above but as a SEPARATE `PreToolUse` matcher group (`"Write"`, never
`"Edit|Write"` — this hook only ever fires on a path that does not exist yet; an edit to an
existing file is `pre-edit-chain.sh`'s job, not this one's). Deliberately minimal: no
`MEMCONTINUUM_ROOT`/`PROJECT`/`STRIP_PREFIX`, since this hook never calls `memidx.py` or reads the
index at all — it only checks that the write target is new, under a configured code root, and has
an indexed source extension, then injects one reminder line.

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
            "if": "Write(<code-root>/**)",
            "command": "MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/newfile-nudge.sh"
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

`install.sh` writes this wrapper as `<store>/.git/hooks/post-commit`
(not a bare symlink — a symlinked git hook carries no environment of its
own, and `post-commit-reindex.sh` silently no-ops without `MEMCONTINUUM_ROOT`
set):

```bash
#!/usr/bin/env bash
export MEMCONTINUUM_ROOT="<store>"
export MEMCONTINUUM_PROJECT="<project>"
export MEMCONTINUUM_PYTHON="<python>"
exec "<this-repo>/hooks/post-commit-reindex.sh"
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
draft one, never classify anything as a ruling. See the README's "Hooks"
table for what each one does.

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
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/ledger-post-edit.sh"
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/precompact-persist.sh"
          }
        ]
      }
    ],
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/sessionstart-remind.sh"
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/userprompt-remind.sh"
          }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MEMCONTINUUM_ROOT=<store> MEMCONTINUUM_CODE_ROOT=<code-root> MEMCONTINUUM_PROJECT=<project> MEMCONTINUUM_PYTHON=<python> <this-repo>/hooks/sessionend-stamp.sh"
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
  section 1 above; the other four scripts gate on payload fields (`source`, `trigger`, `agent_id`)
  in-script instead, since they have no `if` to lean on.
- `SessionStart` fires with several `source` values (`startup`, `resume`, `clear`, `compact`,
  `fork`); `sessionstart-remind.sh` branches on all of them itself — wire it unconditionally
  (no settings-level source filter needed, though one is supported if you want to narrow it).
- `MEMCONTINUUM_CODE_ROOT` is new here (not used by `pre-edit-chain.sh`/`post-commit-reindex.sh`):
  it is the code root `ledger-post-edit.sh` scopes edits to. The five write-side hooks only
  support **one** `MEMCONTINUUM_CODE_ROOT` each — with multiple `--code-root`s given to
  `install.sh`, the first one given is what they get.
- `MEMCONTINUUM_HOME` is deliberately omitted here for the same reason as section 1: falls back
  to `~/.memcontinuum` unless overridden, and must never point at a synced/cloud drive.
- These five scripts' only writable surface is `$MEMCONTINUUM_HOME/sessions/**/*.json[.lock]` and
  `$MEMCONTINUUM_HOME/hook.log` — never the store, never the code tree. `git diff`/`git status`
  in either root staying empty across every hook invocation is a permanent regression test
  (`tests/test_write_hooks.py`).
