"""Documentation doctrine checks (TOP-0116).

The README describes the product for its user; engineering lives in
docs/INTERNALS.md; development history lives in git and in the store, never in
a public-facing doc. These are the two mechanical halves of that rule:

1. No user-facing text carries build archaeology -- fix-round/regate/
   reviewer-finding numbers, task numbers, or store record ids. Prose that
   explains a constraint is welcome; the numbering of the review pass that
   produced it is not. "User-facing" is wider than the README: a skill file is
   read aloud into conversations, and `--help` output is read by whoever types
   the command, so both are checked here. Code comments in .sh/.py files are
   deliberately NOT checked -- history is allowed to live there.
2. docs/INTERNALS.md exists and the README links it exactly where a maintainer
   would look for it.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
README = TOOLS_DIR / "README.md"
INTERNALS = TOOLS_DIR / "docs" / "INTERNALS.md"
SKILL = TOOLS_DIR / "skills" / "memcontinuum" / "SKILL.md"
INSTALL_HOOKS = TOOLS_DIR / "hooks" / "install-hooks.md"

# Scripts whose *rendered* --help text is checked. Two of these build their
# usage text out of their own header comment block, so a provenance note added
# to the header lands in front of a user without anyone noticing.
HELP_COMMANDS = [
    ("memcontinuum-setup.sh --help", ["bash", str(TOOLS_DIR / "memcontinuum-setup.sh"), "--help"]),
    ("repo-init.sh --help", ["bash", str(TOOLS_DIR / "scripts" / "repo-init.sh"), "--help"]),
]

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

PUBLIC_DOCS = [README, INTERNALS, SKILL, INSTALL_HOOKS]


def _scan(label, text, offenders):
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in FORBIDDEN:
            if pattern.search(line):
                offenders.setdefault(label, []).append(
                    f"{lineno}: {name}: {line.strip()[:90]}"
                )


class TestPublicDocsCarryNoProvenance(unittest.TestCase):
    def test_no_forbidden_tokens(self):
        offenders = {}
        for doc in PUBLIC_DOCS:
            self.assertTrue(doc.is_file(), f"missing {doc}")
            _scan(doc.name, doc.read_text(), offenders)
        self.assertEqual(
            offenders, {},
            "development-history provenance found in a public-facing doc "
            f"(TOP-0116): {offenders}",
        )

    def test_no_forbidden_tokens_in_rendered_help(self):
        env = dict(os.environ)
        # Same convention as the other script-driving tests: never let an
        # inherited PYTHONPATH shadow the venv these scripts resolve.
        env["PYTHONPATH"] = ""
        offenders = {}
        for label, argv in HELP_COMMANDS:
            proc = subprocess.run(
                argv, cwd=str(TOOLS_DIR), env=env, capture_output=True, text=True,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"{label} exited {proc.returncode}: {proc.stderr[:400]}",
            )
            self.assertTrue(proc.stdout.strip(), f"{label} printed nothing")
            _scan(label, proc.stdout + proc.stderr, offenders)
        self.assertEqual(
            offenders, {},
            f"development-history provenance found in --help output: {offenders}",
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
