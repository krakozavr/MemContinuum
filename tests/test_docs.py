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
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
README = TOOLS_DIR / "README.md"
INTERNALS = TOOLS_DIR / "docs" / "INTERNALS.md"
DESIGN = TOOLS_DIR / "docs" / "DESIGN.md"
SKILL = TOOLS_DIR / "skills" / "memcontinuum" / "SKILL.md"
SEARCH_SKILL = TOOLS_DIR / "skills" / "memory-search" / "SKILL.md"
INSTALL_HOOKS = TOOLS_DIR / "hooks" / "install-hooks.md"

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
        "reindex", "search", "chain", "for-path", "check", "why", "drift",
        "unmapped", "code-reindex", "code-search", "code-census", "stats",
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
]

PUBLIC_DOCS = [
    README, INTERNALS, DESIGN, SKILL, SEARCH_SKILL, INSTALL_HOOKS,
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


if __name__ == "__main__":
    unittest.main()
