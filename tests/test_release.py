"""Release hygiene checks (task-9): CI workflow, dependency lockfile, version
bump, and CHANGELOG.md. These are process-artifact assertions, not behavioral
tests -- the interface is the presence and content of a file, not a
function's return value.
"""
import re
import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import chunkers  # noqa: E402 -- LANGUAGE_TABLE is the source the pin checks derive from

PYPROJECT = TOOLS_DIR / "pyproject.toml"
CHANGELOG = TOOLS_DIR / "CHANGELOG.md"
WORKFLOW = TOOLS_DIR / ".github" / "workflows" / "tests.yml"
LOCKFILE = TOOLS_DIR / "requirements.lock"
REQUIREMENTS = TOOLS_DIR / "requirements.txt"
RUN_BASH32 = TOOLS_DIR / "tests" / "run_bash32.sh"
README = TOOLS_DIR / "README.md"


class TestReleaseHygiene(unittest.TestCase):
    def test_pyproject_version_is_0_2_0rc3(self):
        text = PYPROJECT.read_text()
        self.assertIn('version = "0.2.0rc3"', text)

    def test_changelog_exists_and_mentions_the_version(self):
        self.assertTrue(CHANGELOG.exists())
        self.assertIn("0.2.0rc3", CHANGELOG.read_text())

    def test_pyproject_python_floor_is_3_12(self):
        text = PYPROJECT.read_text()
        self.assertIn('requires-python = ">=3.12"', text)

    def test_readme_requirement_line_agrees_with_pyproject_floor(self):
        match = re.search(r'requires-python = ">=(\d+\.\d+)"', PYPROJECT.read_text())
        self.assertIsNotNone(match, "pyproject.toml has no requires-python floor to compare against")
        floor = match.group(1)
        readme_text = README.read_text()
        self.assertIn(
            f"Python {floor}+", readme_text,
            f"README Requirements section does not state the same Python {floor}+ floor as pyproject.toml",
        )

    def test_ci_workflow_exists_and_runs_the_suite_with_pythonpath_cleared(self):
        self.assertTrue(WORKFLOW.exists())
        text = WORKFLOW.read_text()
        self.assertIn("PYTHONPATH=", text)
        self.assertIn("unittest discover -s tests", text)

    def test_ci_workflow_runs_bash32_harness(self):
        text = WORKFLOW.read_text()
        self.assertIn("run_bash32.sh", text)

    def test_ci_workflow_runs_a_python_version_matrix(self):
        text = WORKFLOW.read_text()
        self.assertIn('"3.12"', text)
        self.assertIn('"3.13"', text)
        self.assertIn("matrix:", text)

    def test_ci_workflow_has_a_macos_job(self):
        text = WORKFLOW.read_text()
        self.assertIn("macos-latest", text)
        # The macOS job re-runs the shell-driving suites under a real
        # bash 3.2.57 (MC_BASH32 points run_bash32.sh at it directly),
        # not a from-source build like the Ubuntu bash32 job.
        self.assertIn("MC_BASH32=/bin/bash", text)

    def test_lockfile_exists(self):
        self.assertTrue(LOCKFILE.exists())

    def test_lockfile_pins_every_requirements_txt_package_with_exact_equals(self):
        # requirements.txt uses >= (documented deliberately -- see its own
        # header comment); the lock must still pin every one of those
        # top-level packages to an exact version with ==, or "lockfile"
        # is just a second copy of the loose file.
        names = re.findall(r"^([A-Za-z0-9_.-]+)\s*>=", REQUIREMENTS.read_text(), re.M)
        self.assertTrue(names, "requirements.txt parsed no package names -- check the regex/file")
        lock_text = LOCKFILE.read_text()
        for name in names:
            pattern = re.compile(
                r"^%s==\S+" % re.escape(name), re.M | re.I
            )
            self.assertRegex(
                lock_text, pattern,
                f"{name} from requirements.txt has no exact-pinned '{name}==...' line in requirements.lock",
            )

    def test_lockfile_carries_no_machine_identifying_path(self):
        # uv pip compile's default header embeds the absolute --python path
        # it was run with (e.g. /home/<user>/.../bin/python) -- a real leak
        # of this dev machine's layout and username into a tracked file.
        # tests/test_repo_init.py's TestNoMachineIdentifyingContent has the
        # broader repo-wide version of this check (inline token list, no
        # shared constant to import); this is the lockfile-specific,
        # faster-signal counterpart.
        text = LOCKFILE.read_text()
        username_needle = "kra" + "kozavr"
        for needle in ("/home/", "/mnt/", "/Users/", username_needle):
            self.assertNotIn(
                needle, text,
                f"requirements.lock contains machine-identifying text {needle!r} "
                "-- regenerate with `uv pip compile --no-header` or "
                "--custom-compile-command so the header carries no absolute path",
            )

    def test_run_bash32_updates_apt_cache_before_downloading(self):
        text = RUN_BASH32.read_text()
        idx_update = text.find("apt-get update")
        idx_download = text.find("apt-get download")
        self.assertNotEqual(idx_update, -1, "run_bash32.sh must apt-get update before apt-get download on a bare CI runner")
        self.assertLess(idx_update, idx_download)

    def test_ci_workflow_uses_the_same_interpreter_for_install_and_tests(self):
        # A hardcoded MEMCONTINUUM_PYTHON path can silently diverge from
        # whatever actions/setup-python actually put first on PATH for the
        # `pip install` step -- dependencies land on one interpreter, tests
        # gate on and run under another, and MEMCONTINUUM_PYTHON being
        # non-empty makes the subprocess-hook tests in tests/test_hooks.py
        # actually RUN against it (they only skip when it's empty). Assert
        # the fix instead of the symptom: no hardcoded interpreter path, and
        # the same-shell-step resolution that guarantees a match.
        text = WORKFLOW.read_text()
        self.assertNotIn("/usr/bin/python3", text)
        self.assertIn("command -v python", text)


