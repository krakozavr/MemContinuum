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

PYPROJECT = TOOLS_DIR / "pyproject.toml"
CHANGELOG = TOOLS_DIR / "CHANGELOG.md"
WORKFLOW = TOOLS_DIR / ".github" / "workflows" / "tests.yml"
LOCKFILE = TOOLS_DIR / "requirements.lock"
REQUIREMENTS = TOOLS_DIR / "requirements.txt"
RUN_BASH32 = TOOLS_DIR / "tests" / "run_bash32.sh"


class TestReleaseHygiene(unittest.TestCase):
    def test_pyproject_version_is_0_2_0rc1(self):
        text = PYPROJECT.read_text()
        self.assertIn('version = "0.2.0rc1"', text)

    def test_changelog_exists_and_mentions_the_version(self):
        self.assertTrue(CHANGELOG.exists())
        self.assertIn("0.2.0rc1", CHANGELOG.read_text())

    def test_ci_workflow_exists_and_runs_the_suite_with_pythonpath_cleared(self):
        self.assertTrue(WORKFLOW.exists())
        text = WORKFLOW.read_text()
        self.assertIn("PYTHONPATH=", text)
        self.assertIn("unittest discover -s tests", text)

    def test_ci_workflow_runs_bash32_harness(self):
        text = WORKFLOW.read_text()
        self.assertIn("run_bash32.sh", text)

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
