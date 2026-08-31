# UserPromptSubmit payload fixtures

`userpromptsubmit_prompt_key.json` and `userpromptsubmit_user_input_key.json`
are **SYNTHETIC** payloads, hand-written from the publicly documented
UserPromptSubmit hook input shape. They exist so `tests/test_write_hooks.py`
has a payload-contract test that does not depend on a keyword list decided
inside the test file itself, per the docs/DESIGN.md's
payload-contract note ("accept both `user_input` and `prompt` keys; add a
captured real payload fixture ... so synthetic tests don't decide the
contract").

**These are NOT a captured real payload.** No live Claude Code 2.1.251
UserPromptSubmit payload has been captured into this repo. The one place a
real payload's actual shape is observed is the `payload_keys=...` line that
`hooks/userprompt-remind.sh` writes to `hook.log` on a session's first
eligible turn (sorted top-level key names only, never values -- see that
script's header comment). Whoever eventually captures a real payload should
diff its key set against that log line, not assume these two fixtures are
it.
