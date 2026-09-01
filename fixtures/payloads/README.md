# UserPromptSubmit payload fixtures

`userpromptsubmit_prompt_key.json` and `userpromptsubmit_user_input_key.json`
are **SYNTHETIC** payloads, hand-written from the publicly documented
UserPromptSubmit hook input shape. They exist so `tests/test_write_hooks.py`
has a payload-contract test that does not depend on a keyword list decided
inside the test file itself, per the docs/DESIGN.md's
payload-contract note ("accept both `user_input` and `prompt` keys; add a
captured real payload fixture ... so synthetic tests don't decide the
contract").

**These are NOT a captured real payload.** No live Claude Code
UserPromptSubmit payload has been captured into this repo. The one place a
real payload's actual shape is observed is the `payload_keys=...` line that
`hooks/userprompt-remind.sh` writes to `hook.log` on a session's first turn
with a resolvable session_id and an already-started session (sorted top-level
key names only, never values -- see that script's header comment). Whoever
eventually captures a real payload should diff its key set against that log
line, not assume these two fixtures are it.

**Fix-round update (2026-08-31):** these fixtures previously carried a
`"source": "user"` field, copied from an earlier misreading of the docs. It
does not exist on this event -- `source` is a `SessionStart`-only field
(`startup`/`resume`/`clear`/`compact`/`fork`); `UserPromptSubmit` and
`SessionStart` were confused when these fixtures were first written. This was
confirmed empirically as well as against the docs: a hook gate that required
`source == "user"` on `UserPromptSubmit` rejected 30/30 real invocations in
one live session (outcome=non-user-source in hook.log), because the field it
was checking was never present to begin with. The fixtures now carry the
documented common fields (`session_id`, `transcript_path`, `cwd`,
`permission_mode`, `hook_event_name`, `prompt_id`) plus one of the two
documented prompt-text key variants (`prompt` / `user_input`) -- no `source`.
A subagent-context invocation additionally carries `agent_id` and
`agent_type`, which these two fixtures deliberately omit (they represent the
main-thread, human-turn shape the hook is meant to act on).
