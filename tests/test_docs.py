"""Documentation doctrine checks (TOP-0116).

The README describes the product for its user; engineering lives in
docs/INTERNALS.md; development history lives in git and in the store, never in
a public-facing doc. These are the two mechanical halves of that rule:

1. No public doc carries build archaeology -- fix-round/regate/reviewer-finding
   numbers, task numbers, or store record ids. Prose that explains a constraint
   is welcome; the numbering of the review pass that produced it is not.
2. docs/INTERNALS.md exists and the README links it exactly where a maintainer
   would look for it.
"""
import re
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
README = TOOLS_DIR / "README.md"
INTERNALS = TOOLS_DIR / "docs" / "INTERNALS.md"

# Each entry: (human name, compiled pattern). Patterns are deliberately narrow
# so ordinary prose ("a round trip", "R2 of the pipeline") is not caught by
# accident -- what they match is numbered process archaeology.
FORBIDDEN = [
    ("fix-round reference", re.compile(r"fix-round", re.I)),
    ("regate reference", re.compile(r"\bregate", re.I)),
    ("numbered review round", re.compile(r"\bround\s+\d", re.I)),
    ("numbered reviewer finding", re.compile(r"\bfinding\s+\d", re.I)),
    ("finding code (R2/R3)", re.compile(r"\bR\d+/[A-Z]?\d")),
    ("numbered build task", re.compile(r"\bTask\s+\d")),
    ("store incident id", re.compile(r"\bINC-\d{4}")),
]

PUBLIC_DOCS = [README, INTERNALS]


class TestPublicDocsCarryNoProvenance(unittest.TestCase):
    def test_no_forbidden_tokens(self):
        offenders = {}
        for doc in PUBLIC_DOCS:
            text = doc.read_text()
            for lineno, line in enumerate(text.splitlines(), start=1):
                for name, pattern in FORBIDDEN:
                    if pattern.search(line):
                        offenders.setdefault(doc.name, []).append(
                            f"{lineno}: {name}: {line.strip()[:90]}"
                        )
        self.assertEqual(
            offenders, {},
            "development-history provenance found in a public-facing doc "
            f"(TOP-0116): {offenders}",
        )


class TestInternalsIsLinked(unittest.TestCase):
    def test_internals_exists(self):
        self.assertTrue(INTERNALS.is_file(), f"missing {INTERNALS}")

    def test_readme_links_internals(self):
        text = README.read_text()
        self.assertIn(
            "docs/INTERNALS.md", text,
            "README must point maintainers at docs/INTERNALS.md",
        )


if __name__ == "__main__":
    unittest.main()
