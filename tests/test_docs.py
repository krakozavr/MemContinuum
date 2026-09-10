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
import contextlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS_DIR))

import chunkers  # noqa: E402
import memidx  # noqa: E402

README = TOOLS_DIR / "README.md"
INTERNALS = TOOLS_DIR / "docs" / "INTERNALS.md"
DESIGN = TOOLS_DIR / "docs" / "DESIGN.md"
SKILL = TOOLS_DIR / "skills" / "memcontinuum" / "SKILL.md"
SEARCH_SKILL = TOOLS_DIR / "skills" / "memory-search" / "SKILL.md"
INSTALL_HOOKS = TOOLS_DIR / "hooks" / "install-hooks.md"
NEWFILE_NUDGE_HOOK = TOOLS_DIR / "hooks" / "newfile-nudge.sh"
SETUP_SH = TOOLS_DIR / "memcontinuum-setup.sh"
DECIDE_SH = TOOLS_DIR / "scripts" / "memcontinuum-decide.sh"
UPDATE_SH = TOOLS_DIR / "scripts" / "memcontinuum-update.sh"
STORE_README_TMPL = TOOLS_DIR / "templates" / "store-README.md.tmpl"
RULES_TEMPLATE = TOOLS_DIR / "templates" / "memcontinuum-rules.md"
SCHEMA = TOOLS_DIR / "docs" / "SCHEMA.md"
PYPROJECT = TOOLS_DIR / "pyproject.toml"
CHANGELOG = TOOLS_DIR / "CHANGELOG.md"

# This repo dogfoods its own installer: .claude/skills/<name>/SKILL.md is the
# INSTALLED copy that Claude Code actually loads in this checkout. It is a
# gitignored RENDER TARGET, not tracked source -- repo-init.sh installs a
# skill by copying the template over it and stamping the copy, so tracking it
# would mean every `--apply` dirties the working tree with the stamp line
# alone. The two must still stay byte-identical (stamp aside): a template fix
# that never reaches the installed copy is a fix nobody in this repo gets.
INSTALLED_SKILLS = sorted(
    (TOOLS_DIR / ".claude" / "skills").glob("*/SKILL.md")
)

PYTHON = os.environ.get("MEMCONTINUUM_PYTHON") or sys.executable

