"""Tests for hooks/pre-edit-chain.sh (the PreToolUse retrieval-surface hook).

These exercise the real shell script via subprocess -- not a reimplementation
of its logic -- because the thing under test is the script's environment
handling (the PYTHONPATH trap, env-driven project/root resolution, fail-open
behavior) as much as its output shape.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402

# This machine's venv python is never hardcoded in tracked test code -- set
# $MEMCONTINUUM_PYTHON in your own (untracked) shell environment before
# running this file; see README.md "Requirements" / "Running the tests".
VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
_SKIP_NO_VENV = (
    "set $MEMCONTINUUM_PYTHON to a venv python with fastembed/PyYAML "
    "installed to run these tests (see README.md)"
)
HOOK_SCRIPT = TOOLS_DIR / "hooks" / "pre-edit-chain.sh"
SKILL_FILE = TOOLS_DIR / "skills" / "memory-search" / "SKILL.md"
SCHEMA_FIXTURE_ROOT = TOOLS_DIR / "fixtures" / "schema"
# A decoy PYTHONPATH entry ahead of the synthetic poison dir below, standing
# in for whatever unrelated site-packages tree a real shell's PYTHONPATH
# might export. It does not need to exist on disk -- Python silently skips
# a missing PYTHONPATH entry -- it just needs to have no `yaml` module in it,
# so the synthetic poison dir (which does) is what actually gets imported.
POISONED_SITE_PACKAGES = os.environ.get(
    "MEMCONTINUUM_TEST_DECOY_SITE_PACKAGES", "/nonexistent/decoy-site-packages"
)

CITATION_REMINDER = (
    "CONSTRAINT only if authority is owner-verbatim/owner-ratified and status "
    "active; HOLD for evidence-bearing incidents; everything else is context."
)


def clean_env(**overrides):
    """A hook-invocation environment: PYTHONPATH deliberately left unset unless
    a test explicitly pollutes it -- the hook itself is responsible for the
    hard-clear, not the caller."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(overrides)
    return env


