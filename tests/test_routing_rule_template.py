"""Tests for templates/memcontinuum-rules.md -- the always-loaded project
routing rule (TOP-0117 / INC-0105) that a later repo-init change will render
into <repo>/.claude/rules/memcontinuum.md. This file only checks the
template artifact itself; repo-init.sh's render step is out of scope here
(it is mid-change on another branch).
"""
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
TEMPLATE = TOOLS_DIR / "templates" / "memcontinuum-rules.md"

IDENTITY_MARKER = (
    "<!-- memcontinuum-rules v1 — rendered by MemContinuum repo-init; "
    "do not hand-edit -->"
)


class TestRoutingRuleTemplate(unittest.TestCase):
    def test_exists(self):
        self.assertTrue(TEMPLATE.is_file(), f"missing {TEMPLATE}")

    def test_starts_with_identity_marker(self):
        text = TEMPLATE.read_text()
        self.assertTrue(
            text.startswith(IDENTITY_MARKER),
            f"first line must be the identity marker, got: {text.splitlines()[:1]!r}",
        )

    def test_store_placeholder_present_exactly_once(self):
        text = TEMPLATE.read_text()
        self.assertEqual(text.count("{{STORE}}"), 1)

    def test_short(self):
        text = TEMPLATE.read_text()
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 25, f"template is {len(lines)} lines, want <= 25")

    def test_names_the_record_shapes_and_auto_memory_distinction(self):
        text = TEMPLATE.read_text()
        self.assertIn("topics/<area>/<topic>.md", text)
        self.assertIn("incidents/", text)
        self.assertIn("auto-memory", text)
        self.assertIn("Never both", text)


if __name__ == "__main__":
    unittest.main()