def tree_sitter_pins():
    """The pinned distribution -> version map, DERIVED from
    `chunkers.LANGUAGE_TABLE` rather than copied beside it.

    Whole-branch review, finding 7: this used to be a third hand-maintained
    copy of the seven pins, checked against requirements.txt and
    requirements.lock and never against the table. The table's own
    `grammar_pin`/`runtime_pin` are what `chunker_version` hashes -- the
    fingerprint deliberately never introspects the installed package -- so
    bumping a grammar in the two requirements files while leaving the row
    alone left `chunker_version` unchanged, and a new grammar version
    served chunks produced by the old one. That is precisely the failure
    the exact pins exist to prevent, and the suite stayed green either way.
    Deriving the expectation ties all three together with one assertion.

    A distribution name is the grammar module's name with underscores
    turned into dashes (`tree_sitter_javascript` ->
    `tree-sitter-javascript`), which is how every one of these is published.
    `typescript` and `tsx` share a grammar module and therefore collapse to
    one entry."""
    pins = {}
    for row in chunkers.LANGUAGE_TABLE.values():
        if row["backend"] != "tree-sitter":
            continue
        pins["tree-sitter"] = row["runtime_pin"]
        pins[row["grammar_module"].replace("_", "-")] = row["grammar_pin"]
    return pins


class TestTreeSitterPins(unittest.TestCase):
    def test_the_table_is_the_only_place_a_pin_is_authored(self):
        pins = tree_sitter_pins()
        self.assertTrue(pins, "no tree-sitter row in LANGUAGE_TABLE to derive pins from")
        # One runtime for the whole tier: every row names the same
        # runtime_pin, so one "tree-sitter" entry can stand for all of them.
        runtimes = {row["runtime_pin"] for row in chunkers.LANGUAGE_TABLE.values()
                    if row["backend"] == "tree-sitter"}
        self.assertEqual(len(runtimes), 1, f"tree-sitter rows disagree on runtime_pin: {runtimes}")
        # Two rows sharing a grammar module must agree on its version --
        # otherwise one dist name maps to two pins and nothing can satisfy both.
        by_module = {}
        for lang, row in chunkers.LANGUAGE_TABLE.items():
            if row["backend"] != "tree-sitter":
                continue
            by_module.setdefault(row["grammar_module"], set()).add(row["grammar_pin"])
        for module, versions in by_module.items():
            self.assertEqual(len(versions), 1, f"{module} is pinned to {versions} by different rows")

    def test_requirements_txt_has_an_exact_pin_block_naming_the_policy_exception(self):
        text = REQUIREMENTS.read_text()
        self.assertIn("exact", text.lower())
        self.assertIn("pin", text.lower())
        for name, version in tree_sitter_pins().items():
            self.assertIn(
                f"{name}=={version}", text,
                f"requirements.txt is missing the exact pin {name}=={version} "
                f"that chunkers.LANGUAGE_TABLE declares",
            )

    def test_lockfile_pins_match_the_table_exactly(self):
        lock_text = LOCKFILE.read_text()
        for name, version in tree_sitter_pins().items():
            pattern = re.compile(r"^%s==%s\b" % (re.escape(name), re.escape(version)), re.M | re.I)
            self.assertRegex(
                lock_text, pattern,
                f"requirements.lock does not pin {name} to {version} "
                f"(chunkers.LANGUAGE_TABLE is the source; requirements.txt must agree too)",
            )

    def test_requirements_txt_declares_no_tree_sitter_pin_the_table_does_not(self):
        # The other direction: a pin added to requirements.txt for a row
        # that does not exist (or at a version the row does not name) is the
        # same drift seen from the other side.
        pins = tree_sitter_pins()
        for line in REQUIREMENTS.read_text().splitlines():
            line = line.strip()
            if not line.startswith("tree-sitter"):
                continue
            name, _, version = line.partition("==")
            self.assertIn(name, pins, f"{name} is pinned in requirements.txt but no row declares it")
            self.assertEqual(
                version, pins[name],
                f"requirements.txt pins {name} to {version}, LANGUAGE_TABLE says {pins[name]}",
            )

    def test_pyproject_agrees_with_the_table_on_any_tree_sitter_dependency(self):
        # pyproject.toml declares no tree-sitter dependency today (the
        # grammars are installed from requirements.txt/.lock, not as
        # metadata dependencies), so this holds vacuously -- and stops
        # holding the moment someone adds one at a version the table does
        # not name.
        pins = tree_sitter_pins()
        for match in re.finditer(r'"(tree-sitter[a-z0-9-]*)\s*==\s*([^"]+)"', PYPROJECT.read_text()):
            name, version = match.group(1), match.group(2).strip()
            self.assertIn(name, pins, f"{name} is a pyproject dependency but no row declares it")
            self.assertEqual(version, pins[name],
                             f"pyproject pins {name} to {version}, LANGUAGE_TABLE says {pins[name]}")


class TestChangelogAddedToDoctrineScan(unittest.TestCase):
    def test_changelog_is_scanned_by_the_doctrine_machinery(self):
        from test_docs import PUBLIC_DOCS  # noqa: E402

        names = {str(p) for p in PUBLIC_DOCS}
        self.assertTrue(
            any("CHANGELOG.md" in n for n in names),
            "CHANGELOG.md missing from PUBLIC_DOCS -- task 9's own drafted "
            "changelog text must be caught by the same doctrine scan every "
            "other public doc gets",
        )


if __name__ == "__main__":
    unittest.main()