def run_hook(payload_text: str, env: dict, timeout: float = 5.0):
    start = time.monotonic()
    proc = subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=payload_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    elapsed = time.monotonic() - start
    return proc, elapsed


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreEditChainHook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memcontinuum-hook-test-")
        cls.memtool_home = str(Path(cls.tmp) / "memcontinuum-home")
        os.makedirs(cls.memtool_home, exist_ok=True)
        cls.project = "hooktest"
        # Build the index once for the whole class: fixtures/schema indexed
        # under a fixed project name, --no-embed since the hook path (for-path)
        # never touches embeddings anyway.
        args = type(
            "Args",
            (),
            dict(
                root=str(SCHEMA_FIXTURE_ROOT),
                project=cls.project,
                db=str(Path(cls.memtool_home) / f"{cls.project}.sqlite"),
                full=True,
                no_embed=True,
            ),
        )()
        memidx.cmd_reindex(args)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_bash_syntax_is_valid(self):
        result = subprocess.run(["bash", "-n", str(HOOK_SCRIPT)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_matching_path_emits_chain_with_citation_reminder(self):
        """file_path is absolute, under a repo root that isn't the code_ref's
        own relative prefix -- forces the MEMCONTINUUM_STRIP_PREFIX fallback path,
        not a lucky exact/cwd match, so this proves the real production shape
        (a real PreToolUse payload always carries an absolute file_path)."""
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "cwd": "/some/other/unrelated/dir",
                "tool_input": {
                    "file_path": "/fake/repo/src/core/scan/scan_plan.py"
                },
            }
        )
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
        )
        proc, elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.0, f"hook took {elapsed:.3f}s")
        self.assertTrue(proc.stdout.strip(), "expected additionalContext output, got nothing")

        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PreToolUse")

        # newest-first chain lines: L4 must appear before L3, L3 before L2, L2 before L1
        self.assertIn("TOP-0042", ctx)
        pos_l4 = ctx.index("L4 ")
        pos_l3 = ctx.index("L3 ")
        pos_l2 = ctx.index("L2 ")
        pos_l1 = ctx.index("L1 ")
        self.assertTrue(pos_l4 < pos_l3 < pos_l2 < pos_l1, ctx)

        self.assertIn(CITATION_REMINDER, ctx)

    def test_b_non_matching_path_emits_nothing(self):
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "cwd": "/nowhere",
                "tool_input": {"file_path": "/nowhere/near/anything.py"},
            }
        )
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        proc, elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_c_malformed_payload_emits_nothing_and_logs(self):
        log_path = Path(self.memtool_home) / "hook.log"
        before = log_path.read_text() if log_path.exists() else ""

        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        proc, elapsed = run_hook("{ this is not json ]]]", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

        after = log_path.read_text() if log_path.exists() else ""
        self.assertTrue(log_path.exists(), "hook.log was never created")
        self.assertGreater(len(after), len(before), "no log line was appended for the failure")

    def test_d_completes_under_one_second_through_real_script_with_poisoned_pythonpath(self):
        """Exercises the actual PYTHONPATH trap named in docs/DESIGN.md SS8: a
        polluted PYTHONPATH is present in the environment the hook is spawned
        in (as .bashrc would export it), and the hook script itself -- not
        the test -- is responsible for hard-clearing it before calling the
        venv python.

        The decoy PYTHONPATH entry ahead of it has no `yaml` module, so a
        bare `PYTHONPATH=<that dir>` would pass even with the hook's own
        `export PYTHONPATH=` deleted -- that would be decoration, not a test
        of the trap. A synthetic poison directory ships a `yaml.py` that
        raises on import; memidx.py imports yaml unconditionally at module
        level, so this makes the hard-clear load-bearing: delete it and the
        venv python dies on the poisoned yaml, this test goes red. See
        test_control_unclearing_pythonpath_breaks_the_hook below, which
        proves exactly that against a temp copy of the script.
        """
        payload = self._matching_payload()
        env = self._poisoned_env()
        proc, elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.0, f"hook took {elapsed:.3f}s with a poisoned PYTHONPATH")
        self.assertTrue(proc.stdout.strip())
        out = json.loads(proc.stdout)
        self.assertIn("TOP-0042", out["hookSpecificOutput"]["additionalContext"])

    def _matching_payload(self):
        return json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Write",
                "cwd": "/some/other/unrelated/dir",
                "tool_input": {
                    "file_path": "/fake/repo/src/core/scan/scan_plan.py"
                },
            }
        )

    def _poisoned_env(self, **overrides):
        poison_dir = Path(self.tmp) / "poison-site-packages"
        poison_dir.mkdir(exist_ok=True)
        (poison_dir / "yaml.py").write_text(
            'raise RuntimeError("poisoned PYTHONPATH not cleared")\n'
        )
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
            PYTHONPATH=f"{POISONED_SITE_PACKAGES}:{poison_dir}",
        )
        env.update(overrides)
        return env

    def test_control_unclearing_pythonpath_breaks_the_hook(self):
        """Control experiment (project rule: a fix's test must fail on the
        pre-fix code, or it's decoration): with `export PYTHONPATH=` deleted
        from a copy of the script, the poisoned yaml.py must make the hook
        fail to produce output under a polluted PYTHONPATH -- proving
        test_d actually exercises the hard-clear rather than passing by
        accident."""
        import re

        original = HOOK_SCRIPT.read_text()
        self.assertIn("export PYTHONPATH=", original)
        # The script hard-clears twice: once globally (`export PYTHONPATH=`)
        # and once inline before every python invocation (`PYTHONPATH= "$PY"
        # ...`, belt-and-suspenders since a later env mutation inside the
        # script could otherwise re-leak it). A real regression could drop
        # either guard, so the control removes both to prove the poison
        # actually reaches the venv python once neither is present.
        broken = original.replace("export PYTHONPATH=\n", "", 1)
        broken, n = re.subn(r'PYTHONPATH=\s*("\$PY")', r"\1", broken)
        self.assertGreater(n, 0, "did not find any inline PYTHONPATH= prefixes to remove")
        self.assertNotEqual(broken, original, "did not find the lines to remove")

        # Must live next to the real memidx.py -- the script resolves it via
        # $SCRIPT_DIR/../memidx.py, so a temp-dir copy would fail on that
        # path lookup instead of on the (poisoned) import it's meant to test.
        broken_script = HOOK_SCRIPT.parent / "pre-edit-chain-broken-control.sh"
        broken_script.write_text(broken)
        broken_script.chmod(0o755)
        self.addCleanup(broken_script.unlink)

        env = self._poisoned_env()
        proc = subprocess.run(
            ["bash", str(broken_script)],
            input=self._matching_payload(),
            capture_output=True,
            text=True,
            env=env,
            timeout=5.0,
        )
        # Fail-open contract still holds (exit 0, no stdout) -- but the log
        # must show the poisoned import actually broke the underlying call,
        # not a clean no-match.
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("poisoned PYTHONPATH not cleared", log_text)


class TestMemorySearchSkill(unittest.TestCase):
    def test_e_skill_frontmatter_has_name_and_description(self):
        import yaml

        self.assertTrue(SKILL_FILE.exists(), f"missing {SKILL_FILE}")
        text = SKILL_FILE.read_text()
        self.assertTrue(text.startswith("---\n"), "SKILL.md must open with a frontmatter block")
        end = text.index("\n---", 4)
        frontmatter = yaml.safe_load(text[4:end])
        self.assertEqual(frontmatter.get("name"), "memory-search")
        self.assertTrue((frontmatter.get("description") or "").strip())

    def test_skill_is_at_most_80_lines(self):
        lines = SKILL_FILE.read_text().splitlines()
        self.assertLessEqual(len(lines), 80, f"SKILL.md is {len(lines)} lines, must be <= 80")


if __name__ == "__main__":
    unittest.main()
