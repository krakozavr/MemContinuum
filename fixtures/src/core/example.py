"""Fixture: the file fixtures/memlint_clean/topics/processing/clean-topic.md's
`src/core/example.py#thing` code_ref names, so `--code-root fixtures` (used by
tests.test_memlint.TestMemlintRejectsUnknownFlags.test_known_root_with_code_root_still_works)
resolves it to a real symbol instead of a dangling ref (task A2-2's marker
verification checks a topic's path#symbol code_refs for existence -- see
memlint.lint_markers). No decision: marker is deliberately placed above
`thing` -- the topic's link stays clean by rc, but store->code marker
verification still WARNS "no marker at src/core/example.py#thing", exactly
as an adopted store with no markers yet is expected to (TOP-0122 L2).
"""


def thing():
    return None
