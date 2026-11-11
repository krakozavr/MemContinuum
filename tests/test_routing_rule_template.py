"""Tests for templates/memcontinuum-rules.md -- the always-loaded project
routing rule (TOP-0117 / INC-0105) that a later repo-init change will render
into <repo>/.claude/rules/memcontinuum.md. This file only checks the
template artifact itself; repo-init.sh's render step is out of scope here
(it is mid-change on another branch).
"""
import re
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = TOOLS_DIR / "templates" / "memcontinuum-rules.md"

# This file's first line IS the identity marker -- it is where that string is
# defined, and everything else (the installer, the re-render walk, the other
# tests) reads it from here. So this test checks its SHAPE rather than
# comparing it to a second copy of itself, which would only prove the two
# copies match. The shape is what has to hold: an HTML comment (invisible when
# the rules file is read as markdown), version-tagged so a future format can
# be told apart, naming the tool and saying not to hand-edit.
IDENTITY_MARKER_SHAPE = re.compile(
    r"^<!--\s*memcontinuum-rules v\d+\b.*\bdo not hand-edit\s*-->$"
)


class TestRoutingRuleTemplate(unittest.TestCase):
    def test_exists(self):
        self.assertTrue(TEMPLATE.is_file(), f"missing {TEMPLATE}")

    def test_starts_with_identity_marker(self):
        first = TEMPLATE.read_text().splitlines()[0]
        self.assertRegex(first, IDENTITY_MARKER_SHAPE)
        self.assertIn("MemContinuum", first)

    def test_store_placeholder_present_exactly_once(self):
        text = TEMPLATE.read_text()
        self.assertEqual(text.count("{{STORE}}"), 1)

    def test_project_placeholder_present_exactly_once(self):
        text = TEMPLATE.read_text()
        self.assertEqual(text.count("{{PROJECT}}"), 1)

    def test_short(self):
        # Cap moved 25 -> 26: task A2-2 (TOP-0122 L1 rule 2b) adds one
        # sentence documenting the decision-marker convention (constraint/
        # hold links mirrored by a comment at the bound symbol), folded
        # into the existing "how to write a store record" paragraph rather
        # than its own blank-line-delimited block -- a genuine, minimal,
        # net +1-line content addition, not budget creep.
        # Cap moved 26 -> 27: task A2-3 (TOP-0122 L1 rule 2a) adds one more
        # sentence to the SAME paragraph -- name the decision in commit
        # messages too -- again a net +1-line addition, not a new block.
        # Cap moved 27 -> 32: TOP-0135 L2 adds the context-pack paragraph (briefs
        # open with the records) as its own block after the command served a
        # real gate (PR #44).
        # Cap moved 32 -> 36: TOP-0137 L15 adds the C3 sentence (find the
        # existing implementation and name it; ask "did this duplicate
        # anything?") as its own block after the context-pack paragraph.
        # Cap moved 36 -> 40: TOP-0137 L29 adds the "a decision must still be
        # true tomorrow" paragraph (session state is not a ruling) as its own
        # block after the store-section bullets.
        # Cap moved 40 -> 41: TOP-0137 L35 adds one sentence to the context-pack
        # paragraph (the pack lists the repository's reuse-inventory rows for the
        # files the brief touches) -- a net +1-line addition inside that paragraph.
        # Cap moved 41 -> 42: TOP-0137 L35 round 1 extends the gate sentence of the
        # "find the existing implementation" paragraph with the inventory questions
        # (which jobs were touched, what was reused, what the guard misses) -- a net
        # +1-line addition inside that paragraph.
        text = TEMPLATE.read_text()
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 42, f"template is {len(lines)} lines, want <= 42")

    def test_names_the_still_true_tomorrow_rule(self):
        text = TEMPLATE.read_text()
        self.assertIn("A decision must still be true tomorrow.", text)
        self.assertIn("session state, not a ruling", text)

    def test_names_the_reuse_inventory_sentence(self):
        text = TEMPLATE.read_text()
        self.assertIn("docs/REUSE-INVENTORY.json", text)
        self.assertIn("--code-root <repo>", text)
        self.assertIn("reuses the named owner", text)

    def test_names_the_c3_rule(self):
        text = TEMPLATE.read_text()
        self.assertIn("find the existing implementation and name it", text)
        self.assertIn("did this duplicate anything?", text)
        flat = " ".join(text.split())
        self.assertIn('and, when the repository carries an inventory, "which inventory jobs were touched, '
                      'what existing implementation was reused, what does the guard miss?"', flat)

    def test_names_the_record_shapes_and_auto_memory_distinction(self):
        text = TEMPLATE.read_text()
        self.assertIn("topics/<area>/<topic>.md", text)
        self.assertIn("incidents/", text)
        self.assertIn("auto-memory", text)
        self.assertIn("Never both", text)


if __name__ == "__main__":
    unittest.main()
