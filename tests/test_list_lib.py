"""scripts/mc-list-lib.sh: the one raw ';' join, and the two callers that
share it (memcontinuum-update.sh joins raw; memcontinuum-decide.sh encodes
each element once, then joins). Every bash call goes through MC_BASH, so
tests/run_bash32.sh re-runs this under a real bash 3.2."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

from tests.test_update import (  # noqa: E402
    DECIDE_SH, INSTALL_SH, VENV_PYTHON, _SKIP_NO_VENV, decisions_tsv, git_repo, run, tmpdir,
)

MC_BASH = os.environ.get("MC_BASH", "bash")
LIST_LIB = TOOLS_DIR / "scripts" / "mc-list-lib.sh"


def join(*args):
    """Source the library and print mc_join_semi ARGS, NUL-safe via od-free
    capture: returns the exact stdout."""
    proc = subprocess.run(
        [MC_BASH, "-c", '. "$1"; shift; mc_join_semi "$@"', "x", str(LIST_LIB), *args],
        capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class TestMcJoinSemi(unittest.TestCase):
    def test_zero_arguments_print_nothing(self):
        self.assertEqual(join(), "")

    def test_single_element(self):
        self.assertEqual(join("a"), "a")

    def test_several_elements(self):
        self.assertEqual(join("a", "b", "c"), "a;b;c")

    def test_empty_elements_are_kept(self):
        self.assertEqual(join("", ""), ";")
        self.assertEqual(join("a", "", "b"), "a;;b")
        self.assertEqual(join(""), "")

    def test_spaces_kept_raw(self):
        self.assertEqual(join("a b", " c "), "a b; c ")

    def test_literal_percent_and_format_directives_kept_raw(self):
        self.assertEqual(join("100%", "%s", "%d%%", "\\n"), "100%;%s;%d%%;\\n")

    def test_no_trailing_newline(self):
        self.assertFalse(join("a", "b").endswith("\n"))

    def test_sourcing_prints_and_defines_nothing_but_the_function(self):
        proc = subprocess.run(
            [MC_BASH, "-c", 'set -u; . "$1"; declare -F | wc -l', "x", str(LIST_LIB)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "1")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestDecideEncodesOnce(unittest.TestCase):
    def setUp(self):
        self.tmp = tmpdir(self, "memcontinuum-list-lib-test-")
        self.home = str(Path(self.tmp) / "home")
        os.makedirs(self.home, exist_ok=True)

    def test_each_element_is_percent_encoded_exactly_once(self):
        repo = git_repo(str(Path(self.tmp) / "repo"))
        claude = str(Path(repo) / ".claude")
        store = str(Path(self.tmp) / "store")
        proc = run(INSTALL_SH, ["--project", "p", "--store", store, "--claude-dir", claude,
                                "--non-interactive"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = run(DECIDE_SH, ["wired", "--repo", repo, "--store", store, "--project", "p",
                               "--claude-dir", claude,
                               "--code-root", "/a b", "--code-root", "/c%d"], self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        note = decisions_tsv(self.home).read_text().splitlines()[-1]
        # "/a b" -> "/a%20b", "/c%d" -> "/c%25d"; a second encoding pass over
        # the joined string would have produced "%2520" / "%2525".
        self.assertIn("code-roots=/a%20b;/c%25d", note)


if __name__ == "__main__":
    unittest.main()