# Every command the README tells a person they can run with --help. Each must
# print usage and exit 0, and that usage is checked for provenance too: two of
# these scripts build their help text out of their own header comment block, so
# a note added to the header lands in front of a user with no doc edit at all.
HELP_COMMANDS = [
    ("memcontinuum-setup.sh --help", ["bash", str(TOOLS_DIR / "memcontinuum-setup.sh"), "--help"]),
    ("repo-init.sh --help", ["bash", str(TOOLS_DIR / "scripts" / "repo-init.sh"), "--help"]),
    ("memcontinuum-state.sh --help",
     ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-state.sh"), "--help"]),
    ("memcontinuum-decide.sh --help",
     ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-decide.sh"), "--help"]),
    ("memcontinuum-update.sh --help",
     ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-update.sh"), "--help"]),
    ("memlint.py --help", [PYTHON, str(TOOLS_DIR / "memlint.py"), "--help"]),
    ("memidx.py --help", [PYTHON, str(TOOLS_DIR / "memidx.py"), "--help"]),
] + [
    (f"memidx.py {sub} --help", [PYTHON, str(TOOLS_DIR / "memidx.py"), sub, "--help"])
    for sub in [
        "reindex", "embed-worker", "search", "chain", "for-path", "check", "why", "drift",
        "unmapped", "code-reindex", "code-search", "code-census", "stats",
        "backend-preflight",
    ]
]

# Each entry: (human name, compiled pattern). Patterns are deliberately narrow
# so ordinary prose ("a round trip", "R2 of the pipeline") is not caught by
# accident -- what they match is numbered process archaeology, dated rulings,
# roadmap vocabulary, and build-brief terms that mean nothing to a user.
FORBIDDEN = [
    ("fix-round reference", re.compile(r"fix-round", re.I)),
    ("regate reference", re.compile(r"\bregate", re.I)),
    ("numbered review round", re.compile(r"\bround\s+\d", re.I)),
    ("numbered reviewer finding", re.compile(r"\bfinding\s+\d", re.I)),
    ("finding code (R2/R3)", re.compile(r"\bR\d+/[A-Z]?\d")),
    ("numbered build task", re.compile(r"\bTask\s+\d")),
    ("store incident id", re.compile(r"\bINC-\d{4}")),
    ("numbered ruling", re.compile(r"\bRuling\s+\d", re.I)),
    ("dated owner ruling", re.compile(r"owner ruling", re.I)),
    ("roadmap vocabulary", re.compile(r"\bmilestone", re.I)),
    ("build-brief store name", re.compile(r"\bStore [A-Z]\b")),
    ("phantom schema section", re.compile(r"§G\d")),
    ("pre-fix archaeology", re.compile(r"pre-fix", re.I)),
    ("legacy wording", re.compile(r"\blegacy", re.I)),
    ("grandfathered wording", re.compile(r"grandfathered", re.I)),
    ("backward-compatible wording", re.compile(r"backward-compatible", re.I)),
    ("Migration note heading", re.compile(r"Migration note", re.I)),
    ("store record id", re.compile(r"\bTOP-\d{4}")),
    # Whole-branch review item 7: the old pattern (\bF\d+:) required a
    # literal colon immediately after the digits -- "**F8 — text**" (an em
    # dash, not a colon) evaded it entirely, and it only ever covered the
    # F-series to begin with. Widened to every bare plan/finding-code
    # letter this project's own development process uses (F=external-
    # review finding, D=decision-index-engine test-class label, H=HOLD-
    # rule class, M=Anatomy milestone, W=docs-round finding, C=Codex-
    # pending item, G=docs-round finding) followed by digits, an OPTIONAL
    # single lowercase letter (Anatomy's own milestone-sub-label shape,
    # "M2a"), then a colon, em dash, or plain hyphen -- the shape a
    # labeled-paragraph heading or inline reference actually takes
    # ("F8 — ...", "M2a: ...", "W9 - ..."), not ordinary prose (a bare "F8"
    # with no separator, or a trailing digit like "R2" from the existing
    # finding-code pattern above, is left alone). Re-gate item 3: this
    # comment's own "M2a: ..." claim is now backed by a real test case
    # (test_forbidden_catches_a_store_record_id_and_a_finding_code_label),
    # not just asserted in prose.
    ("internal plan/finding-code label", re.compile(r"\b[FDHMWCG]\d+[a-z]?\s*[—:\-]")),
]

PUBLIC_DOCS = [
    README, INTERNALS, DESIGN, SKILL, SEARCH_SKILL, INSTALL_HOOKS,
    STORE_README_TMPL, RULES_TEMPLATE, SCHEMA, PYPROJECT, CHANGELOG,
] + INSTALLED_SKILLS


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
            # Path-relative label: several of these are named SKILL.md.
            _scan(str(doc.relative_to(TOOLS_DIR)), doc.read_text(), offenders)
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


class TestInstalledSkillsMatchTheirTemplates(unittest.TestCase):
    """The defect this catches: a skill is edited under skills/ and the copy
    this checkout actually loads, .claude/skills/<name>/SKILL.md, keeps the
    old text -- invisible to a scan that only looks at the template.
    repo-init.sh installs a skill by copying the template over the
    destination, so identity is the real invariant, not similarity.

    .claude/skills/ is a gitignored RENDER TARGET (the engine repo is itself
    a wired MemContinuum project), not tracked source -- a fresh clone, or
    any checkout where repo-init.sh has never been --apply'd, carries no
    installed copy at all. These tests skip rather than fail when
    INSTALLED_SKILLS is empty; the doctrine scan in
    TestPublicDocsCarryNoProvenance still covers the installed copy whenever
    one is present."""

    def test_each_installed_skill_is_byte_identical_to_its_template(self):
        # An empty glob is the ordinary state of a checkout that never ran
        # repo-init.sh --apply (a fresh clone included) -- that's a skip,
        # not a failure. If the install location moves, point
        # INSTALLED_SKILLS at its new home rather than letting this go
        # vacuous silently.
        if not INSTALLED_SKILLS:
            self.skipTest(
                "no .claude/skills/*/SKILL.md in this checkout -- it's a "
                "gitignored render target, not tracked source; run "
                "scripts/repo-init.sh --apply to produce one, or point "
                "INSTALLED_SKILLS at its new home if the install path moved"
            )
        # D1 (updater workstream): repo-init.sh stamps the copy it installs
        # with one extra line -- "<!-- memcontinuum-rendered: SHA -->",
        # right after the frontmatter's closing "---" -- that the template
        # never carries and that changes on every commit. Stripped before
        # comparing, "identical" still means "identical" (the invariant
        # this test guards: a template fix reaching the installed copy),
        # just tolerant of the one line whose whole job is to differ.
        stamp_re = re.compile(rb"^<!-- memcontinuum-rendered: [^\n]* -->\n", re.M)
        drifted = []
        for installed in INSTALLED_SKILLS:
            template = TOOLS_DIR / "skills" / installed.parent.name / "SKILL.md"
            self.assertTrue(
                template.is_file(),
                f"{installed.relative_to(TOOLS_DIR)} has no template at "
                f"{template.relative_to(TOOLS_DIR)}",
            )
            installed_bytes = stamp_re.sub(b"", installed.read_bytes())
            if installed_bytes != template.read_bytes():
                drifted.append(str(installed.relative_to(TOOLS_DIR)))
        self.assertEqual(
            drifted, [],
            "installed skill copies have drifted from their templates -- copy the "
            f"template over each (that is what repo-init.sh does): {drifted}",
        )


class TestInstalledSkillCopyIsUntracked(unittest.TestCase):
    """.claude/skills/ is a RENDER TARGET: repo-init.sh (via
    memcontinuum-update.sh --apply) copies the template there and inserts a
    render-stamp comment line -- right after the frontmatter's closing
    `---`, never at byte 0 -- on every run. Tracking it in git means every
    `--apply` dirties the working tree with nothing but that stamp -- the
    defect this guards against."""

    def test_claude_skills_is_gitignored(self):
        proc = subprocess.run(
            ["git", "check-ignore", "-q", ".claude/skills/"],
            cwd=str(TOOLS_DIR),
        )
        self.assertEqual(
            proc.returncode, 0,
            ".claude/skills/ is not gitignored -- add it to .gitignore so "
            "the installed skill copy stops being a candidate for tracking",
        )

    def test_no_path_under_claude_skills_is_tracked(self):
        proc = subprocess.run(
            ["git", "ls-files", "--", ".claude/skills/"],
            cwd=str(TOOLS_DIR), capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout, "",
            "tracked paths under .claude/skills/ -- it's a render target, "
            f"untrack with `git rm --cached`: {proc.stdout.splitlines()}",
        )


class TestReadmeCrossReferencesResolve(unittest.TestCase):
    """Docs that point a reader at a README section by name must name a
    section that exists. The README was restructured; every cross-reference
    written against the old headings became a dead end, silently -- prose
    cannot be checked by a link checker, so it is checked here.

    A reference is any quoted name attached to a README mention:
    `README.md "Uninstall"`, `the README's "Install -> Once per repository"`.
    An arrow separates a heading from its subheading; both halves must exist.
    """

    REF_RE = re.compile(r"README(?:\.md)?(?:'s)?\s+\"([^\"]+)\"")
    # Files that point readers at the README. Kept explicit rather than
    # globbed: a new doc joining this list should be a deliberate act.
    REFERRING_DOCS = (
        [INSTALL_HOOKS, DESIGN, INTERNALS, TOOLS_DIR / "docs" / "SCHEMA.md",
         SKILL, SEARCH_SKILL]
        + INSTALLED_SKILLS
        + sorted((TOOLS_DIR / "templates").glob("*.md"))
    )

    def _readme_headings(self):
        return {
            line.lstrip("#").strip().lower()
            for line in README.read_text().splitlines()
            if line.startswith("#")
        }

    def _refs(self):
        """(doc, lineno, reference) for every quoted README section reference.

        Scanned over the whole file, not line by line: these references wrap
        across lines in prose, and a per-line scan silently sees none of them.
        """
        for doc in self.REFERRING_DOCS:
            if not doc.is_file():
                continue
            text = doc.read_text()
            for m in self.REF_RE.finditer(text):
                yield doc, text.count("\n", 0, m.start()) + 1, m.group(1)

    def test_the_scan_actually_finds_the_known_references(self):
        # Guards the guard: a regex that matches nothing passes vacuously.
        found = {ref for _, _, ref in self._refs()}
        self.assertGreaterEqual(
            len(found), 2,
            f"expected several README section references across the docs, found {found}",
        )

    def test_every_quoted_readme_section_exists(self):
        headings = self._readme_headings()
        self.assertIn("uninstall", headings, "README heading scan found nothing usable")
        broken = []
        for doc, lineno, ref in self._refs():
            rel = str(doc.relative_to(TOOLS_DIR))
            for part in re.split(r"[→>]+", ref.replace("->", "→")):
                part = part.strip().lower()
                if part and part not in headings:
                    broken.append(f"{rel}:{lineno}: README has no section {part!r}")
        self.assertEqual(
            broken, [],
            "stale README cross-references (the README's real headings are "
            f"{sorted(headings)}): {broken}",
        )


class TestDocumentedHelpFlagsWork(unittest.TestCase):
    """The README tells a person to run any command with --help. That has to
    be true of every command it names, not just the ones argparse happens to
    cover: a script that treats --help as a positional argument silently does
    the wrong thing instead of explaining itself."""

    def _help(self, argv):
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        return subprocess.run(
            argv, cwd=str(TOOLS_DIR), env=env, capture_output=True, text=True,
        )

    def _assert_prints_usage(self, label, argv):
        proc = self._help(argv)
        self.assertEqual(
            proc.returncode, 0,
            f"{label} must exit 0, got {proc.returncode}: {proc.stderr[:400]}",
        )
        out = proc.stdout
        self.assertTrue(out.strip(), f"{label} printed nothing on stdout")
        self.assertIn(
            "usage", out.lower(),
            f"{label} printed no usage text: {out[:200]!r}",
        )

    def test_memlint_help(self):
        self._assert_prints_usage(
            "memlint.py --help", [PYTHON, str(TOOLS_DIR / "memlint.py"), "--help"])

    def test_state_help(self):
        self._assert_prints_usage(
            "memcontinuum-state.sh --help",
            ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-state.sh"), "--help"])

    def test_decide_help(self):
        self._assert_prints_usage(
            "memcontinuum-decide.sh --help",
            ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-decide.sh"), "--help"])

    def test_state_help_does_not_treat_the_flag_as_a_repo_path(self):
        # The failure this guards: --help read as a REPO argument, reported as
        # `path=--help state=not-a-repo`, exit 0 -- help that looks like output.
        proc = self._help(
            ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-state.sh"), "--help"])
        self.assertNotIn("path=--help", proc.stdout)


# Commands with a real option parser: an argument loop with an
# `*) unknown argument` arm, cataloguing several named flags a --help text
# and the parser must agree on. memlint.py and memcontinuum-state.sh are
# deliberately absent from THIS list -- each takes only a bare positional
# (ROOT, REPO_PATH) plus, for memlint.py, one named `--code-root`, so neither
# has the >=5-flag matrix this list's tests probe, and memlint.py's parser
# lives in Python, not one of these scripts' `--foo)` case arms. Both DO now
# refuse an unrecognised `--flag` (H6) -- covered separately below,
# TestUnparsedCommandsStillRefuseUnknownFlags, with the same negative-control
# shape as test_the_probe_is_actually_refused.
PARSED_COMMANDS = [
    ("memcontinuum-setup.sh", ["bash", str(TOOLS_DIR / "memcontinuum-setup.sh")], []),
    ("repo-init.sh", ["bash", str(TOOLS_DIR / "scripts" / "repo-init.sh")], []),
    # decide.sh dispatches on an ACTION in $1 before its option loop, so the
    # probe needs one. `wired` never runs here: the loop refuses the probe
    # flag first, and the action dispatch is below the loop.
    ("memcontinuum-decide.sh",
     ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-decide.sh")], ["wired"]),
    ("memcontinuum-update.sh",
     ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-update.sh")], []),
]

PROBE_FLAG = "--mc-help-consistency-probe"
PROBE_VALUE = "MC_HELP_CONSISTENCY_PROBE_VALUE"

_LONG_FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
# An OPTION ENTRY, not prose that happens to begin with a flag: the flag is
# followed by end-of-line, the description column (two or more spaces), an
# `=`, or a single space and a token that is not a plain lowercase word
# (a placeholder like DIR/LIST/.ext, or a bracketed alternative).
_OPTION_ENTRY = re.compile(r"^(--[a-z][a-z0-9-]*)(?:$|\s{2,}|=| (?![a-z]+(?:\s|$)))")
# `--foo)` / `--foo|--bar)` case arms in a bash argument loop.
_CASE_LABEL = re.compile(r"^\s*((?:--[a-z0-9-]+\|)*--[a-z0-9-]+)\)", re.M)


def documented_flags(own_basename, help_text):
    """Every long option a help text presents as one of THIS command's own.

    Only three line shapes count: the usage synopsis (a line naming the
    command itself), a bracketed synopsis continuation, and an option entry.
    Free prose is skipped on purpose -- these help texts discuss sibling
    commands' flags ("every installer run it makes is passed --adopt-only")
    and wrap mid-sentence onto lines that begin with one.
    """
    flags = []
    for line in help_text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("usage:"):
            stripped = stripped.split(":", 1)[1].strip()
        if not (stripped.startswith(own_basename)
                or stripped.startswith("[--")
                or _OPTION_ENTRY.match(stripped)):
            continue
        for flag in _LONG_FLAG.findall(stripped):
            if flag != "--help" and flag not in flags:
                flags.append(flag)
    return flags


def parser_flags(script_path):
    flags = []
    for group in _CASE_LABEL.findall(Path(script_path).read_text()):
        for flag in group.split("|"):
            if flag.startswith("--") and flag != "--help" and flag not in flags:
                flags.append(flag)
    return flags


class TestHelpTextsAgreeWithTheParsers(unittest.TestCase):
    """A --help that names a flag the parser rejects sends whoever reads it to
    an error; a parser that accepts a flag the help never mentions is a
    feature only its author can find. Both halves are checked, and the
    documented half is checked by actually FEEDING each flag to the command --
    a help text and a parser can agree on a spelling that no longer parses.

    Nothing here mutates anything: every probe run appends an argument the
    parser cannot know, so each command exits inside its argument loop, before
    it does any work. HOME is sandboxed anyway.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-helpflags-test-")
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = ""
        self.env["HOME"] = self.tmp
        for key in list(self.env):
            if key.startswith("MEMCONTINUUM_"):
                del self.env[key]

    def _run(self, argv):
        return subprocess.run(argv, cwd=str(TOOLS_DIR), env=self.env,
                              capture_output=True, text=True, timeout=120)

    def _help_text(self, argv):
        proc = self._run(argv + ["--help"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout

    def test_the_probe_is_actually_refused(self):
        """The negative control. Every assertion below is "this flag was NOT
        called unknown", which a command that never prints that phrase would
        pass without parsing anything. So first: an argument no parser can
        know must be named, by every one of them."""
        for label, argv, prefix in PARSED_COMMANDS:
            with self.subTest(command=label):
                proc = self._run(argv + prefix + [PROBE_FLAG])
                self.assertIn("unknown argument: " + PROBE_FLAG,
                              proc.stdout + proc.stderr,
                              proc.stdout + proc.stderr)
                self.assertNotEqual(proc.returncode, 0)

    def test_every_documented_flag_is_accepted_by_the_parser(self):
        for label, argv, prefix in PARSED_COMMANDS:
            flags = documented_flags(label, self._help_text(argv))
            with self.subTest(command=label):
                # A floor, so a broken extractor cannot pass by finding none.
                self.assertGreaterEqual(len(flags), 5, flags)
            for flag in flags:
                with self.subTest(command=label, flag=flag):
                    proc = self._run(argv + prefix + [flag, PROBE_VALUE, PROBE_FLAG])
                    combined = proc.stdout + proc.stderr
                    self.assertNotIn(
                        "unknown argument: " + flag, combined,
                        f"{label} --help documents {flag}, and its parser refuses it")

    def test_every_flag_the_parser_accepts_is_documented(self):
        for label, argv, _prefix in PARSED_COMMANDS:
            script = argv[-1]
            documented = documented_flags(label, self._help_text(argv))
            for flag in parser_flags(script):
                with self.subTest(command=label, flag=flag):
                    self.assertIn(
                        flag, documented,
                        f"{label} accepts {flag} and its --help never says so")


class TestUnparsedCommandsStillRefuseUnknownFlags(unittest.TestCase):
    """H6: memlint.py and memcontinuum-state.sh sit outside PARSED_COMMANDS
    (neither has the named-flag matrix that list's tests probe -- see the
    comment above it), but both must still refuse an unrecognised `--flag`
    rather than swallow it as their one positional argument. Same negative
    control as TestHelpTextsAgreeWithTheParsers.test_the_probe_is_actually_
    refused, scoped to these two."""

    def _run(self, argv):
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        return subprocess.run(
            argv, cwd=str(TOOLS_DIR), env=env, capture_output=True, text=True,
        )

    def test_memlint_refuses_the_probe_flag(self):
        proc = self._run([PYTHON, str(TOOLS_DIR / "memlint.py"), PROBE_FLAG])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown argument: " + PROBE_FLAG, proc.stdout + proc.stderr)

    def test_state_sh_refuses_the_probe_flag(self):
        proc = self._run(
            ["bash", str(TOOLS_DIR / "scripts" / "memcontinuum-state.sh"), PROBE_FLAG])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown argument: " + PROBE_FLAG, proc.stdout + proc.stderr)


class TestDocumentedSkipBehaviour(unittest.TestCase):
    """The README promises that the tests needing machine-local data skip with
    a clear message rather than failing. Verified by running the one suite that
    reads the private incident corpus with that corpus pointed at an empty
    directory."""

    def test_private_corpus_test_skips_when_the_corpus_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            env = dict(os.environ)
            env["PYTHONPATH"] = ""
            env["MEMCONTINUUM_TEST_INCIDENTS"] = td  # present, but empty
            proc = subprocess.run(
                [PYTHON, "-m", "unittest", "-v", "tests.test_memidx.TestD1RebuildStable"],
                cwd=str(TOOLS_DIR), env=env, capture_output=True, text=True,
            )
            combined = proc.stdout + proc.stderr
            self.assertEqual(
                proc.returncode, 0,
                f"expected a clean skip, got rc={proc.returncode}:\n{combined[-1500:]}",
            )
            self.assertIn("skipped=1", combined, combined[-1500:])
            self.assertIn("fixtures/records/incidents", combined, combined[-1500:])


class TestInternalsIsLinked(unittest.TestCase):
    def test_internals_exists(self):
        self.assertTrue(INTERNALS.is_file(), f"missing {INTERNALS}")

    def test_readme_links_internals(self):
        text = README.read_text()
        self.assertIn(
            "docs/INTERNALS.md", text,
            "README must point maintainers at docs/INTERNALS.md",
        )


class TestInternalsDocumentsRootScopedCodeIndex(unittest.TestCase):
    """The code-index section has to describe --drop-root, the heal's
    --heal-limit cap, and the report's degraded state -- all three landed
    in the code-index foundation work and none of them showed up in
    INTERNALS.md by name until this doc pass."""

    def test_internals_mentions_drop_root_heal_limit_degraded(self):
        text = INTERNALS.read_text()
        for token in ("--drop-root", "--heal-limit", "degraded"):
            self.assertIn(token, text, f"docs/INTERNALS.md never mentions {token!r}")


class TestInternalsDocumentsPreEditChainWatchdog(unittest.TestCase):
    """F6 (external-review fix round): pre-edit-chain.sh moved from the
    "Unguarded" list to "Guarded", and the watchdog section documents the
    measured budget, the verified Claude Code outer-timeout default, the
    asymmetric fail-open cost, and the timeout fallback/named stats
    outcome -- not just the moved list entry on its own."""

    def test_pre_edit_chain_moved_to_guarded_not_unguarded(self):
        text = INTERNALS.read_text()
        idx = text.index("## The watchdog")
        section = text[idx:idx + 3000]
        guarded_idx = section.index("Guarded:")
        unguarded_idx = section.index("Unguarded:")
        pre_edit_idx = section.index("`pre-edit-chain.sh`")
        self.assertTrue(
            guarded_idx < pre_edit_idx < unguarded_idx,
            "pre-edit-chain.sh must be named in the Guarded list, before Unguarded:",
        )

    def test_documents_verified_outer_default_and_measured_budget(self):
        text = INTERNALS.read_text()
        self.assertIn("600 seconds", text)
        self.assertIn("p95", text)
        self.assertIn("p99", text)

    def test_documents_asymmetric_fail_open_cost(self):
        text = INTERNALS.read_text()
        self.assertIn("asymmetric", text)

    def test_documents_timeout_fallback_and_named_stats_outcome(self):
        text = INTERNALS.read_text()
        self.assertRegex(text, r"not\s+established")
        self.assertIn("watchdog-killed", text)

    def test_the_10x_headroom_figure_is_not_left_standing_as_the_whole_story(self):
        """Claims-audit item: the 34-sample controlled measurement above is
        real and stays -- but a read of an actual machine's hook.log shows
        real per-project variance the controlled run never saw, including
        the inner watchdog firing outright. The lab figure must not be the
        only thing this section says about how long a lookup actually takes
        on a real machine."""
        text = INTERNALS.read_text()
        idx = text.index("roughly 10x headroom")
        section = text[idx:idx + 900]
        self.assertIn("hook.log", section)
        self.assertIn("watchdog itself having fired", section)
        self.assertRegex(section, r"2x")


class TestReadmeLookupLatencyClaim(unittest.TestCase):
    """Claims-audit item: the pre-edit lookup paragraph used to name the
    same 34-sample controlled figure (see
    TestInternalsDocumentsPreEditChainWatchdog) as what "a real lookup
    measures", full stop. A real machine's hook.log shows that number
    holding for some projects and not others -- a distribution, not a
    constant a static string can usefully pin. This class holds two
    things a test CAN check without depending on a fresh hook.log read of
    its own: the retired absolute claim never comes back, and the
    replacement still names the watchdog deadline and admits real lookups
    are not uniformly fast. It deliberately does NOT try to pin a number
    -- "some real lookups run close to a second" is not a fact a string
    match can verify, only a hook.log read can, and that read is a
    one-time claims-audit finding, not a repeatable test fixture.
    """

    def test_no_longer_claims_the_lab_figure_as_what_every_lookup_measures(self):
        # \s+ (not a literal space) between words: README.md hard-wraps its
        # prose, so this exact phrase spans a line break in the source
        # ("...of a\nsecond...") -- a literal-space match would silently
        # never fire, before or after a fix, which defeats the whole point
        # of this test.
        text = README.read_text()
        self.assertNotRegex(
            text, r"measures\s+in\s+the\s+low\s+tenths\s+of\s+a\s+second",
            "README must not present a single controlled-measurement "
            "figure as what a real lookup measures -- production "
            "hook.log data varies sharply by which project's store is "
            "asking.",
        )

    def test_deadline_sentence_still_names_the_watchdog_and_admits_a_slow_tail(self):
        text = README.read_text()
        idx = text.index("watchdog deadline")
        section = text[max(0, idx - 20):idx + 320]
        self.assertIn("2-second", section)
        self.assertIn("project", section)
        self.assertIn("deadline itself", section)


class TestInternalsDocumentsPreEditTopicsLogging(unittest.TestCase):
    """eval-topic-logging: a matched hook.log line now names which topic
    ids were injected -- INTERNALS' "Logging, per hook" section must say so,
    name the cap, and say why (retrieval quality can be graded later), and
    CHANGELOG.md must record the change under a new Unreleased heading
    without touching the already-released rc4 section. No real `TOP-nnnn`-
    shaped store record id may appear in either (public-docs doctrine) --
    `TOP-nnnn` is the placeholder used instead."""

    def test_internals_documents_the_topics_field_and_its_cap(self):
        text = INTERNALS.read_text()
        self.assertIn("topics=", text)
        self.assertIn("10", text)
        self.assertRegex(text, r"grad(e|ing|ed)")

    def test_internals_uses_the_nnnn_placeholder_not_a_real_topic_id(self):
        text = INTERNALS.read_text()
        idx = text.index("topics=")
        snippet = text[max(0, idx - 200):idx + 400]
        self.assertIn("TOP-nnnn", snippet)

    def test_changelog_has_an_unreleased_heading_above_rc4(self):
        text = CHANGELOG.read_text()
        self.assertIn("## [Unreleased]", text)
        unreleased_idx = text.index("## [Unreleased]")
        rc4_idx = text.index("## [0.2.0rc4]")
        self.assertLess(
            unreleased_idx, rc4_idx,
            "Unreleased must sit above the already-released rc4 section",
        )

    def test_changelog_unreleased_section_mentions_topics_logging(self):
        text = CHANGELOG.read_text()
        unreleased_idx = text.index("## [Unreleased]")
        rc4_idx = text.index("## [0.2.0rc4]")
        section = text[unreleased_idx:rc4_idx]
        self.assertIn("topics=", section)

    def test_internals_documents_rotation_env_var_and_two_file_policy(self):
        text = INTERNALS.read_text()
        self.assertIn("MEMCONTINUUM_LOG_MAX_BYTES", text)
        self.assertIn("hook.log.1", text)

    def test_internals_rotation_paragraph_names_stats_and_data_loss_by_design(self):
        text = INTERNALS.read_text()
        idx = text.index("MEMCONTINUUM_LOG_MAX_BYTES")
        snippet = text[max(0, idx - 400):idx + 1200]
        self.assertIn("stats", snippet.lower())
        self.assertRegex(snippet, r"gone|lost|discarded")

    def test_changelog_unreleased_section_mentions_rotation(self):
        text = CHANGELOG.read_text()
        unreleased_idx = text.index("## [Unreleased]")
        rc4_idx = text.index("## [0.2.0rc4]")
        section = text[unreleased_idx:rc4_idx]
        self.assertIn("hook.log.1", section)

    def test_rc4_section_untouched(self):
        """The rc4 release is already shipped -- this change must not edit
        a single byte of its own section, only add a new one above it."""
        proc = subprocess.run(
            ["git", "show", "HEAD:CHANGELOG.md"],
            cwd=str(TOOLS_DIR), capture_output=True, text=True,
        )
        if proc.returncode != 0:
            self.skipTest("no committed CHANGELOG.md at HEAD to diff against")
        old_text = proc.stdout
        old_idx = old_text.index("## [0.2.0rc4]")
        old_rc4 = old_text[old_idx:]

        new_text = CHANGELOG.read_text()
        new_idx = new_text.index("## [0.2.0rc4]")
        new_rc4 = new_text[new_idx:]
        self.assertEqual(old_rc4, new_rc4, "the rc4 section must not change")


class TestReadmeListsEveryDocumentedMemidxSubcommand(unittest.TestCase):
    """A memidx.py subcommand only shows up in `memidx.py --help`'s own
    subcommand listing when its subparser was given a `help=` description
    (argparse renders those as indented sub-bullets under "positional
    arguments:"; a subcommand with none renders bare, undiscoverable from
    --help alone). The README's "commands a person actually types" block is
    supposed to name every one of those -- checked by parsing --help rather
    than trusting a maintainer to also remember the README when a new
    subcommand gets a description."""

    def _documented_subcommands(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = ""
        proc = subprocess.run(
            [PYTHON, str(TOOLS_DIR / "memidx.py"), "--help"],
            cwd=str(TOOLS_DIR), env=env, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = []
        in_positional = False
        for line in proc.stdout.splitlines():
            if line.strip() == "positional arguments:":
                in_positional = True
                continue
            if not in_positional:
                continue
            if not line.startswith(" ") or line.strip() == "options:":
                break
            m = re.match(r"^ {4}(\S+)\s{2,}\S", line)
            if m:
                names.append(m.group(1))
        return names

    def _commands_block(self):
        text = README.read_text()
        marker = "The commands a person actually types."
        idx = text.index(marker)
        fence_start = text.index("```", idx)
        fence_end = text.index("```", fence_start + 3)
        return text[fence_start:fence_end]

    def test_the_scan_finds_at_least_one_documented_subcommand(self):
        # Guards the guard: a broken --help parse would pass vacuously below.
        self.assertGreaterEqual(len(self._documented_subcommands()), 1)

    def test_readme_names_every_documented_subcommand(self):
        names = self._documented_subcommands()
        block = self._commands_block()
        missing = [n for n in names if f"memidx.py {n} " not in block]
        self.assertEqual(
            missing, [],
            f"memidx.py --help documents {names} with a help= description, "
            f"and the README's \"commands a person actually types\" block "
            f"never mentions {missing}",
        )


class TestF8F9Documented(unittest.TestCase):
    def test_internals_states_the_f8_ceiling_in_searchable_vectors_not_topic_count(self):
        text = INTERNALS.read_text()
        self.assertNotIn("≤120 topics", text)
        self.assertIn("searchable vector", text.lower())

    def test_internals_names_edges_for_topic_as_presentation_not_reasoning(self):
        text = INTERNALS.read_text()
        self.assertIn("presentation, not reasoning", text)


class TestDocsRound7(unittest.TestCase):
    def test_skill_search_recipe_is_hybrid_not_vector(self):
        text = SEARCH_SKILL.read_text()
        self.assertNotIn("--mode vector", text.split("## Reading the output")[0])
        self.assertIn("--mode hybrid", text)

    def test_skill_search_recipe_does_not_call_status_active_the_default(self):
        """Grok re-gate MINOR 3: the no-flag default is active-OR-no-status,
        while an explicit `--status active` is STRICTER (it drops the
        status-less records the default keeps) -- the recipe must not pass
        `--status active` and call it merely a spelled-out default."""
        text = SEARCH_SKILL.read_text()
        recipe_line = next(l for l in text.splitlines() if "memidx.py search" in l)
        self.assertNotIn("--status", recipe_line)
        self.assertNotIn("naming the engine's own default explicitly", text)
        self.assertIn("status-less", text)

    def test_store_readme_search_recipe_carries_status_active(self):
        text = STORE_README_TMPL.read_text()
        recipe_line = next(l for l in text.splitlines() if "memidx.py search" in l)
        self.assertIn("--status", recipe_line)

    def test_pyproject_description_no_longer_claims_whole_record_embeddings(self):
        text = PYPROJECT.read_text()
        self.assertNotIn("whole-record embeddings", text)

    def test_store_readme_distinguishes_owner_verbatim_from_owner_ratified(self):
        text = STORE_README_TMPL.read_text()
        self.assertIn("agent-drafted", text)

    def test_rules_template_names_the_authority_label_not_just_exact_words(self):
        text = RULES_TEMPLATE.read_text()
        self.assertIn("authority", text.lower())

    def test_readme_blocking_paragraph_names_hold(self):
        text = README.read_text()
        self.assertIn("HOLD", text)

    def test_readme_append_only_notes_the_linter_enforces_it(self):
        # Originally pinned "linter does not enforce" (the append-only
        # check used to be schema documentation only, deliberately not
        # implemented -- see docs/SCHEMA.md section 7's old text). Task
        # A2-1 (TOP-0122 L1 rule 3) implemented it as `memlint.py
        # --against-ref` plus a store pre-commit hook, so the sentence
        # became false and was rewritten to say so; this pin moves with
        # it, in the same commit, rather than pinning stale wording.
        text = README.read_text()
        self.assertIn("linter enforces this", text.lower())

    def test_schema_current_field_comment_says_hand_set(self):
        text = SCHEMA.read_text()
        self.assertNotIn("DERIVED by the linter", text)

    def test_schema_incidents_section_names_every_field_actually_used(self):
        # G2 was vacuous as first drafted: docs/SCHEMA.md already contains
        # "incident" and "investigation" as substrings today, in unrelated
        # locations, so assertIn on those words alone passes on the
        # UNEDITED file. Enumerate the REAL frontmatter keys from the
        # actual incident records at runtime and require the new section
        # to name every one of them -- this is red until the section both
        # exists and actually reflects the real corpus.
        #
        # `memory/` is this engine repo's OWN store (its real incident
        # corpus), present in the main checkout but gitignored -- a
        # worktree carries no copy of it at all, same shape as fixtures/
        # records/ (see TestDocumentedSkipBehaviour above). Skip cleanly
        # rather than fail when it is absent, matching the README's own
        # promise that machine-local-data tests skip with a clear message.
        incidents_dir = TOOLS_DIR / "memory" / "incidents"
        keys = set()
        for f in sorted(incidents_dir.glob("*.md")):
            fm, _ = memidx.parse_frontmatter(f)
            keys.update(fm.keys())
        if not keys:
            self.skipTest(
                f"no incident files under {incidents_dir} -- this engine "
                "repo's own store is machine-local, gitignored data; point "
                "MEMCONTINUUM_TEST_INCIDENTS-style local setup at it (or "
                "symlink memory/ to the real store) to run this test"
            )
        text = SCHEMA.read_text()
        self.assertIn("Incidents and investigations", text)
        section = text.split("Incidents and investigations", 1)[1]
        for key in sorted(keys):
            self.assertIn(key, section,
                           f"SCHEMA.md's incidents section must name field {key!r} (seen in real incident frontmatter)")

    def test_internals_probe_disclosure_pointer_sits_next_to_the_10_of_10_claim(self):
        text = INTERNALS.read_text()
        idx_claim = text.find("10/10 top-1 paraphrase")
        idx_pointer = text.find("private, untracked files")
        self.assertNotEqual(idx_claim, -1, "the 10/10 claim itself must still exist")
        self.assertNotEqual(idx_pointer, -1, "the new disclosure pointer sentence must exist")
        self.assertLess(abs(idx_pointer - idx_claim), 400,
                         "the disclosure pointer must sit right next to the 10/10 claim, not stay only in the far-below disclosure")


class TestDoctrineMachineryCoversDocsRound7Files(unittest.TestCase):
    def test_public_docs_includes_the_files_task_8_writes_into(self):
        names = {str(p) for p in PUBLIC_DOCS}
        for rel in ("templates/store-README.md.tmpl", "templates/memcontinuum-rules.md",
                    "docs/SCHEMA.md", "pyproject.toml", "CHANGELOG.md"):
            self.assertTrue(any(rel in n for n in names), f"{rel} missing from PUBLIC_DOCS")

    def test_forbidden_catches_a_store_record_id_and_a_finding_code_label(self):
        sample_id = "See TOP-0116 for the ruling."
        sample_label = "F1: decision-index provenance state"
        self.assertTrue(any(p.search(sample_id) for _, p in FORBIDDEN),
                         "FORBIDDEN has no pattern for a bare TOP-#### store record id")
        self.assertTrue(any(p.search(sample_label) for _, p in FORBIDDEN),
                         "FORBIDDEN has no pattern for an F#: finding-code label")

    def test_forbidden_catches_the_em_dash_hyphen_and_milestone_sub_label_shapes(self):
        # Re-gate item 3: the widened pattern's OWN self-test used to check
        # only the colon shape (F1:) -- it never proved the em dash/hyphen
        # separators the widening was specifically FOR, nor the milestone
        # sub-label shape (M2a) the pattern's own comment claimed to cover.
        em_dash_label = "**F8 — no ANN index; a linear scan over every searchable vector.**"
        hyphen_label = "D3 - decision-index engine test-class label"
        milestone_sub_label = "Anatomy M2a: binding point 2"
        for sample, desc in (
            (em_dash_label, "an em-dash-separated F# label"),
            (hyphen_label, "a hyphen-separated D# label"),
            (milestone_sub_label, "a colon-separated M#<letter> milestone sub-label"),
        ):
            self.assertTrue(any(p.search(sample) for _, p in FORBIDDEN),
                             f"FORBIDDEN has no pattern catching {desc}: {sample!r}")


class TestInternalsDocumentsTreeSitterTier(unittest.TestCase):
    """Task 12: the Code index section must name backend-preflight,
    MEMCONTINUUM_VENV_MANAGED, and every LANGUAGE_TABLE row's own language
    name (tsx included) so a reader of docs/INTERNALS.md alone knows the
    tree-sitter tier exists and how to check it -- not just the README's
    six human-named languages, but all seven registry rows."""

    def test_mentions_backend_preflight_and_venv_managed(self):
        text = INTERNALS.read_text()
        self.assertIn("backend-preflight", text)
        self.assertIn("MEMCONTINUUM_VENV_MANAGED", text)

    def test_mentions_every_registry_language_name(self):
        text = INTERNALS.read_text()
        for lang in chunkers.LANGUAGE_TABLE:
            self.assertIn(
                lang, text,
                f"docs/INTERNALS.md never names the {lang!r} LANGUAGE_TABLE row",
            )


class TestSkillHonesty(unittest.TestCase):
    """skill-honesty: the memcontinuum skill is an agent's operating manual,
    not documentation about code -- anything it restates that repo-init.sh /
    mc-registry-lib.sh already compute (the default store location, its
    refusals) will drift the moment the code changes, unseen, exactly as it
    did for nine days before this fix (memory/incidents/
    machine-layer-drifted-unseen-for-nine-days.md). It must instead point the
    agent at the dry-run's own printed output. Separately, the consent flow
    must be a pinned structured prompt for every state the skill can see --
    not prose, and not silent for `wired`/`declined`/`partial-wired`, which
    used to leave the agent to improvise (the same defect S2 fixed for
    `undecided`, in three more states).

    Fix round 2 (skill-honesty gate): both reviewers mutated a copy of
    SKILL.md -- dropping options, reordering them, making the dry-run
    optional, allowing store deletion, reintroducing a removed computed
    rule under different wording -- and the ORIGINAL version of this class
    caught none of it, because it only checked isolated substrings.
    `_options()` below now pins each state's COMPLETE option list (text,
    order, and count in one `assertEqual`, so a drop/add/reorder/reword all
    fail), FORBIDDEN_PATTERNS below catches a removed rule regrown in
    different words (not just its exact historical phrase), and
    TestSkillHonestyMutations re-applies the reviewers' own mutations,
    in-memory, against these tests to prove each one now fails.

    What this class cannot catch, by construction: a computed rule restated
    in wording that matches none of FORBIDDEN_PATTERNS (the pattern list is
    finite, not a semantic diff against the tool's own source); a
    dropped/altered SENTENCE inside an option's pinned text that some other
    assertion doesn't happen to cover; and anything about behavior once an
    agent leaves the page -- these tests read SKILL.md as text, they never
    run it.
    """

    # Exact phrases the old SKILL.md used to restate a rule repo-init.sh /
    # mc-registry-lib.sh computes on its own -- the default store's WSL-disk
    # rule and its plain-sibling fallback, and --project's character class.
    # Regrowing any of these means the skill is predicting an answer again
    # instead of reading it off the tool's own dry-run output.
    REMOVED_PHRASES = [
        "Windows-mounted",
        "$HOME/dev/<repo>-MemContinuum-Store",
        "beside the git repo the cwd is in",
        "[A-Za-z0-9._-]+",
        "earns its keep",
        "does not enforce this name",
        "is REQUIRED here",
        "except the one flow in step 4",
    ]

    # Same removed rules, but matched as PATTERNS rather than one exact
    # historical phrase -- a reviewer mutation reintroduced the WSL rule as
    # "Windows mounted" (no hyphen) and "$HOME/dev" (no full suffix),
    # neither of which REMOVED_PHRASES above would catch.
    FORBIDDEN_PATTERNS = [
        (re.compile(r'windows[\s-]?mounted', re.IGNORECASE),
         "the Windows-mounted-drive default-location rule repo-init.sh computes"),
        (re.compile(r'\$HOME/dev\b'),
         "the WSL default store location repo-init.sh computes"),
        (re.compile(
            r'--store\b.{0,80}(?:requires?|must\s+(?:be\s+)?(?:paired|accompanied)|needs?)'
            r'\b.{0,80}--claude-dir',
            re.IGNORECASE | re.DOTALL,
         ),
         "the --store/--claude-dir pairing repo-init.sh already enforces and reports"),
        (re.compile(r'inside an existing git repo', re.IGNORECASE),
         "the nested-repo refusal message repo-init.sh already prints"),
        # Widened from a literal "five hooks" (regate round 2, Grok N6): a
        # mutation reworded this as "five write-side hooks" and slipped
        # through the exact two-word phrase. Up to two words may sit between
        # the count and "hooks" now.
        (re.compile(r'\bfive\b(?:\s+\S+){0,2}\s+hooks\b', re.IGNORECASE),
         "a hardcoded hook count (the write-side count is a fact of the templates, not this prose)"),
    ]

    STATE_MARKERS = [
        ("**`undecided`**", "**`partial-wired`**"),
        ("**`partial-wired`**", "**`wired`**"),
        ("**`wired`**", "**`declined`**"),
        ("**`declined`**", "**`not-a-repo`"),
    ]

    @staticmethod
    def _section(text, start_marker, end_marker):
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        return text[start:end]

    @classmethod
    def _ask_section(cls, text):
        return cls._section(text, "## 2. Ask", "## 3. Act on the answer")

    @staticmethod
    def _options(section):
        """Every top-level numbered option ("N. ...") in one state's prompt
        section, in document order, each whitespace-normalized across its
        own continuation lines (markdown hard-wraps prose, so a pinned
        option can legitimately carry a newline+indent that isn't a wording
        change). Line-based, not a single normalize-then-regex pass over
        the whole section: a numbered list's continuation lines are never
        blank-separated from their own item, but ARE separated from
        whatever prose follows the list (e.g. `wired`'s "Moving a wired
        store..." paragraph after its two options) -- the first blank line
        ends the LIST (nothing after it is collected as an option's text).
        Pinning len()+order+text in one assertEqual against this list's
        output fails on a dropped, added, reordered, OR reworded option --
        not just a phrase substring, which is what let every one of the
        reviewers' option mutations slip past the original version of this
        class. The printed NUMBER itself is also checked here, not just
        discarded (brief B2: pin "numbering" too) -- a renumbering with no
        reorder (e.g. "1. Keep as is" / "3. Stop using...") would pass a
        text-and-order-only check, so each item's digit must equal its
        1-based position or this raises immediately.

        Fix round (Grok gate finding 3): the list ending on a blank line
        used to mean nothing past it was even LOOKED at -- a numbered item
        re-appearing there still got caught (it re-enters the counted
        sequence below and trips the len()/numbering checks), but an
        UNNUMBERED bullet (`- Maybe later -- ...`) slipped through
        completely silent, because a plain `-`/`*` line never matched the
        numbered-option regex and, once the list had closed, nothing else
        was watching for it either. Once the list closes, this now also
        watches for bullet-shaped stray content (`- `/`* ` at the start of
        a line) and raises immediately if it sees one -- ordinary prose
        after the list (no leading bullet marker) still passes through
        uncaptured, same as before."""
        items = []
        current = None
        list_closed = False
        for line in section.splitlines():
            m = re.match(r'^\s*(\d+)\.\s+(.*)$', line)
            if m:
                if current is not None:
                    items.append(current)
                expected = len(items) + 1
                assert int(m.group(1)) == expected, (
                    f"option numbered {m.group(1)!r} where {expected} was expected: {m.group(2)!r}"
                )
                current = m.group(2).strip()
                continue
            if current is not None:
                stripped = line.strip()
                if stripped == "":
                    items.append(current)
                    current = None
                    list_closed = True
                else:
                    current += " " + stripped
                continue
            if list_closed and re.match(r'^\s*[-*]\s+\S', line):
                raise AssertionError(
                    "option-shaped bullet content after this state's "
                    f"option list already closed on a blank line: {line.strip()!r}"
                )
        if current is not None:
            items.append(current)
        return items

    @classmethod
    def _state_options(cls, text, start_marker, end_marker):
        return cls._options(cls._section(cls._ask_section(text), start_marker, end_marker))

    def test_no_computed_store_rules_restated(self):
        text = SKILL.read_text()
        for phrase in self.REMOVED_PHRASES:
            self.assertNotIn(
                phrase, text,
                f"SKILL.md restates a rule repo-init.sh/mc-registry-lib.sh "
                f"computes ({phrase!r}) -- point at the dry-run's own "
                "output instead of predicting it"
            )

    def test_no_computed_rule_restated_in_different_wording(self):
        text = SKILL.read_text()
        for pattern, label in self.FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            self.assertIsNone(
                match, f"SKILL.md restates {label}, using wording "
                f"REMOVED_PHRASES does not pin ({match.group(0) if match else ''!r})"
            )

    def test_store_location_points_at_the_dry_run_verbatim(self):
        text = SKILL.read_text()
        self.assertIn("dry-run", text)
        self.assertIn("verbatim", text)
        self.assertIn("`store :` line", text)

    def test_consent_section_names_the_structured_prompt(self):
        text = SKILL.read_text()
        section = self._ask_section(text)
        # Whitespace-normalized: markdown hard-wraps prose at ~80 columns, so
        # a pinned multi-word phrase can legitimately carry a newline+indent
        # between two of its words without the sentence having changed.
        normalized = " ".join(section.split())
        self.assertIn("structured multiple-choice prompt", normalized)
        self.assertIn("never prose", normalized)

    def test_undecided_names_all_four_options_exactly(self):
        options = self._state_options(SKILL.read_text(), "**`undecided`**", "**`partial-wired`**")
        self.assertEqual(options, [
            "Yes, with code retrieval — records, plus decisions surfaced before edits under the named code root",
            "Yes, rationale only — records, no code retrieval",
            "No — record the decline; this repo is never asked again",
            "Not now — nothing is recorded; run `/memcontinuum` again to decide",
        ])

    def test_partial_wired_names_all_three_options_exactly(self):
        options = self._state_options(SKILL.read_text(), "**`partial-wired`**", "**`wired`**")
        self.assertEqual(options, [
            "Complete the wiring — finishes what a prior install left half-done",
            "Remove what is there",
            "Not now — leave it half-wired; run `/memcontinuum` again to decide",
        ])

    def test_wired_names_both_options_exactly(self):
        # Fix round 2 (B1): "Change where the store lives" and "Add or
        # remove code retrieval" were dropped -- repo-init.sh re-renders a
        # hook group entirely from the current invocation's own flags with
        # no way to read back what was already wired, so either option was
        # a data-loss trap (see step 4 and memory/incidents/ for the
        # reproduction). Only the two options the tooling can do safely
        # remain.
        options = self._state_options(SKILL.read_text(), "**`wired`**", "**`declined`**")
        self.assertEqual(options, [
            "Keep as is — nothing changes",
            "Stop using MemContinuum here — record the decline (the hooks stay wired until removed by hand; say so)",
        ])

    def test_declined_names_both_options_exactly(self):
        options = self._state_options(SKILL.read_text(), "**`declined`**", "**`not-a-repo`")
        self.assertEqual(options, [
            "Keep declined",
            "Wire it after all",
        ])

    def test_wired_and_declined_first_option_keeps_the_recorded_answer(self):
        # The true invariant (corrected mid-task: the coordinator's own
        # spec for `partial-wired` puts "Complete the wiring" -- not a
        # no-change option -- first, so "the first option is ALWAYS the
        # safe one" was never accurate for every state). What actually
        # holds everywhere: a repo with a recorded answer (`wired`,
        # `declined`) always offers that answer, unchanged, as option 1.
        text = SKILL.read_text()
        wired = self._state_options(text, "**`wired`**", "**`declined`**")
        declined = self._state_options(text, "**`declined`**", "**`not-a-repo`")
        self.assertEqual(wired[0], "Keep as is — nothing changes")
        self.assertEqual(declined[0], "Keep declined")

    def test_no_option_ever_reads_as_deleting_a_store(self):
        # Behavior-level guard rather than a phrase: scan every option's
        # own text, in every state, for anything that reads as destroying
        # the store -- catches a NEW destructive option added anywhere,
        # regardless of the words it uses.
        text = SKILL.read_text()
        destructive = re.compile(r'delete|destroy|\bwipe\b|rm -rf', re.IGNORECASE)
        for start, end in self.STATE_MARKERS:
            for opt in self._state_options(text, start, end):
                self.assertIsNone(
                    destructive.search(opt),
                    f"an option in {start} reads as deleting the store: {opt!r}"
                )

    def test_no_option_ever_deletes_a_store_is_a_stated_rule(self):
        text = SKILL.read_text()
        # "## 5. Rules" is the last section -- slice to end of file rather
        # than to a following marker that does not exist.
        rules = text[text.index("## 5. Rules"):]
        self.assertIn("Never delete a store, ever", rules)
        self.assertIn("never destructive by accident", rules)
        self.assertIn("recorded answer", rules)

    def test_dry_run_is_never_optional(self):
        # Pins the canonical dry-run paragraphs, whitespace-normalized,
        # rather than a loose substring -- "make the dry-run optional" was
        # one of the reviewers' mutations and the word "Always" is exactly
        # what such a mutation would drop or hedge.
        text = SKILL.read_text()
        section = self._section(text, "## 3. Act on the answer", "## 4. Reversing")
        normalized = " ".join(section.split())
        self.assertIn(
            "**Always dry-run first, and read the `store :` line — and any "
            "`note:` line above it — out of that dry-run's own output, "
            "verbatim, to the human.**",
            normalized,
        )
        self.assertIn("Always dry-run first, show the plan, then run it.", normalized)

    def test_not_a_repo_and_no_config_get_no_prompt(self):
        text = SKILL.read_text()
        section = self._ask_section(text)
        tail = section[section.index("**`not-a-repo`"):]
        self.assertIn("no prompt", tail)


class TestSkillHonestyMutations(unittest.TestCase):
    """Proves the pinned tests above actually catch what they claim to --
    against the REAL production assertions, not a second, independently
    written check that merely replicates what the guard is supposed to do.

    Fix round 3 (skill-honesty re-gate, Codex 5 / Grok 5): the previous
    version of this class re-implemented each assertion inline (comparing
    `_state_options()` output, or re-scanning FORBIDDEN_PATTERNS, by hand)
    instead of calling the `TestSkillHonesty` method it claimed to be
    proving. Codex demonstrated the gap by replacing every production
    honesty test with a no-op in memory and reran this class: all eight
    still passed. That made this class fixture coverage, not proof.

    Every test below now uses `_mutated_skill()` to point the shared module
    global `SKILL` at a temp file holding the mutated text, then calls the
    actual bound `TestSkillHonesty` test method by name -- the same method
    that runs in the real suite, reading `SKILL.read_text()` itself. If a
    future edit ever weakens or deletes the underlying production
    assertion, the corresponding test here fails too, because there is no
    second copy of the logic left to keep passing on its own.

    A mutation whose regex/replace finds nothing to change is a stale
    fixture, not a passing test -- guarded by assertNotEqual against the
    unmutated text first."""

    def setUp(self):
        self.text = SKILL.read_text()

    @staticmethod
    @contextlib.contextmanager
    def _mutated_skill(mutated_text):
        """Point the module-level SKILL global at a temp file holding
        `mutated_text` for the duration of the `with` block, then restore
        it. Test methods on TestSkillHonesty reference the bare module
        global `SKILL` (resolved at call time, not bound to `self`), so
        this makes their own `SKILL.read_text()` read the mutation."""
        global SKILL
        real_skill = SKILL
        fd, tmp_name = tempfile.mkstemp(suffix=".md")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(mutated_text)
            SKILL = tmp_path
            yield
        finally:
            SKILL = real_skill
            tmp_path.unlink(missing_ok=True)

    def _assert_production_test_catches(self, mutated_text, test_name):
        with self._mutated_skill(mutated_text):
            with self.assertRaises(
                AssertionError,
                msg=f"TestSkillHonesty.{test_name} did not fail against the mutation",
            ):
                getattr(TestSkillHonesty(test_name), test_name)()

    def test_dropping_a_wired_option_is_caught(self):
        mutated = self.text.replace(
            "1. Keep as is — nothing changes\n"
            "2. Stop using MemContinuum here — record the decline (the hooks stay wired\n"
            "   until removed by hand; say so)\n",
            "1. Keep as is — nothing changes\n",
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_wired_names_both_options_exactly")

    def test_reordering_partial_wired_options_is_caught(self):
        mutated = self.text.replace(
            "1. Complete the wiring — finishes what a prior install left half-done\n"
            "2. Remove what is there\n"
            "3. Not now — leave it half-wired; run `/memcontinuum` again to decide\n",
            "1. Remove what is there\n"
            "2. Complete the wiring — finishes what a prior install left half-done\n"
            "3. Not now — leave it half-wired; run `/memcontinuum` again to decide\n",
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_partial_wired_names_all_three_options_exactly")

    def test_adding_an_extra_undecided_option_is_caught(self):
        mutated = self.text.replace(
            "4. Not now — nothing is recorded; run `/memcontinuum` again to decide\n\n"
            "**`partial-wired`**",
            "4. Not now — nothing is recorded; run `/memcontinuum` again to decide\n"
            "5. Maybe later — think about it and decide next week\n\n"
            "**`partial-wired`**",
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_undecided_names_all_four_options_exactly")

    def test_adding_an_unnumbered_bullet_after_undecided_options_is_caught(self):
        # Grok gate finding 3: a numbered "5." after the closing blank line
        # was already caught (see test_adding_an_extra_undecided_option_is_
        # caught above -- it re-enters the counted sequence and trips the
        # len()/numbering checks), but an UNNUMBERED bullet in the same
        # position slipped past every test in this file: _options() never
        # captured, or even watched, anything once the list's closing blank
        # line had gone by. Reproduces the reviewer's exact defeat.
        mutated = self.text.replace(
            "4. Not now — nothing is recorded; run `/memcontinuum` again to decide\n\n"
            "**`partial-wired`**",
            "4. Not now — nothing is recorded; run `/memcontinuum` again to decide\n\n"
            "- Maybe later — think about it and I will wire it for you\n\n"
            "**`partial-wired`**",
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_undecided_names_all_four_options_exactly")

    def test_making_the_dry_run_optional_is_caught(self):
        mutated = self.text.replace("Always dry-run first, show the plan, then run it.",
                                     "Optionally dry-run first, show the plan, then run it.")
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_dry_run_is_never_optional")

    def test_allowing_store_deletion_is_caught(self):
        mutated = self.text.replace(
            "1. Keep declined\n2. Wire it after all\n",
            "1. Keep declined\n2. Wire it after all\n3. Delete the store and start over\n",
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_no_option_ever_reads_as_deleting_a_store")

    def test_reintroducing_the_wsl_rule_in_different_words_is_caught(self):
        mutated = self.text.replace(
            "**`undecided`** — four options:",
            "On a Windows mounted drive the default landing spot is under "
            "$HOME/dev.\n\n**`undecided`** — four options:",
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_no_computed_rule_restated_in_different_wording")

    def test_deleting_the_never_delete_rule_is_caught(self):
        mutated = self.text.replace(
            "- Never delete a store, ever, regardless of what is asked. Never move one\n"
            "  on your own judgment either — step 4's relocation path is a deliberate,\n"
            "  human-directed command, never something this skill decides or automates\n"
            "  by itself.\n",
            "",
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_no_option_ever_deletes_a_store_is_a_stated_rule")

    def test_restoring_the_claude_dir_requirement_is_caught(self):
        mutated = self.text.replace(
            "## 3. Act on the answer",
            "An explicit --store REQUIRES an explicit --claude-dir alongside it.\n\n"
            "## 3. Act on the answer",
            1,
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_no_computed_rule_restated_in_different_wording")

    def test_widened_hook_count_rewording_is_caught(self):
        # Regate round 2, Grok N6: "five write-side hooks" slipped past the
        # old literal "five hooks" FORBIDDEN_PATTERNS entry. Proves the
        # widened pattern (see FORBIDDEN_PATTERNS above) now catches it via
        # the real production test, not just a standalone regex check.
        mutated = self.text.replace(
            "## 5. Rules",
            "This wiring always installs five write-side hooks.\n\n## 5. Rules",
            1,
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_no_computed_rule_restated_in_different_wording")

    def test_rewording_the_store_claude_dir_pairing_without_requires_is_caught(self):
        # Regate round 2, Grok N1: "--store must be paired with --claude-dir"
        # (no "requires") slipped past the old requires?-only pattern.
        mutated = self.text.replace(
            "## 3. Act on the answer",
            "An explicit --store must be paired with an explicit --claude-dir.\n\n"
            "## 3. Act on the answer",
            1,
        )
        self.assertNotEqual(mutated, self.text, "fixture stale: nothing matched")
        self._assert_production_test_catches(mutated, "test_no_computed_rule_restated_in_different_wording")


class TestNewlangDecisionPointDocumented(unittest.TestCase):
    """N1 (owner ruling, TOP-0128): skills/memcontinuum/SKILL.md must
    document what an agent actually runs for each of the newfile-nudge
    hook's three answers -- section 6, added this fix round. This class
    pins the section's presence and its most load-bearing facts (the
    tiered --never-ext cost is the whole point of N1's honesty
    requirement); TestSkillHonesty's own FORBIDDEN_PATTERNS/REMOVED_PHRASES
    scans already cover this section too since they scan the whole file,
    not just step 2 -- not duplicated here."""

    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text()

    def _section(self, start_marker, end_marker=None):
        start = self.text.index(start_marker)
        end = self.text.index(end_marker, start) if end_marker else len(self.text)
        return self.text[start:end]

    def _normalized_section(self, start_marker, end_marker=None):
        # Markdown hard-wraps prose, so a pinned multi-word phrase can
        # legitimately carry a newline+indent between two of its words
        # without the sentence having changed -- same normalization
        # TestSkillHonesty uses for its own multi-line phrase checks.
        return " ".join(self._section(start_marker, end_marker).split())

    def test_section_six_exists_and_names_the_three_options(self):
        section = self._section("## 6. The new-file nudge's language offer")
        for label in ("Wire it now", "Never mention it again", "Not now"):
            self.assertIn(label, section, section)

    def test_option_one_points_at_step_four_without_restating_it(self):
        section = self._normalized_section("## 6. The new-file nudge's language offer")
        self.assertIn("step 4", section)
        # Never restate the actual re-run flags here (INC-0117) -- step 4
        # is the one place that names them.
        for restated_flag in ("--claude-dir DIR --project", "--record-decision ("):
            self.assertNotIn(restated_flag, section)

    def test_option_two_names_the_tiered_never_ext_cost_honestly(self):
        section = self._normalized_section("## 6. The new-file nudge's language offer")
        self.assertIn("--never-ext", section)
        self.assertIn("memcontinuum-update.sh", section)
        self.assertIn("one command", section)
        # The conditional nature is the whole point -- must not read as
        # unconditionally one command.
        self.assertIn("no one-command path", section)
        self.assertIn("no-code-root:", section)
        self.assertIn("no-wired-row:", section)

    def test_option_three_records_nothing(self):
        section = self._normalized_section("## 6. The new-file nudge's language offer")
        idx = section.index("3. **Not now**")
        tail = section[idx:]
        self.assertIn("nothing is recorded", tail)


class TestConsentIsManualNotAutomatic(unittest.TestCase):
    """TOP-0110 L3: manual wiring (running `/memcontinuum` in a repository)
    is the ONLY official way to set up a repo. The SessionStart detector
    speaks through `hookSpecificOutput.additionalContext`, delivered to the
    ASSISTANT, never to the human -- nothing can compel an assistant to
    surface it. Across two observed sessions the owner was never asked;
    both times he reached the setup dialogue only by invoking the skill
    himself. The README used to promise "You get asked once", which is
    exactly the claim this ruling forbids: a doc may describe the detector
    as advisory, never as something that reaches the human without the
    human (or an unreliable assistant) acting on it.

    The same retired promise also sat in section 2 of
    skills/memcontinuum/SKILL.md itself -- the pinned structured-prompt
    "Not now" option, in the single most user-visible place in the
    product, said the human "will be asked again next session". Fixed
    alongside the README (the option text now points at re-running
    `/memcontinuum`, TestSkillHonesty above pins the corrected list); this
    class additionally pins the retired phrasing as absent from the whole
    skill file, not just from step 3's prose.

    These tests pin the retired phrasing's absence and the replacement's
    substance for a specific reason: a PUBLIC_DOCS-wide keyword scan for
    "asked" would also trip on skills/memcontinuum/SKILL.md's own pinned
    structured-prompt options (settled design, tested by TestSkillHonesty
    above -- e.g. "this repo is never asked again", the `declined` option,
    is a true statement about a LIVE dialogue the human just triggered by
    running the skill, not an unprompted claim) and on docs/INTERNALS.md's
    accurate internal description of the detector's own hook-state
    machine (`undecided` | `emits, once` names what the hook puts into
    additionalContext, not a promise about who sees it). Exact-phrase
    pins, not a blanket regex, are what THIS class checks -- and Grok's
    gate proved their limit: a REPHRASED promise ("You will be asked once
    at session start whether this repo should keep a decision store.",
    "you'll be prompted next time you open this repo") slipped past every
    pin here while restating exactly the claim this ruling forbids.
    TestNoRephrasedAskPromise below is the semantic guard that catches a
    rephrase by its SHAPE -- a human named, by "you", as the recipient of
    ask/prompt/remind/offer, in a future or habitual construction --
    rather than one fixed wording; it runs alongside these pins, not
    instead of them. It is not a full semantic diff either: a promise
    with no "you" as the recipient (a bare passive "the repo will be
    asked again", or third person throughout) is outside what either
    guard catches by shape and still depends on the exact pins above, or
    a reviewer's eye, to be caught.
    """

    @staticmethod
    def _between(text, start_marker, end_marker):
        start = text.index(start_marker)
        end = text.index(end_marker, start)
        return text[start:end]

    def test_readme_no_longer_claims_you_get_asked_once(self):
        text = README.read_text()
        self.assertNotIn("You get asked once", text)
        self.assertNotIn("the detector puts one question to you", text)

    def test_readme_day_to_day_leads_with_manual_invocation(self):
        text = README.read_text()
        section = text[text.index("## Day to day"):text.index("**`/memcontinuum` any time.**")]
        normalized = " ".join(section.split())
        self.assertIn(
            "run `/memcontinuum` inside it, once — that is the supported "
            "way to decide",
            normalized,
        )

    def test_readme_day_to_day_states_the_detector_is_advisory_only(self):
        text = README.read_text()
        section = text[text.index("## Day to day"):text.index("**`/memcontinuum` any time.**")]
        normalized = " ".join(section.split())
        self.assertIn(
            "that channel is advisory: it reaches the assistant, not you, "
            "and nothing here can compel an assistant to raise it",
            normalized,
        )
        self.assertIn(
            "if a session never asks, that is the expected case, not a bug",
            normalized,
        )

    def test_readme_day_to_day_keeps_the_registry_and_reversal_substance(self):
        # The one part of the old paragraph that was TRUE and had to survive
        # the rewrite: an answer is recorded permanently and reversible.
        text = README.read_text()
        section = text[text.index("## Day to day"):text.index("**`/memcontinuum` any time.**")]
        normalized = " ".join(section.split())
        self.assertIn(
            "recorded permanently in the machine's decision registry and "
            "can be changed again at any time through the same skill",
            normalized,
        )

    def test_readme_day_to_day_does_not_overclaim_not_now_is_recorded(self):
        # TOP-0110 L3, Grok gate finding 3: "Whatever you answer through
        # /memcontinuum is recorded permanently ... and can be changed
        # again" was FALSE for "Not now" -- skills/memcontinuum/SKILL.md's
        # own "Not now" option records nothing (section 2's option list,
        # section 3's "'Not now' -> record nothing" heading). Only a
        # yes-or-no answer is recorded and reversible; the paragraph now
        # says so and names the carve-out rather than claiming universal
        # coverage.
        text = README.read_text()
        section = text[text.index("## Day to day"):text.index("**`/memcontinuum` any time.**")]
        normalized = " ".join(section.split())
        self.assertNotIn("Whatever you answer", normalized)
        self.assertIn(
            "A yes-or-no answer through `/memcontinuum` is recorded "
            "permanently",
            normalized,
        )
        self.assertIn(
            'answering "Not now" instead records nothing and leaves the '
            "repo undecided",
            normalized,
        )

    def test_readme_install_section_names_the_manual_step_as_required(self):
        text = README.read_text()
        section = self._between(text, "### Once per repository", "```bash")
        normalized = " ".join(section.split())
        self.assertIn(
            "Run `/memcontinuum` inside the repository — that is the "
            "required step",
            normalized,
        )

    def test_readme_noticeable_paragraph_does_not_imply_the_human_is_asked(self):
        text = README.read_text()
        self.assertNotIn(
            "makes an un-initialized repository *noticeable*:", text,
            "the old phrasing read as a promise to the human -- it must "
            "name the assistant as the audience instead",
        )
        section = self._between(
            text, "This step is what makes an un-initialized repository",
            "### Once per repository",
        )
        normalized = " ".join(section.split())
        self.assertIn("noticeable to the assistant", normalized)
        self.assertIn("Noticing is not the same as asking you", normalized)

    def test_skill_frontmatter_names_itself_the_official_manual_entry_point(self):
        text = SKILL.read_text()
        frontmatter = text.split("---", 2)[1]
        self.assertIn("official, manual way", frontmatter)

    def test_skill_opening_states_the_detector_is_advisory_only(self):
        text = SKILL.read_text()
        section = text[:text.index("## 1. Read the current state")]
        normalized = " ".join(section.split())
        self.assertIn(
            "that channel is advisory only — it reaches the assistant, "
            "never the human directly",
            normalized,
        )

    def test_skill_not_now_paragraph_no_longer_promises_a_bare_re_ask(self):
        # This sentence sits in "## 3. Act on the answer", explanatory prose
        # around the pinned option list in "## 2. Ask" (section 2's own
        # option text carried the same false promise and got the matching
        # fix -- see test_skill_section_2_no_longer_promises_a_bare_re_ask
        # below and TestSkillHonesty's pinned option lists above). This
        # paragraph is not pinned there and carried the same false promise
        # in the assistant's own follow-up explanation, so it gets the same
        # correction as the README.
        text = SKILL.read_text()
        section = self._between(
            text, '**"Not now" → record nothing.**', "## 4. Reversing",
        )
        normalized = " ".join(section.split())
        self.assertNotIn(
            "the detector stays quiet for the rest of this session and "
            "asks again next time",
            normalized,
        )
        self.assertIn("no guarantee it reaches the human unless", normalized)

    def test_skill_section_2_no_longer_promises_a_bare_re_ask(self):
        # TOP-0110 L3: section 2's pinned "Not now" option (the single most
        # user-visible place in the product -- a numbered choice the human
        # sees live) used to say the human "will be asked again next
        # session" / "asked again next session", in both the `undecided`
        # and `partial-wired` states. That is the same retired promise as
        # the README's old "You get asked once" -- nothing compels an
        # assistant to raise the SessionStart detector's advisory nudge, so
        # no document may promise a bare re-ask. Pinned absent from the
        # WHOLE file (not just step 3's prose, which the test above
        # covers), so a regression in either state's option text is caught
        # here even if TestSkillHonesty's exact-list pins above are ever
        # loosened.
        text = SKILL.read_text()
        self.assertNotIn("you will be asked again next session", text)
        self.assertNotIn("asked again next session", text)

    def test_setup_sh_no_longer_carries_the_retired_third_person_promises(self):
        # TOP-0110 L3, installer-closing-honesty branch, Grok gate finding
        # 1: memcontinuum-setup.sh holds the LAST thing a user reads after
        # installing (the "=== done ===" epilogue) and the --help header
        # (usage() prints the top comment block verbatim) -- the file this
        # whole episode's original sweep missed. TestNoRephrasedAskPromise
        # below now scans this file too, but by its own disclosed limits
        # cannot see either retired string here (verified by planting
        # both: the header's "will" and "ask you" sit four words apart,
        # past the guard's two-filler-word window; the epilogue was third
        # person throughout, no "you" recipient at all -- zero regex hits
        # either way). The cheap literal pin belongs BESIDE the semantic
        # guard, not instead of it -- same shape as the README/SKILL pins
        # above.
        text = SETUP_SH.read_text()
        self.assertNotIn(
            "will prompt the assistant to ask you", text,
            "the retired closing epilogue promised the assistant would "
            "ask you once -- see TOP-0110 L3",
        )
        self.assertNotIn(
            "the human is asked about", text,
            "the retired --help header described the memcontinuum skill "
            "this way -- see TOP-0110 L3",
        )

    def test_setup_sh_epilogue_names_memcontinuum_as_the_way_to_decide(self):
        # Positive companion to the pin above: the rewritten "=== done ==="
        # epilogue actually says what TOP-0110 L3 requires, not just that
        # the old text is gone.
        text = SETUP_SH.read_text()
        self.assertIn(
            "Run /memcontinuum inside a repository to decide for it.",
            text,
        )
        self.assertIn("advisory only, and it may never reach you.", text)

    def test_setup_sh_epilogue_does_not_overclaim_not_now_is_recorded(self):
        # TOP-0110 L3, Grok gate finding 3: the epilogue said "Whatever you
        # answer through the skill is recorded and reversible" -- FALSE for
        # "Not now" (skills/memcontinuum/SKILL.md's own "Not now" option
        # records nothing). Corrected to name the yes-or-no case
        # specifically and the "not now" carve-out, matching the README's
        # own fix for the identical overclaim.
        text = SETUP_SH.read_text()
        self.assertNotIn(
            "Whatever you answer through the skill is recorded and "
            "reversible",
            text,
        )
        self.assertIn(
            "A yes-or-no answer through the skill is recorded and "
            "reversible",
            text,
        )
        self.assertIn(
            "answering not now records nothing and leaves the repo "
            "undecided",
            text,
        )

    def test_setup_sh_help_header_states_the_detector_is_advisory_only(self):
        # Runs the real --help (usage() prints the top comment block
        # verbatim, via sed) rather than reading source lines directly --
        # the sentence spans three separate `#`-prefixed comment lines, so
        # this is checked against what a person actually sees.
        proc = subprocess.run(
            ["bash", str(SETUP_SH), "--help"],
            cwd=str(TOOLS_DIR), capture_output=True, text=True,
            env=dict(os.environ, PYTHONPATH=""),
        )
        self.assertEqual(
            proc.returncode, 0, f"--help must exit 0: {proc.stderr[:400]}",
        )
        normalized = " ".join(proc.stdout.split())
        self.assertIn(
            "the SessionStart detector flags an undecided repo to the "
            "assistant, advisory only",
            normalized,
        )
        self.assertIn(
            "/memcontinuum is the supported way to actually decide",
            normalized,
        )


# Apostrophe as either a straight quote or a curly one -- a rephrase is just
# as likely to introduce "you’ll" as "you'll", and nothing else in this
# codebase's prose currently uses the curly form (checked: zero hits in
# README.md / SKILL.md), so accepting both costs nothing today and closes a
# free mutation tomorrow.
_APOSTROPHE = r"[\'’]"

# TOP-0110 L3, Grok gate finding 2: the exact-phrase pins in
# TestConsentIsManualNotAutomatic catch verbatim restoration of a retired
# promise, but not a REPHRASED one -- the gate proved this by inserting
# "You will be asked once at session start whether this repo should keep a
# decision store." next to the README's new Day-to-day paragraph, and by
# rewording section 2's "Not now" option to "you'll be prompted next time
# you open this repo": both passed every exact-phrase pin (the second was
# only caught by TestSkillHonesty's exact option-list pin, a different
# guard for a different reason). These patterns catch the SHAPE of a
# promise instead of one fixed wording: a human named as the RECIPIENT of
# ask/prompt/remind/offer -- second person "you"/"you'll"/"you're", as
# either the subject of a passive ("you('ll) be asked", "you're prompted",
# "you get reminded") or the object of an active future/habitual verb
# ("will ask you", "reminds you") -- because the ruling this guards is
# precise: no user-facing text may promise a human will be asked, prompted,
# or reminded without acting themselves. A run of up to two filler words is
# allowed between the modal and the participle ("you'll always be asked",
# "will then remind you") so a hedge word doesn't buy an escape.
#
# Known, disclosed limit (not a claim of full semantic coverage): this is
# "you"-anchored by design, so a promise with no second-person recipient --
# a bare passive ("the repo will be asked again") or third person
# throughout -- is NOT caught here. That shape is still covered, when it
# matches, by the exact-phrase pins in TestConsentIsManualNotAutomatic and
# TestSkillHonesty's pinned option lists; a novel third-person rephrase of
# neither pinned string is caught by nothing here and needs a reviewer.
PROMISE_TO_BE_ASKED_PATTERNS = [
    # "you will (then) be asked" / "you'll (always) get prompted"
    re.compile(
        r"\byou(?:" + _APOSTROPHE + r"ll|\s+will)\s+(?:\w+\s+){0,2}"
        r"(?:be\s+|get\s+)?(?:asked|prompted|reminded|offered)\b",
        re.IGNORECASE,
    ),
    # "you are (always) asked" / "you're prompted" / "you get reminded" --
    # present-tense habitual passive, no "will"/"'ll" needed
    re.compile(
        r"\byou(?:" + _APOSTROPHE + r"re|\s+are|\s+get)\s+(?:\w+\s+){0,2}"
        r"(?:asked|prompted|reminded|offered)\b",
        re.IGNORECASE,
    ),
    # "will (then) ask you" / "'ll prompt you" -- future active, human as
    # the object
    re.compile(
        r"\b(?:will|" + _APOSTROPHE + r"ll)\s+(?:\w+\s+){0,2}"
        r"(?:ask|prompt|remind|offer)s?\s+you\b",
        re.IGNORECASE,
    ),
    # "asks you" / "prompts you" / "reminds you" / "offers you" -- bare
    # present-tense habitual active, human as the object
    re.compile(r"\b(?:asks|prompts|reminds|offers)\s+you\b", re.IGNORECASE),
]

# Small, explicit allowlist: exact substrings this scan would otherwise
# flag, kept out only because each names why it is accurate under the
# ruling. A hit is excused ONLY when the matched span sits entirely INSIDE
# one of these substrings' own span in the whitespace-normalized text --
# proximity is not enough, so a real violation typed next to an allowed
# phrase still fails.
#
# hooks/newfile-nudge.sh (added alongside the README/SKILL scan below,
# same ruling): checked directly, not assumed -- as of the newlang-nudge
# branch's own option-3 text ("stays unwired until decided", no "you" at
# all) and every other ask/prompt/remind/offer occurrence in the file
# (comments describing what the HOOK does, third person, e.g. "reminds
# the agent", "this tool asks"), the PROMISE_TO_BE_ASKED_PATTERNS scan
# produces ZERO hits against it -- nothing to allowlist today. Left empty
# rather than pre-populated with a guess; a real future hit gets a named,
# commented entry here, the same as any doc's.
#
# memcontinuum-setup.sh (TOP-0110 L3, installer-closing-honesty branch):
# this file holds the LAST thing a user reads after installing -- the one
# place this whole episode's sweep missed -- so it is added here too.
# Checked directly, not assumed: the rewritten "=== done ===" epilogue
# ("Run /memcontinuum inside a repository to decide for it ... it may
# never reach you. Whatever you answer through the skill is recorded and
# reversible.") never puts "you" next to ask/prompt/remind/offer at all,
# and the file's `-h`/`--help` header (usage() prints the top comment
# block verbatim) and every other say/comment line were checked the same
# way -- ZERO hits today, nothing to allowlist. Disclosed, not just
# assumed: the OLD epilogue this branch replaced ("...will prompt the
# assistant to ask you once whether...") also produced ZERO hits against
# this same scan -- "will" and "ask you" sit four words apart across "the
# assistant to", past the two-filler-word window PROMISE_TO_BE_ASKED_
# PATTERNS allows between a modal and its verb (TOP-0129 L2 documents the
# guard's other known gap, third-person phrasing with no "you" recipient
# at all; this is a second, distinct gap in the same shape-not-meaning
# design -- a real promise whose modal and verb are separated by an
# intervening clause). This scan would NOT have caught the defect this
# branch fixes had it run before the fix; only the exact-phrase pins this
# class's own docstring describes, or a reviewer, catch that shape.
#
# scripts/memcontinuum-decide.sh: its two runtime "asked" echoes
# (`declined` -> "this repo will not be asked again", `forget` -> "the
# SessionStart detector will emit its ask again") are third person
# throughout -- no "you" -- so, same as TOP-0129 L2 describes, this scan
# does not and cannot evaluate them; checked directly and confirmed ZERO
# regex hits today regardless, nothing to allowlist.
#
# scripts/memcontinuum-update.sh (Grok gate finding 2, installer-closing-
# honesty branch): its --help header carried the same third-person shape
# as setup.sh's retired header ("...is the memcontinuum skill's repair
# path (a human is asked), not this command's"), fixed alongside setup.sh
# to name `/memcontinuum` as the way the install gets finished rather than
# an event that happens on its own, and added to this scan since it was
# never on any scan before. That fixed sentence, and the rest of the
# file's say/comment lines, produce ZERO regex hits today -- checked
# directly, not assumed. A second occurrence of the identical retired
# words sits at line ~1223, but as a code comment past the file's own
# `--MC-USAGE-END--` marker (confirmed: usage() only ever prints lines
# 2 through that marker) it is never printed by `--help` or seen by a
# user, so it is left alone on purpose, same as this doctrine's own
# top-of-file rule that code comments are not in scope.
ALLOWED_ASK_PROMISE_SUBSTRINGS = [
    # README's "Day to day": present tense, describing what /memcontinuum
    # itself does WHILE the human is running it. A human asked BY the
    # skill they just invoked is the one true "asks you" this system has
    # (TOP-0110 L3) -- not an unprompted claim about some future session.
    "asks you the one question if there is one to ask",
]


def _ask_promise_offenders(path):
    """Every PROMISE_TO_BE_ASKED_PATTERNS hit in `path`, whitespace-
    normalized first (markdown hard-wraps prose, so a promise like "you
    will be asked" can legitimately carry a newline+indent between its own
    words) and not covered by ALLOWED_ASK_PROMISE_SUBSTRINGS."""
    normalized = " ".join(path.read_text().split())
    allowed_spans = []
    for phrase in ALLOWED_ASK_PROMISE_SUBSTRINGS:
        start = 0
        while True:
            idx = normalized.find(phrase, start)
            if idx == -1:
                break
            allowed_spans.append((idx, idx + len(phrase)))
            start = idx + 1
    offenders = []
    for pattern in PROMISE_TO_BE_ASKED_PATTERNS:
        for m in pattern.finditer(normalized):
            if any(a_start <= m.start() and m.end() <= a_end
                   for a_start, a_end in allowed_spans):
                continue
            snippet = normalized[max(0, m.start() - 30):m.end() + 30]
            offenders.append(f"{path.name}: ...{snippet}...")
    return offenders


class TestNoRephrasedAskPromise(unittest.TestCase):
    """Semantic companion to TestConsentIsManualNotAutomatic's exact-phrase
    pins -- see PROMISE_TO_BE_ASKED_PATTERNS above for what this catches
    and its disclosed limit. Scans README.md, skills/memcontinuum/
    SKILL.md, hooks/newfile-nudge.sh, memcontinuum-setup.sh,
    scripts/memcontinuum-decide.sh, and scripts/memcontinuum-update.sh --
    the docs, the one hook, and the three installer scripts this episode's
    fix touched (TOP-0110 L3): the
    README describes the product to the human who reads it, the skill's
    own pinned structured-prompt
    options are the single most user-visible place in the product, a
    numbered choice the human sees live, newfile-nudge.sh's own
    three-option decision point (newlang-nudge) used to make the
    identical promise in its option 3 ("asked again next session") before
    that branch retired it, and memcontinuum-setup.sh holds the LAST
    thing a user reads after installing -- the "=== done ===" epilogue --
    which is exactly what this whole episode's original sweep missed:
    every other document got the fix, this file did not get scanned at
    all. scripts/memcontinuum-decide.sh's runtime text was corrected on
    the consent branch but was likewise never added to any scan, so it
    is added here alongside setup.sh even though (see
    ALLOWED_ASK_PROMISE_SUBSTRINGS above) its two "asked" echoes are
    third person and this class cannot evaluate them -- TOP-0129 L2's
    disclosed limit, not a gap fixed here.
    scripts/memcontinuum-update.sh (Grok gate finding 2,
    installer-closing-honesty branch) carried the identical third-person
    shape in its own --help header and was never on any scan either; its
    fixed header is added here too, same treatment as the other five. All
    six are shell or prose
    with user-facing text (option lists, say/echo lines, comments a
    `--help` prints) -- a hit here is expected to need
    ALLOWED_ASK_PROMISE_SUBSTRINGS entries over time the way README/SKILL
    already do; checked directly (not assumed) that today's six files
    produce none. Reads the bare module globals README/SKILL/
    NEWFILE_NUDGE_HOOK/SETUP_SH/DECIDE_SH/UPDATE_SH at call time (not a
    pre-bound
    list), same as TestSkillHonesty's methods, so
    TestNoRephrasedAskPromiseMutations below can point any of them at a
    mutated temp file and prove this method reacts to it for real.

    Disclosed, not just claimed: this scan is shape-anchored on "you" as
    the recipient (TOP-0129 L2), so it does NOT cover a third-person
    promise, and separately (found while extending it to setup.sh) it
    does not reliably cover a "you"-recipient promise either when an
    intervening clause pushes the modal ("will") and the ask/prompt/
    remind/offer verb more than two words apart -- memcontinuum-setup.sh's
    OWN retired closing text ("...will prompt the assistant to ask you
    once whether...") is a real example that produced zero hits against
    this exact scan. Neither gap is fixed by this change; both are why
    the exact-phrase pins elsewhere, and a reviewer's eye, still matter
    alongside this class."""

    def test_no_user_facing_text_promises_you_will_be_asked(self):
        offenders = []
        for doc in (
            README, SKILL, NEWFILE_NUDGE_HOOK, SETUP_SH, DECIDE_SH,
            UPDATE_SH,
        ):
            offenders.extend(_ask_promise_offenders(doc))
        self.assertEqual(
            offenders, [],
            "user-facing text promises a human will be asked/prompted/"
            "reminded/offered (TOP-0110 L3): the SessionStart detector's "
            "ask reaches only the assistant, through additionalContext, "
            "never the human directly, so nothing here may promise "
            f"otherwise: {offenders}",
        )


class TestNoRephrasedAskPromiseMutations(unittest.TestCase):
    """Proves TestNoRephrasedAskPromise actually catches what it claims to
    -- against the REAL production test method, not a second hand-written
    copy of its logic (the same house rule fix round 3 established for
    TestSkillHonestyMutations above). Reproduces Grok gate finding 2's two
    exact defeats verbatim (both slipped past every exact-phrase pin in
    TestConsentIsManualNotAutomatic when the gate first found them), a
    third for newfile-nudge.sh's own option 3 (newlang-nudge, TOP-0110
    L3): rephrasing "nothing is recorded; stays unwired until decided"
    back into the same retired promise ("you'll be asked again next
    session") must fail the suite too, not just the exact-string absence
    check in tests/test_write_hooks.py. A fourth and fifth do the same
    for the two files added to the scan on the installer-closing-honesty
    branch (TOP-0110 L3): memcontinuum-setup.sh (rephrasing its own
    closing epilogue, which this class pins nowhere else) and
    scripts/memcontinuum-decide.sh (an inserted "you"-shaped promise,
    since that file's own real "asked"/"ask" text is third person and
    outside what TestNoRephrasedAskPromise can evaluate -- TOP-0129 L2 --
    so a rephrase-of-existing-text defeat is not available there; this
    proves the scan still reaches the file rather than silently skipping
    it). A sixth does the same for scripts/memcontinuum-update.sh (added
    to the scan for the same gate's finding 2): an inserted "you"-shaped
    promise, for the identical reason as decide.sh -- update.sh's own
    real "asked" text (its --help header, fixed alongside setup.sh's, and
    the untouched code comment past --MC-USAGE-END-- at line ~1223) is
    third person throughout, outside what this class can evaluate."""

    @staticmethod
    @contextlib.contextmanager
    def _mutated_doc(varname, mutated_text):
        """Point the module-level global named `varname` ("README",
        "SKILL", "NEWFILE_NUDGE_HOOK", "SETUP_SH", "DECIDE_SH", or
        "UPDATE_SH") at a
        temp file holding `mutated_text` for the duration of the `with`
        block, then restore it -- generalizes
        TestSkillHonestyMutations._mutated_skill (which only ever swaps
        SKILL) so this class can reproduce a defeat planted in any doc,
        hook, or script TestNoRephrasedAskPromise scans; that test's
        method resolves README/SKILL/NEWFILE_NUDGE_HOOK/SETUP_SH/
        DECIDE_SH/UPDATE_SH from this module's globals at call time, so
        this reaches it."""
        module = sys.modules[__name__]
        real_path = getattr(module, varname)
        fd, tmp_name = tempfile.mkstemp(suffix=".md")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(mutated_text)
            setattr(module, varname, tmp_path)
            yield
        finally:
            setattr(module, varname, real_path)
            tmp_path.unlink(missing_ok=True)

    def _assert_guard_catches(self, varname, mutated_text):
        with self._mutated_doc(varname, mutated_text):
            test = TestNoRephrasedAskPromise(
                "test_no_user_facing_text_promises_you_will_be_asked"
            )
            with self.assertRaises(
                AssertionError,
                msg="TestNoRephrasedAskPromise did not fail against the "
                    "mutation",
            ):
                test.test_no_user_facing_text_promises_you_will_be_asked()

    def test_readme_insertion_defeat_is_caught(self):
        # Grok gate finding 2, defeat 1: inserted next to the README's new
        # Day-to-day paragraph.
        text = README.read_text()
        marker = "**`/memcontinuum` any time.**"
        self.assertIn(marker, text, "fixture stale: marker not found")
        mutated = text.replace(
            marker,
            "You will be asked once at session start whether this repo "
            "should keep a decision store.\n\n" + marker,
            1,
        )
        self.assertNotEqual(mutated, text, "fixture stale: nothing matched")
        self._assert_guard_catches("README", mutated)

    def test_skill_section_2_rephrase_defeat_is_caught(self):
        # Grok gate finding 2, defeat 2: section 2's "Not now" option
        # reworded to keep the same false promise in different words.
        original = (
            "4. Not now — nothing is recorded; run `/memcontinuum` again "
            "to decide"
        )
        text = SKILL.read_text()
        self.assertIn(original, text, "fixture stale: option text not found")
        mutated = text.replace(
            original,
            "4. Not now — you'll be prompted next time you open this repo",
            1,
        )
        self.assertNotEqual(mutated, text, "fixture stale: nothing matched")
        self._assert_guard_catches("SKILL", mutated)

    def test_newfile_nudge_hook_option_three_rephrase_defeat_is_caught(self):
        # TOP-0110 L3: newfile-nudge.sh's own option 3 reworded back into
        # the identical retired promise this branch just fixed it away
        # from ("Piece 2" of this same pass) -- the guard extended to
        # this file in "Piece 3" must catch it, not just the exact-string
        # absence assertions pinned in tests/test_write_hooks.py.
        original = "3. Not now — nothing is recorded; stays unwired until decided"
        text = NEWFILE_NUDGE_HOOK.read_text()
        self.assertIn(original, text, "fixture stale: option text not found")
        mutated = text.replace(
            original,
            "3. Not now — you'll be asked again next session",
            1,
        )
        self.assertNotEqual(mutated, text, "fixture stale: nothing matched")
        self._assert_guard_catches("NEWFILE_NUDGE_HOOK", mutated)

    def test_setup_sh_closing_rephrase_defeat_is_caught(self):
        # TOP-0110 L3, installer-closing-honesty branch: memcontinuum-
        # setup.sh's own "=== done ===" epilogue is the file this whole
        # episode's original sweep missed -- the one that holds the LAST
        # thing a user reads after installing. Rewording the fixed
        # closing text back into a close rephrase of the retired promise
        # must fail the suite through this file, not just be absent from
        # a pin (this class pins setup.sh's exact wording nowhere else).
        marker = (
            "answering not now records nothing and leaves the repo "
            "undecided."
        )
        text = SETUP_SH.read_text()
        self.assertIn(marker, text, "fixture stale: closing text not found")
        mutated = text.replace(
            marker,
            "You'll be asked again next session if you don't decide now.",
            1,
        )
        self.assertNotEqual(mutated, text, "fixture stale: nothing matched")
        self._assert_guard_catches("SETUP_SH", mutated)

    def test_decide_sh_insertion_defeat_is_caught(self):
        # scripts/memcontinuum-decide.sh's own real "asked"/"ask" text
        # ("this repo will not be asked again", "will emit its ask
        # again") is third person throughout, with no "you" recipient --
        # outside what TestNoRephrasedAskPromise can evaluate (TOP-0129
        # L2, see the class docstring above), so no rephrase-of-existing-
        # text defeat exists here the way it does for the other four
        # files. This proves the scan still actually REACHES the file
        # (added to the loop, not silently skipped) by inserting a
        # "you"-shaped promise the pattern IS designed to catch.
        text = DECIDE_SH.read_text()
        marker = "set -u"
        self.assertIn(marker, text, "fixture stale: marker not found")
        mutated = text.replace(
            marker, marker + "\n# you will be asked again next session\n", 1
        )
        self.assertNotEqual(mutated, text, "fixture stale: nothing matched")
        self._assert_guard_catches("DECIDE_SH", mutated)

    def test_update_sh_insertion_defeat_is_caught(self):
        # scripts/memcontinuum-update.sh (Grok gate finding 2,
        # installer-closing-honesty branch): its --help header, like
        # decide.sh's runtime text, is third person throughout ("...is
        # the memcontinuum skill's repair path...", no "you" recipient)
        # even after the fix -- outside what TestNoRephrasedAskPromise
        # can evaluate, TOP-0129 L2 -- so, same as decide.sh above, no
        # rephrase-of-existing-text defeat exists here. This proves the
        # scan still actually REACHES the file (added to the loop, not
        # silently skipped) by inserting a "you"-shaped promise the
        # pattern IS designed to catch.
        text = UPDATE_SH.read_text()
        marker = "set -u"
        self.assertIn(marker, text, "fixture stale: marker not found")
        mutated = text.replace(
            marker, marker + "\n# you will be asked again next session\n", 1
        )
        self.assertNotEqual(mutated, text, "fixture stale: nothing matched")
        self._assert_guard_catches("UPDATE_SH", mutated)


if __name__ == "__main__":
    unittest.main()
