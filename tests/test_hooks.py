"""Tests for hooks/pre-edit-chain.sh (the PreToolUse retrieval-surface hook).

These exercise the real shell script via subprocess -- not a reimplementation
of its logic -- because the thing under test is the script's environment
handling (the PYTHONPATH trap, env-driven project/root resolution, fail-open
behavior) as much as its output shape.
"""
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402

# This machine's venv python is never hardcoded in tracked test code -- set
# $MEMCONTINUUM_PYTHON in your own (untracked) shell environment before
# running this file; see README.md "Requirements" / "Running the tests".
VENV_PYTHON = os.environ.get("MEMCONTINUUM_PYTHON", "")
# macOS-port bash-3.2 verification harness (tests/run_bash32.sh): overrides
# which bash interpreter every hook subprocess call below runs under.
# Defaults to the system "bash" (unchanged behavior for every normal run).
MC_BASH = os.environ.get("MC_BASH", "bash")
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


# Task 9 review carry-over item 3: how much extra wall time a config.sh
# resolution hop (a couple of file reads/`source`s before the same venv
# python + FTS-search call every hook makes anyway) is allowed to cost on
# top of the SAME-run, single-hop baseline measured alongside it -- see
# TestPreEditChainHook._direct_python_baseline_elapsed. Generous on purpose:
# a real regression here means seconds (a hang, a retry loop), not
# milliseconds, so this never needs tightening for a genuine resolution-chain
# slowdown to still be caught.
CONFIG_CHAIN_SLACK_S = 2.0


def run_hook(payload_text: str, env: dict, timeout: float = 5.0):
    start = time.monotonic()
    proc = subprocess.run(
        [MC_BASH, str(HOOK_SCRIPT)],
        input=payload_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    elapsed = time.monotonic() - start
    return proc, elapsed


def memidx_wrapper_python(target_dir, name: str, body: str, real_py: str = ""):
    """A bash impersonation of MEMCONTINUUM_PYTHON, shared by every test
    that needs to observe or modify what memidx.py's own machinery does
    without touching the hook script under test: any `-c` call (the
    watchdog launcher's own `-c "$MC_WATCHDOG_LAUNCHER_PY"` invocation, or
    a hook's own inline JSON-parsing/assembly `-c` calls) passes straight
    through to the real venv python unmodified (both need the real
    interpreter to run); only a script-path invocation ("$PY"
    "<engine>/memidx.py" <subcommand> ...) is intercepted, running a
    real-python `-c` snippet that executes `body` (e.g. recording
    `sys.argv[1:]`, monkeypatching a memidx.py function) before handing
    off to memidx.main(sys.argv[1:])."""
    wrapper = Path(target_dir) / name
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f'REAL_PY="{real_py or VENV_PYTHON}"\n'
        'if [ "$1" = "-c" ]; then\n'
        '    exec "$REAL_PY" "$@"\n'
        'fi\n'
        'shift\n'
        f'exec "$REAL_PY" -c \'\n'
        'import sys\n'
        # Double-quoted, not repr() -- the whole snippet is itself
        # wrapped in a bash SINGLE-quoted `-c '...'` string below, so a
        # literal single quote here (what !r would produce) would
        # break out of that bash quoting early.
        f'sys.path.insert(0, "{TOOLS_DIR}")\n'
        'import memidx\n'
        f'{body}\n'
        'sys.exit(memidx.main(sys.argv[1:]))\n'
        "' \"$@\"\n"
    )
    wrapper.chmod(0o755)
    return wrapper


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
        result = subprocess.run([MC_BASH, "-n", str(HOOK_SCRIPT)], capture_output=True, text=True)
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

    def test_log_line_carries_project(self):
        """Liveness metric fix (INC-0103/INC-0105): memidx.py stats groups
        hook.log by project=. pre-edit-chain.sh already stamps it in its own
        finish() -- this is a regression pin, not a new fix, so the shared
        memidx.py stats tool can rely on it for every outcome, matched or
        not."""
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
        run_hook(payload, env)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn(f"project={self.project}", log_text)

    def test_no_file_path_outcome_carries_project(self):
        """Round-2 Codex gate item 10 (finding: `no-file-path` calls
        finish() BEFORE PROJECT used to be initialized -- an empty
        project=). PROJECT resolution was moved above the no-python check
        (item 3), which is itself above the payload read / no-file-path
        gate -- so this outcome must carry a real project= too, not just
        the matched/no-match paths test_log_line_carries_project already
        covers."""
        payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Edit", "cwd": "/nowhere"})
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        proc, _elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "no-file-path" in l]
        self.assertTrue(matching, log_text)
        self.assertIn(f"project={self.project}", matching[-1])

    def test_index_error_fails_open_and_logs_a_distinct_outcome(self):
        """F1 (ruling 65's belt-and-suspenders catch): for-path's exit 4
        (index-error). Renaming records.path breaks the query AFTER
        decision_index_state has already reported a usable state ("current"
        here, since the state check itself never references `path`) -- the
        matched candidate's chain-building (topic_row["path"]) is what
        actually raises. Restores the column afterward (addCleanup) so
        correctness of the rest of this class's shared class-level db
        doesn't depend on unittest's alphabetical run order."""
        db = Path(self.memtool_home) / f"{self.project}.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("ALTER TABLE records RENAME COLUMN path TO path_broken")
        conn.commit(); conn.close()

        def _restore():
            c = sqlite3.connect(str(db))
            c.execute("ALTER TABLE records RENAME COLUMN path_broken TO path")
            c.commit(); c.close()

        self.addCleanup(_restore)

        payload = json.dumps(
            {
                "session_id": "s-preedit-index-error", "hook_event_name": "PreToolUse",
                "tool_name": "Edit", "cwd": "/some/other/unrelated/dir",
                "tool_input": {"file_path": "/fake/repo/src/core/scan/scan_plan.py"},
            }
        )
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
        )
        proc, elapsed = run_hook(payload, env, timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=index-error", log_text, log_text)

    def test_for_path_all_candidates_failing_logs_query_failed_not_no_match(self):
        """Round-3 addendum (review finding): a candidate whose `for-path`
        call itself FAILS (non-zero exit -- a broken python, a corrupt db
        mid-write, any exec failure) used to `continue` silently and, if
        EVERY candidate failed the same way, fall through to the exact
        same `outcome=no-match` a genuine "queried fine, found nothing"
        result produces -- indistinguishable in the log from real
        negative evidence. Must log a distinct `query-failed` outcome
        instead. The stub python fails ONLY the `for-path` calls (not the
        jq-fallback JSON payload parsing, so this test doesn't depend on
        whether jq happens to be on PATH) -- proxying every other call to
        the real venv python."""
        fail_py = Path(self.tmp) / "fail-for-path-python"
        fail_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    for-path) exit 1 ;;\n"
            "  esac\n"
            "done\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        fail_py.chmod(0o755)

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
            MEMCONTINUUM_PYTHON=str(fail_py),
        )
        proc, _elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "outcome=" in l]
        self.assertIn("outcome=query-failed", matching[-1], matching[-1])
        self.assertNotIn("outcome=no-match", matching[-1])

    def test_no_python_resolved_line_carries_project(self):
        """Round-2 review finding: this fail-open diagnostic (the FIRST,
        sometimes ONLY, trace a session with a broken python resolution
        ever leaves) used to have no project= -- memlib.sh's own twin was
        fixed in round 1 (project resolution moved above it); this is the
        same move for pre-edit-chain.sh's independent copy."""
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT="nopy-proj",
            MEMCONTINUUM_PYTHON="/no/such/python",
        )
        proc, _elapsed = run_hook("{}", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "no python resolved" in l]
        self.assertTrue(matching, log_text)
        self.assertIn("project=nopy-proj", matching[-1])

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
        # Cleanup registered BEFORE the write: this file lands in the REAL
        # checkout (it must sit next to memidx.py, see above), so an
        # interruption between write and a later-registered cleanup would
        # leave a broken hook script in the working tree (regate finding 2).
        self.addCleanup(lambda: broken_script.unlink(missing_ok=True))
        broken_script.write_text(broken)
        broken_script.chmod(0o755)

        env = self._poisoned_env()
        proc = subprocess.run(
            [MC_BASH, str(broken_script)],
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

    def _direct_python_baseline_elapsed(self, payload):
        """Task 10 fix-round carry-over (Task 9 review item 3): an absolute
        1.0s wall-clock bound flaked under machine load (observed at load
        10+) -- venv-python startup and the fixture DB's FTS query both
        slow down under CPU contention, for reasons that have nothing to
        do with the config.sh resolution chain under test. Running this
        SAME hook once more with MEMCONTINUUM_PYTHON given directly (the
        single-hop, no-resolution-needed env
        test_a_matching_path_emits_chain_with_citation_reminder uses),
        in the SAME test, gives a load-normalized floor: whatever the box
        is doing right now, this number reflects it too, so a comparison
        against it (see CONFIG_CHAIN_SLACK_S) stays meaningful at any load
        instead of chasing a bigger and bigger constant."""
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
        )
        _, elapsed = run_hook(payload, env)
        return elapsed

    def test_f_resolves_python_via_config_sh_when_env_unset(self):
        """F6 regression, round 4: this hook's own python resolution used
        to be two-step only ($MEMCONTINUUM_PYTHON, else the engine's
        <engine>/.venv/bin/python) -- skipping the config.sh middle step
        hooks/memlib.sh already consults. A non-default venv (e.g. --venv
        pointed elsewhere, or an existing --python handed to
        memcontinuum-setup.sh) left this hook's own python dead. With
        MEMCONTINUUM_PYTHON unset from the env and no engine .venv in this
        checkout, a config.sh at $MEMCONTINUUM_HOME/config.sh is the ONLY
        way this hook can resolve a python at all -- proven end to end (the
        hook actually injects the real chain), not just by inspecting the
        resolved path."""
        if (TOOLS_DIR / ".venv" / "bin" / "python").exists():
            self.skipTest(
                "an engine .venv exists in this checkout (INC-0125) -- this "
                "test can only prove python resolves through the "
                "config-pointer chain when no engine venv exists to mask a "
                "broken chain; see tests/test_env_gate.py"
            )
        config_sh = Path(self.memtool_home) / "config.sh"
        config_sh.write_text(f'MEMCONTINUUM_PYTHON="{VENV_PYTHON}"\n')
        self.addCleanup(lambda: config_sh.unlink(missing_ok=True))

        payload = self._matching_payload()
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
        )
        env.pop("MEMCONTINUUM_PYTHON", None)
        proc, elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        baseline = self._direct_python_baseline_elapsed(payload)
        self.assertLess(
            elapsed, baseline + CONFIG_CHAIN_SLACK_S,
            f"hook took {elapsed:.3f}s vs a same-run direct-python baseline of "
            f"{baseline:.3f}s -- the config.sh resolution hop should add only "
            "milliseconds, not seconds",
        )
        self.assertTrue(proc.stdout.strip(), "expected additionalContext output, got nothing")
        out = json.loads(proc.stdout)
        self.assertIn("TOP-0042", out["hookSpecificOutput"]["additionalContext"])

    def test_r2_resolves_python_via_pointer_config_at_custom_home(self):
        """R2 regression, round 4 gate: a custom-HOME install also writes a
        minimal POINTER config.sh at the fixed default $HOME/.memcontinuum
        (memcontinuum-setup.sh "3. config") recording only the real
        MEMCONTINUUM_HOME -- it carries no MEMCONTINUUM_PYTHON. With NO
        MEMCONTINUUM_HOME in this hook's own environment (every installed
        hook line, by construction), the old single-source step read only
        that pointer and stopped there, leaving MEMCONTINUUM_PYTHON
        unresolved. Must follow through to the REAL config.sh at the
        pointed-at home to find it -- proven end to end, not by inspecting
        a resolved path."""
        if (TOOLS_DIR / ".venv" / "bin" / "python").exists():
            self.skipTest(
                "an engine .venv exists in this checkout (INC-0125) -- this "
                "test can only prove python resolves through the "
                "config-pointer chain when no engine venv exists to mask a "
                "broken chain; see tests/test_env_gate.py"
            )
        fake_home = Path(self.tmp) / "r2-fake-home"
        default_mc_home = fake_home / ".memcontinuum"
        default_mc_home.mkdir(parents=True)
        custom_home = Path(self.tmp) / "r2-custom-mc-home"
        custom_home.mkdir()
        (default_mc_home / "config.sh").write_text(
            f"MEMCONTINUUM_HOME='{custom_home}'\n"
        )
        (custom_home / "config.sh").write_text(
            f"MEMCONTINUUM_PYTHON='{VENV_PYTHON}'\nMEMCONTINUUM_HOME='{custom_home}'\n"
        )
        # The prebuilt index lives at cls.memtool_home -- copy it under the
        # CUSTOM home too, since DB_PATH is derived from MEMCONTINUUM_HOME.
        shutil.copyfile(
            Path(self.memtool_home) / f"{self.project}.sqlite",
            custom_home / f"{self.project}.sqlite",
        )

        payload = self._matching_payload()
        env = clean_env(
            HOME=str(fake_home),
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
        )
        env.pop("MEMCONTINUUM_HOME", None)
        env.pop("MEMCONTINUUM_PYTHON", None)
        proc, elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        baseline = self._direct_python_baseline_elapsed(payload)
        self.assertLess(
            elapsed, baseline + CONFIG_CHAIN_SLACK_S,
            f"hook took {elapsed:.3f}s vs a same-run direct-python baseline of "
            f"{baseline:.3f}s -- the two-hop pointer-config chain should add only "
            "milliseconds, not seconds",
        )
        self.assertTrue(proc.stdout.strip(), "expected additionalContext output, got nothing")
        out = json.loads(proc.stdout)
        self.assertIn("TOP-0042", out["hookSpecificOutput"]["additionalContext"])
        # R3: the hook's own log must land under the REAL (custom) home,
        # never the default one the pointer lives at.
        self.assertTrue((custom_home / "hook.log").exists())
        self.assertFalse((default_mc_home / "hook.log").exists())

    def test_watchdog_lib_missing_still_uses_explicit_memcontinuum_python(self):
        """Coordinator review fix (F6 follow-up): mc-watchdog.sh sourcing
        failure (MC_WATCHDOG_LIB_PATH pointing nowhere) leaves MC_GUARD_PY
        unset -- the PY resolution line used to be
        `PY="${MC_GUARD_PY:-$SCRIPT_DIR/../.venv/bin/python}"`, which falls
        straight to the hardcoded engine-venv default in that case,
        silently dropping an explicitly baked MEMCONTINUUM_PYTHON (mirrors
        TestLedgerPostEdit.test_fail_open_when_watchdog_lib_missing in
        tests/test_write_hooks.py, but that test only proves fail-open, not
        that the EXPLICIT python actually ran -- a marker-writing fake
        python, plus a real successful match that only the real venv
        python (proxied through the fake one) can produce, proves it did)."""
        marker = Path(self.tmp) / "watchdog-lib-missing-marker"
        fake_py = Path(self.tmp) / "fake-python-explicit-marker"
        fake_py.write_text(
            "#!/usr/bin/env bash\n"
            f"echo ran >> '{marker}'\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        fake_py.chmod(0o755)
        payload = self._matching_payload()
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=str(fake_py),
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
            MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh",
        )
        proc, elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertTrue(
            marker.exists(),
            "the explicit MEMCONTINUUM_PYTHON must still run when the watchdog "
            "lib fails to source, not silently fall back to the engine venv",
        )
        self.assertTrue(proc.stdout.strip(), "expected additionalContext output, got nothing")
        out = json.loads(proc.stdout)
        self.assertIn("TOP-0042", out["hookSpecificOutput"]["additionalContext"])


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreEditChainRootAndStaleWarning(unittest.TestCase):
    """Final-fix-wave item 2: pre-edit-chain.sh now passes --root to
    for-path (whenever MEMCONTINUUM_ROOT is set) so a store edited since
    the last reindex is served as a positive match (ruling 68 -- never
    withheld) under its own named hook.log outcome, `index-stale-served`,
    instead of the generic `matched` -- the stale warning itself reaches
    hook.log only via for-path's own stderr (captured by the existing
    `2>>"$LOG"` redirect), never the injected additionalContext payload."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-hook-stale-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = Path(self.tmp) / "store"
        shutil.copytree(SCHEMA_FIXTURE_ROOT, self.root)
        self.memtool_home = str(Path(self.tmp) / "memcontinuum-home")
        os.makedirs(self.memtool_home, exist_ok=True)
        self.project = "hookstaletest"
        args = type(
            "Args",
            (),
            dict(
                root=str(self.root),
                project=self.project,
                db=str(Path(self.memtool_home) / f"{self.project}.sqlite"),
                full=True,
                no_embed=True,
            ),
        )()
        memidx.cmd_reindex(args)

    def _matching_payload(self):
        return json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "cwd": "/some/other/unrelated/dir",
                "tool_input": {"file_path": "/fake/repo/src/core/scan/scan_plan.py"},
            }
        )

    def _run(self):
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
            MEMCONTINUUM_ROOT=str(self.root),
        )
        return run_hook(self._matching_payload(), env)

    def test_stale_store_logs_index_stale_served_not_matched(self):
        (self.root / "topics" / "new-topic.md").write_text(
            "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
        )
        proc, elapsed = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected additionalContext output, got nothing")
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TOP-0042", ctx)          # the real match is still injected
        self.assertNotIn("stale", ctx.lower())  # but the staleness caveat never is

        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=index-stale-served", log_text)
        self.assertNotIn("outcome=matched", log_text)

    def test_current_store_still_logs_plain_matched(self):
        proc, elapsed = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=matched", log_text)
        self.assertNotIn("outcome=index-stale-served", log_text)

    def test_rootless_call_unchanged_still_logs_plain_matched(self):
        # MEMCONTINUUM_ROOT unset entirely: for-path is still called (just
        # without --root, exactly its pre-existing behavior) -- proves the
        # new --root plumbing doesn't fire when the env var isn't there.
        (self.root / "topics" / "new-topic.md").write_text(
            "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
        )
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
        )
        proc, elapsed = run_hook(self._matching_payload(), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=matched", log_text)
        self.assertNotIn("outcome=index-stale-served", log_text)

    def test_single_for_path_call_carries_root_and_with_chain_text(self):
        """Round 5 (ruling 137 / CI evidence: the macOS runner measured
        this hook at 1.003-1.022s against its own 1.0s bar). `--root` on
        `for-path` is what triggers `_index_has_drift`, a real on-disk walk
        of the store -- the match-finding loop's call already pays that
        cost and determines the state (`current`/`stale`/`quarantined`),
        warning on it via its own stderr. `--with-chain-text` (memidx.py
        item 1) now folds the pretty chain-view text into that SAME call's
        JSON envelope, so the old second `for-path` call (CHAIN_TEXT_ARGS,
        fetching only the chain text for the candidate the first call
        already matched) is gone entirely -- one hook run makes exactly
        ONE `for-path` invocation, carrying both flags. A fake memidx.py
        wrapper (`memidx_wrapper_python`) records every script-path
        invocation's argv, independent of what for-path itself prints or
        logs.

        A single matching candidate (the raw file_path itself, no cwd/
        strip-prefix fallback needed) keeps this deterministic: exactly
        one `for-path` invocation total, no ambiguity about which call it
        is."""
        argv_log = Path(self.tmp) / "argv.jsonl"
        wrapper = memidx_wrapper_python(
            self.tmp, "argv-recorder",
            f'ARGV_LOG = "{argv_log}"\n'
            'import json\n'
            'with open(ARGV_LOG, "a") as _f:\n'
            '    _f.write(json.dumps(sys.argv[1:]))\n'
            '    _f.write(chr(10))\n',
        )
        payload = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "cwd": "/nowhere-relevant",
                "tool_input": {"file_path": "src/core/scan/scan_plan.py"},
            }
        )
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=str(wrapper),
            MEMCONTINUUM_ROOT=str(self.root),
        )
        proc, elapsed = run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected additionalContext output, got nothing")

        calls = [json.loads(line) for line in argv_log.read_text().splitlines() if line.strip()]
        for_path_calls = [c for c in calls if c and c[0] == "for-path"]
        self.assertEqual(
            len(for_path_calls), 1,
            f"one memidx invocation per hook run: {for_path_calls}",
        )
        self.assertIn("--root", for_path_calls[0], for_path_calls[0])
        self.assertIn("--with-chain-text", for_path_calls[0], for_path_calls[0])


class TestQueryLib(unittest.TestCase):
    """hooks/mc-query-lib.sh (TOP-0133 L1, search fallback): the query
    builder -- camelCase splitting, generic-stem dropping, extension
    stripping, the 12-token cap, and the relativize-before-tokenize rule.
    Pure bash, no python/watchdog involved -- exercised by sourcing the
    library and calling its functions directly, same in-process-shell
    approach a hook subprocess test would use, minus the hook's own
    payload/watchdog machinery this file doesn't need."""

    QUERY_LIB = TOOLS_DIR / "hooks" / "mc-query-lib.sh"

    def _tokens(self, path: str) -> str:
        proc = subprocess.run(
            [MC_BASH, "-c", f'source {shlex.quote(str(self.QUERY_LIB))}; mc_query_tokens {shlex.quote(path)}'],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def _source_path(self, path: str, cwd: str, prefixes: str) -> str:
        proc = subprocess.run(
            [MC_BASH, "-c",
             f'source {shlex.quote(str(self.QUERY_LIB))}; '
             f'mc_query_source_path {shlex.quote(path)} {shlex.quote(cwd)} {shlex.quote(prefixes)}'],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_extension_stripped_directories_and_stem_split_generic_dropped(self):
        self.assertEqual(self._tokens("src/core/scan/unbound.py"), "scan unbound")

    def test_camel_case_boundary_split(self):
        self.assertEqual(
            self._tokens("SearchFallbackFooBarBazQuxLongToken.tsx"),
            "search fallback foo bar baz qux long token",
        )

    def test_short_tokens_dropped(self):
        # "id" (2 chars) dropped; "index" is itself a generic stem too.
        self.assertEqual(self._tokens("a/id/x.go"), "")

    def test_all_generic_stems_yields_empty(self):
        self.assertEqual(self._tokens("src/lib/index.ts"), "")

    def test_cap_at_twelve_tokens(self):
        # 15 distinct, non-generic, non-short, non-camelCase segments.
        segs = [f"tokenseg{i}" for i in range(15)]
        path = "/".join(segs) + ".py"
        result = self._tokens(path).split()
        self.assertEqual(len(result), 12)
        self.assertEqual(result, [f"tokenseg{i}" for i in range(12)])

    def test_dedupe_preserves_first_occurrence_order(self):
        self.assertEqual(self._tokens("scan/scan/scan_plan.py"), "scan plan")

    def test_backslash_normalized_as_a_path_separator(self):
        """MINOR fix-round item: a Windows-style backslash-separated path
        must tokenize exactly like its forward-slash form -- not glue two
        segments into one bogus token (the pre-fix bug: `tr` silently
        dropped an un-escaped `\\` from its match set entirely, leaving
        `core\\scan` as one unsplit run)."""
        self.assertEqual(self._tokens(r"src\core\scan\unbound.py"), "scan unbound")
        self.assertEqual(
            self._tokens(r"src\core\scan\unbound.py"),
            self._tokens("src/core/scan/unbound.py"),
        )

    def test_source_path_strip_prefix_glob_characters_are_literal(self):
        """A strip-prefix containing `*` must be compared literally: it must
        not pathname-expand against sibling directories on disk and strip
        a path that merely matches the expanded name."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "foo").mkdir()
            (Path(tmp) / "foXo").mkdir()
            path = f"{tmp}/foo/src/widgetthing.py"
            # The literal prefix "<tmp>/fo*o" does not begin this path.
            self.assertEqual(
                self._source_path(path, "", f"{tmp}/fo*o"), path,
            )
            # And a literal `*` directory is still stripped literally.
            (Path(tmp) / "fo*o").mkdir()
            lit = f"{tmp}/fo*o/src/widgetthing.py"
            self.assertEqual(
                self._source_path(lit, "", f"{tmp}/fo*o"), "src/widgetthing.py",
            )

    def test_source_path_prefers_cwd_relative(self):
        self.assertEqual(
            self._source_path("/repo/src/core/scan/unbound.py", "/repo", ""),
            "src/core/scan/unbound.py",
        )

    def test_source_path_falls_back_to_strip_prefix(self):
        self.assertEqual(
            self._source_path(
                "/repo/src/core/scan/unbound.py", "/unrelated", "/other/:/repo/"
            ),
            "src/core/scan/unbound.py",
        )

    def test_source_path_falls_back_to_raw_path_when_nothing_matches(self):
        self.assertEqual(
            self._source_path("/random/path/file.py", "/unrelated", "/other/"),
            "/random/path/file.py",
        )

    def test_worktree_remapped_path_strips_to_its_relative_form(self):
        """A worktree path must never be tokenized on its OWN
        `.worktrees/x/...` segment -- pre-edit-chain.sh's own WT_REMAPPED
        (mc_remap_worktree_path's output, `<configured-root>/<relative>`)
        is what gets passed here, and one of STRIP_PREFIXES (the
        configured root itself) strips it straight down to the bare
        relative form, exactly as if the file had been edited in the main
        checkout."""
        remapped = "/repo-a/src/core/scan/unbound.py"
        rel = self._source_path(remapped, "/some/unrelated/dir", "/repo-a/")
        self.assertEqual(rel, "src/core/scan/unbound.py")
        self.assertEqual(self._tokens(rel), "scan unbound")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreEditChainWorktreeGap(unittest.TestCase):
    """Worktree gap fix (docs/internal/SESSION-HANDOFF-releases-0.3-to-0.6.md
    SS"0.2.0 final" item 1): mc_remap_worktree_path (hooks/mc-path-lib.sh),
    tried only once every existing candidate has already failed for
    FILE_PATH itself. repo_a is wired (MEMCONTINUUM_STRIP_PREFIX names it);
    repo_b is a second, real git repo that is never wired at all -- its own
    worktree must never be remapped onto repo_a's records."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-hook-worktree-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.memtool_home = str(Path(self.tmp) / "memcontinuum-home")
        os.makedirs(self.memtool_home, exist_ok=True)
        self.project = "hookwttest"
        args = type(
            "Args", (), dict(
                root=str(SCHEMA_FIXTURE_ROOT), project=self.project,
                db=str(Path(self.memtool_home) / f"{self.project}.sqlite"),
                full=True, no_embed=True,
            ),
        )()
        memidx.cmd_reindex(args)

        def _git_repo(path, files):
            path.mkdir(parents=True)
            for rel, content in files.items():
                fp = path / rel
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content)
            for cmd in (
                ["git", "init", "-q"],
                ["git", "config", "user.email", "t@t.local"],
                ["git", "config", "user.name", "t"],
            ):
                subprocess.run(cmd, cwd=path, check=True)
            subprocess.run(["git", "add", "-A"], cwd=path, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)

        # repo_a: the wired code root -- its tree matches fixtures/schema's
        # own code_refs (src/core/scan/scan_plan.py -> TOP-0042).
        self.repo_a = Path(self.tmp) / "repo-a"
        _git_repo(self.repo_a, {"src/core/scan/scan_plan.py": "# a\n"})
        self.repo_a_wt = Path(self.tmp) / "repo-a-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-a", str(self.repo_a_wt)],
            cwd=self.repo_a, check=True,
        )

        # repo_b: a real repo, never wired -- its worktree must stay unwired.
        self.repo_b = Path(self.tmp) / "repo-b"
        _git_repo(self.repo_b, {"src/other.py": "# b\n"})
        self.repo_b_wt = Path(self.tmp) / "repo-b-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-b", str(self.repo_b_wt)],
            cwd=self.repo_b, check=True,
        )

    def _env(self, **overrides):
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX=str(self.repo_a) + "/",
        )
        env.update(overrides)
        return env

    def _payload(self, file_path, cwd="/some/unrelated/parent-dir"):
        return json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "cwd": cwd,
                "tool_input": {"file_path": file_path},
            }
        )

    def _last_outcome_line(self, log_text):
        matching = [l for l in log_text.splitlines() if "outcome=" in l]
        self.assertTrue(matching, log_text)
        return matching[-1]

    def test_worktree_of_wired_repo_gets_same_chain_as_main(self):
        wt_path = str(self.repo_a_wt / "src" / "core" / "scan" / "scan_plan.py")
        proc, _ = run_hook(self._payload(wt_path), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected additionalContext, got nothing")
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TOP-0042", ctx)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=matched", self._last_outcome_line(log_text))

    def test_main_checkout_of_wired_repo_still_matches_unchanged(self):
        main_path = str(self.repo_a / "src" / "core" / "scan" / "scan_plan.py")
        proc, _ = run_hook(self._payload(main_path), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("TOP-0042", out["hookSpecificOutput"]["additionalContext"])

    def test_worktree_of_unwired_repo_distinct_outcome_no_chain(self):
        wt_path = str(self.repo_b_wt / "src" / "other.py")
        proc, _ = run_hook(self._payload(wt_path), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=worktree-unwired", self._last_outcome_line(log_text))

    def test_main_checkout_miss_pays_no_git_call(self):
        """requirement 1: a lookup on a path already inside a configured
        root pays no extra `git` call. A fake `git` that both leaves a
        marker AND fails outright proves both halves at once: if it were
        ever invoked, either the marker or a crash would show it.

        Search-fallback (TOP-0133 L1): this branch no longer `finish`es
        plain `no-match` (see pre-edit-chain.sh) -- it now runs the
        fallback search (a python subprocess, never git), which the
        assertions below allow for without weakening the git-call proof
        this test actually exists for. src/core/scan/unbound.py's own
        query words ("scan unbound", "src"/"core" dropped as generic)
        find nothing in SCHEMA_FIXTURE_ROOT, so this stays a deterministic
        zero-hit fallback."""
        fake_git_dir = Path(self.tmp) / "fake-git-bin-noop"
        fake_git_dir.mkdir()
        marker = Path(self.tmp) / "git-was-called"
        fake_git = fake_git_dir / "git"
        fake_git.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        fake_git.chmod(0o755)

        miss_path = self.repo_a / "src" / "core" / "scan" / "unbound.py"
        miss_path.write_text("# unbound, no code_ref binds this\n")
        env = self._env(PATH=f"{fake_git_dir}:{os.environ.get('PATH', '')}")
        proc, _ = run_hook(self._payload(str(miss_path)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertFalse(marker.exists(), "git must not be invoked for a path already under a configured root")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=search-fallback-empty", self._last_outcome_line(log_text))
        self.assertIn("reason=no-hits", self._last_outcome_line(log_text))

    def test_git_failure_on_worktree_gives_unresolved_not_crash(self):
        fake_git_dir = Path(self.tmp) / "fake-git-bin-fail"
        fake_git_dir.mkdir()
        fake_git = fake_git_dir / "git"
        fake_git.write_text("#!/bin/sh\nexit 128\n")
        fake_git.chmod(0o755)

        wt_path = str(self.repo_a_wt / "src" / "core" / "scan" / "scan_plan.py")
        env = self._env(PATH=f"{fake_git_dir}:{os.environ.get('PATH', '')}")
        proc, _ = run_hook(self._payload(wt_path), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=worktree-unresolved", self._last_outcome_line(log_text))

    def test_stats_counts_the_new_outcomes(self):
        wt_path = str(self.repo_b_wt / "src" / "other.py")
        run_hook(self._payload(wt_path), self._env())
        # memidx.py stats is dynamic per outcome name (kind="pre-edit"
        # tallies every outcome string it sees, no fixed list) -- exercised
        # end-to-end via the CLI, matching how a real operator would check.
        result = subprocess.run(
            [VENV_PYTHON, str(TOOLS_DIR / "memidx.py"), "stats",
             "--home", self.memtool_home, "--project", self.project, "--json"],
            capture_output=True, text=True, env=clean_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        pre_edit_outcomes = payload.get("pre_edit", {}).get("outcomes", {})
        self.assertIn("worktree-unwired", pre_edit_outcomes, payload)

    def test_in_root_worktree_gets_same_chain_as_main(self):
        """Round 2 BLOCKER (Grok): a worktree checked out INSIDE the wired
        root (e.g. `<root>/.worktrees/feat/`) used to be silently skipped
        by the remap -- the callers' own "already under a configured
        root" containment check short-circuited before
        mc_remap_worktree_path was ever tried, even though this is the
        ONLY worktree location the settings-level "if" glob lets this
        script be invoked for at all (repo_a_wt, a SIBLING, is what every
        other test in this class exercises). cwd=repo_a (not the default
        unrelated dir) so the cwd-relative candidate is also exercised as
        a genuine miss before the remap fires."""
        in_root_wt = self.repo_a / ".worktrees" / "feat"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-in-root", str(in_root_wt)],
            cwd=self.repo_a, check=True,
        )
        wt_path = str(in_root_wt / "src" / "core" / "scan" / "scan_plan.py")
        proc, _ = run_hook(self._payload(wt_path, cwd=str(self.repo_a)), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected additionalContext, got nothing")
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TOP-0042", ctx)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=matched", self._last_outcome_line(log_text))

    def test_configured_root_that_is_itself_a_linked_worktree(self):
        """Grok's second MAJOR / Codex's MAJOR: the CONFIGURED root
        (MEMCONTINUUM_STRIP_PREFIX) can itself be a linked worktree -- its
        own `.git` is a FILE, not a directory, so `cd "$root/.git"` used
        to fail and this root was silently skipped, never matching ANY
        worktree of the same repo. self.repo_a_wt (a sibling worktree of
        repo_a) is wired as the root here; a SECOND sibling worktree of
        the same repo is edited."""
        second_wt = Path(self.tmp) / "repo-a-second-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-second", str(second_wt)],
            cwd=self.repo_a, check=True,
        )
        env = self._env(MEMCONTINUUM_STRIP_PREFIX=str(self.repo_a_wt) + "/")
        wt_path = str(second_wt / "src" / "core" / "scan" / "scan_plan.py")
        proc, _ = run_hook(self._payload(wt_path), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("TOP-0042", out["hookSpecificOutput"]["additionalContext"])

    def test_glob_characters_in_a_configured_root_stay_literal(self):
        """The colon-separated MEMCONTINUUM_STRIP_PREFIX is split without
        pathname expansion: a root spelled `<...>/code-star*` is a literal
        root name, never a glob matching the real `code-star` and a sibling
        `code-starX`. Literal root => the in-root worktree below is not
        under any configured root => worktree-unwired, exactly as before
        the root split was factored into mc_strip_prefix_roots."""
        star_root = Path(self.tmp) / "code-star"
        star_root.mkdir()
        (Path(self.tmp) / "code-starX").mkdir()
        subprocess.run(["git", "init", "-q"], cwd=star_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=star_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=star_root, check=True)
        (star_root / "Sources").mkdir()
        (star_root / "Sources" / "Foo.swift").write_text("// x\n")
        subprocess.run(["git", "add", "-A"], cwd=star_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=star_root, check=True)
        star_wt = star_root / ".worktrees" / "x"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-star", str(star_wt)], cwd=star_root, check=True,
        )
        env = self._env(MEMCONTINUUM_STRIP_PREFIX=str(star_root) + "*")
        proc, _ = run_hook(self._payload(str(star_wt / "Sources" / "Foo.swift")), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=worktree-unwired", self._last_outcome_line(log_text))

    def test_separate_git_dir_root_remaps_worktree(self):
        """A configured root created via `git init --separate-git-dir`
        also has a `.git` FILE at its own top, the same layout defect as
        a root that is itself a linked worktree -- covered by the same
        fix (mc_remap_worktree_path's root loop now asks git for the
        root's own common-dir identity instead of assuming `.git` is a
        directory)."""
        sep_root = Path(self.tmp) / "repo-sep"
        sep_gitdir = Path(self.tmp) / "repo-sep-gitdir"
        sep_root.mkdir()
        subprocess.run(["git", "init", "-q", f"--separate-git-dir={sep_gitdir}"], cwd=sep_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=sep_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=sep_root, check=True)
        (sep_root / "src" / "core" / "scan").mkdir(parents=True)
        (sep_root / "src" / "core" / "scan" / "scan_plan.py").write_text("# sep\n")
        subprocess.run(["git", "add", "-A"], cwd=sep_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=sep_root, check=True)
        sep_wt = Path(self.tmp) / "repo-sep-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-sep", str(sep_wt)], cwd=sep_root, check=True,
        )

        env = self._env(MEMCONTINUUM_STRIP_PREFIX=str(sep_root) + "/")
        wt_path = str(sep_wt / "src" / "core" / "scan" / "scan_plan.py")
        proc, _ = run_hook(self._payload(wt_path), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertIn("TOP-0042", out["hookSpecificOutput"]["additionalContext"])

    def test_git_dir_env_poisoning_unwired_worktree_not_remapped(self):
        """Grok's first MAJOR: mc_remap_worktree_path's own `git` calls
        must ignore an ambient GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR --
        here GIT_DIR points at the WIRED repo (repo_a) while the edited
        file is a worktree of the UNWIRED repo_b; a leaked GIT_DIR must
        never turn that into a false match onto repo_a's records. Pinned
        to the exact outcome (worktree-unwired), not just "not remapped"
        -- a sloppy assertion would also pass against a wrong fix that
        produced worktree-unresolved instead."""
        env = self._env(GIT_DIR=str(self.repo_a / ".git"))
        wt_path = str(self.repo_b_wt / "src" / "other.py")
        proc, _ = run_hook(self._payload(wt_path), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=worktree-unwired", self._last_outcome_line(log_text))

    def test_junk_gitfile_below_unwired_dir_gives_unresolved(self):
        """A `.git` FILE containing junk (not a real `gitdir:` pointer)
        below an otherwise-ordinary, unwired directory must fail git's own
        resolution -- pinned to worktree-unresolved specifically, distinct
        from the GIT_DIR-poisoning case's worktree-unwired above."""
        junk_dir = Path(self.tmp) / "junk-unwired"
        (junk_dir / "sub").mkdir(parents=True)
        (junk_dir / ".git").write_text("not a real gitfile\n")
        target = junk_dir / "sub" / "file.py"
        target.write_text("# x\n")
        proc, _ = run_hook(self._payload(str(target)), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertIn("outcome=worktree-unresolved", self._last_outcome_line(log_text))

    def test_ordinary_checkout_of_unwired_repo_outcome_unchanged(self):
        """Requirement 5: a plain (non-worktree) checkout of a repo that
        was never wired must keep today's plain no-match-BRANCH outcome
        (now the search-fallback channel, TOP-0133 L1 -- `no-match` itself
        no longer `finish`es this branch, see pre-edit-chain.sh), never a
        new worktree-* one -- this is repo_b's OWN main checkout, not
        repo_b_wt. Search-fallback (TOP-0133 L1): this path's own query
        words include this TEST RUN's tmpdir name (no STRIP_PREFIX/cwd
        strips repo_b's path, so the raw absolute path is tokenized -- see
        mc-query-lib.sh's own fallback-to-raw-path rule), which can
        legitimately hit SCHEMA_FIXTURE_ROOT's own content (it mentions
        this engine by name) -- so stdout is no longer pinned empty; only
        the OUTCOME's kind is, deliberately widened to both fallback
        outcomes rather than one exact string."""
        main_path = str(self.repo_b / "src" / "other.py")
        proc, _ = run_hook(self._payload(main_path), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        outcome_line = self._last_outcome_line(log_text)
        self.assertNotIn("outcome=worktree-unwired", outcome_line)
        self.assertNotIn("outcome=worktree-unresolved", outcome_line)
        self.assertTrue(
            "outcome=search-fallback " in outcome_line or "outcome=search-fallback-empty " in outcome_line,
            outcome_line,
        )

    def test_in_root_worktree_miss_logs_a_clean_query_no_worktree_or_branch_tokens(self):
        """TESTS item (coordinator fix round): an in-root worktree MISS
        (mirrors test_in_root_worktree_gets_same_chain_as_main's own
        fixture, but at a path with no code_ref -- a genuine miss that
        reaches the search fallback) must query on the path's own words
        via QUERY_SRC_PATH=WT_REMAPPED, never the raw `.worktrees/feat/`
        segment or the `wt-in-root` branch name. `src/core/scan/scan_
        unbound.py` tokenizes to exactly "scan unbound" once `src`/`core`
        (both generic stems) are dropped and `scan_unbound`'s `_` splits
        -- asserted as an EXACT match on the logged `q=` field, not merely
        the absence of "worktrees" (a weaker check a raw-path
        implementation could still pass by accident)."""
        in_root_wt = self.repo_a / ".worktrees" / "feat"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-in-root", str(in_root_wt)],
            cwd=self.repo_a, check=True,
        )
        miss_path = in_root_wt / "src" / "core" / "scan" / "scan_unbound.py"
        miss_path.parent.mkdir(parents=True, exist_ok=True)
        miss_path.write_text("# unbound\n")
        proc, _ = run_hook(
            self._payload(str(miss_path), cwd=str(self.repo_a)),
            self._env(MEMCONTINUUM_FALLBACK_MODE="fts"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        outcome_line = self._last_outcome_line(log_text)
        self.assertNotIn("outcome=worktree-unwired", outcome_line)
        self.assertNotIn("outcome=worktree-unresolved", outcome_line)
        self.assertIn("q=scan+unbound", outcome_line)
        # Scoped to the q= FIELD specifically, not the whole line -- the
        # line's own `file=` field legitimately carries the real,
        # absolute edited path (`.worktrees/feat/...` included, exactly
        # as it should); this checks that the QUERY built off that path
        # -- not the raw path itself -- is scrubbed of those segments.
        q_match = re.search(r"\bq=(\S*)", outcome_line)
        self.assertIsNotNone(q_match, outcome_line)
        self.assertEqual(q_match.group(1), "scan+unbound")
        self.assertNotIn("worktrees", q_match.group(1))
        self.assertNotIn("wt-in-root", q_match.group(1))


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreEditChainSearchFallback(unittest.TestCase):
    """search fallback (TOP-0133 L1): on pre-edit-chain.sh's own genuine
    no-match branch, `memidx.py search --hydrate` is queried on the
    path's own words and the nearest decision (if any) is injected,
    labelled as a guess. Fixture: ONE topic (TOP-7001, "Needle handling
    policy") whose code_refs point somewhere ELSE entirely (so `for-path`
    never matches the miss file), but whose title/ruling text contains
    "needle" -- exactly the word the miss file's own path tokenizes to --
    so FTS finds it. MEMCONTINUUM_FALLBACK_MODE=fts throughout: no
    embedding model dependency, deterministic, fast."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-hook-fallback-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.memtool_home = str(Path(self.tmp) / "memcontinuum-home")
        os.makedirs(self.memtool_home, exist_ok=True)
        self.project = "hookfallbacktest"

        self.store = Path(self.tmp) / "store"
        (self.store / "topics").mkdir(parents=True)
        # F5's family collapse means a query word shared with the link's
        # own ruling text (e.g. "needle") surfaces the LINK row, whose
        # `snippet` falls back to that SAME ruling text -- indistinguishable
        # from chain_text and useless as a no-snippet mutation check. "needle"
        # here appears ONLY in the title (for-path's own code_refs never
        # match the miss file, so this is unreachable any other way) and the
        # BODY -- the ruling text uses a wholly different word ("widget"), so
        # the match routes through the TOPIC row, whose `snippet` (body
        # prose) is then genuinely, checkably different from chain_text
        # (title + ruling only, never body prose).
        (self.store / "topics" / "needle.md").write_text(
            "---\ntype: topic\nid: TOP-7001\ntitle: Needle handling policy\n"
            "code_refs:\n  - somewhere/else/unrelated.py\n"
            "links:\n"
            '  - link: L1\n    status: active\n    '
            'ruling: {text: "handle the widget case", authority: owner-verbatim, source: s}\n'
            "---\nBody prose mentioning needle handling in detail, at length.\n"
        )
        args = type(
            "Args", (), dict(
                root=str(self.store), project=self.project,
                db=str(Path(self.memtool_home) / f"{self.project}.sqlite"),
                full=True, no_embed=True,
            ),
        )()
        memidx.cmd_reindex(args)

        self.repo = Path(self.tmp) / "repo"
        (self.repo / "src").mkdir(parents=True)

    def _env(self, **overrides):
        env = clean_env(
            MEMCONTINUUM_HOME=self.memtool_home,
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX=str(self.repo) + "/",
            MEMCONTINUUM_FALLBACK_MODE="fts",
        )
        env.update(overrides)
        return env

    def _payload(self, file_path, cwd="/some/unrelated", session_id="sess-fb-1"):
        return json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "cwd": cwd, "session_id": session_id,
            "tool_input": {"file_path": file_path},
        })

    def _last_outcome_line(self, log_text):
        matching = [l for l in log_text.splitlines() if "outcome=" in l]
        self.assertTrue(matching, log_text)
        return matching[-1]

    def test_miss_never_returns_readme_or_sources_records(self):
        """The fallback guesses a DECISION: a store README and a sources/
        record that match the file's words far better than the topic are
        never returned."""
        (self.store / "sources").mkdir()
        (self.store / "README.md").write_text("# Store\n" + "needle " * 20 + "\n")
        (self.store / "sources" / "sitting.md").write_text(
            "---\ntype: source\nid: SITTING-1\ntitle: needle needle\n---\n" + "needle " * 20 + "\n")
        memidx.cmd_reindex(type("Args", (), dict(
            root=str(self.store), project=self.project,
            db=str(Path(self.memtool_home) / f"{self.project}.sqlite"),
            full=True, no_embed=True))())
        proc, _ = run_hook(self._payload(str(self.repo / "src" / "needle.py")), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_line = self._last_outcome_line((Path(self.memtool_home) / "hook.log").read_text())
        self.assertIn("ids=TOP-7001", log_line)
        self.assertNotIn("README", log_line)
        self.assertNotIn("SITTING", log_line)

    def _run_with_fake_search(self, envelope, **env_overrides):
        """Run pre-edit-chain.sh on a miss with `memidx.py search` replaced
        by a fixed --json envelope (for-path etc. stay real)."""
        # The envelope goes through a file: the wrapper body is embedded in
        # a bash single-quoted string, so a repr() literal would break it.
        env_file = Path(self.tmp) / "fake-search-envelope.json"
        env_file.write_text(json.dumps(envelope))
        wrapper = memidx_wrapper_python(
            self.tmp, "fake-search",
            'if sys.argv[1] == "search":\n'
            f'    print(open("{env_file}").read())\n'
            '    sys.exit(0)\n',
        )
        env = self._env(MEMCONTINUUM_PYTHON=str(wrapper), **env_overrides)
        proc, _ = run_hook(self._payload(str(self.repo / "src" / "floorcase.py")), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        return proc, self._last_outcome_line(log_text)

    @staticmethod
    def _floor_envelope(second_score, **extra):
        return {"results": [
            {"id": "TOP-9201", "title": "First", "chain_text": "chain nine-two-oh-one", "score": 0.0328},
            {"id": "TOP-9202", "title": "Second", "chain_text": "chain nine-two-oh-two", "score": second_score},
        ], **extra}

    def test_second_floor_drops_a_below_floor_second_hit_end_to_end(self):
        """TOP-0133 L5: RRF envelope, second score below the 0.0314 floor ->
        one hit rendered, `floor_dropped=` logged, dropped id absent from
        session state."""
        proc, line = self._run_with_fake_search(self._floor_envelope(0.0200, fusion="rrf"))
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("chain nine-two-oh-one", ctx)
        self.assertNotIn("chain nine-two-oh-two", ctx)
        self.assertIn("hits=1 ids=TOP-9201", line)
        self.assertIn("floor_dropped=TOP-9202", line)
        state_file = Path(self.memtool_home) / "sessions" / self.project / "sess-fb-1.json"
        state = json.loads(state_file.read_text())
        self.assertEqual([e["id"] for e in state["search_fallbacks"]], ["TOP-9201"])

    def test_second_floor_env_zero_and_custom_value(self):
        env = self._floor_envelope(0.0200, fusion="rrf")
        proc, line = self._run_with_fake_search(env, MEMCONTINUUM_FALLBACK_SECOND_FLOOR="0")
        self.assertIn("hits=2", line)
        self.assertNotIn("floor_dropped", line)
        proc, line = self._run_with_fake_search(env, MEMCONTINUUM_FALLBACK_SECOND_FLOOR="0.01")
        self.assertIn("hits=2", line)
        proc, line = self._run_with_fake_search(env, MEMCONTINUUM_FALLBACK_SECOND_FLOOR="bogus")
        self.assertIn("hits=1", line)
        self.assertIn("floor_dropped=TOP-9202", line)

    def test_second_floor_not_applied_to_a_non_rrf_envelope(self):
        proc, line = self._run_with_fake_search(self._floor_envelope(0.0200, fusion="vector-only"))
        self.assertIn("hits=2", line)
        self.assertNotIn("floor_dropped", line)

    def test_miss_with_hit_label_and_chain_never_snippet(self):
        target = str(self.repo / "src" / "needle.py")
        proc, _ = run_hook(self._payload(target), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected additionalContext, got nothing")
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # Exact label, byte-for-byte, at the very start -- a mutated label
        # (reworded, retitled, moved) fails this immediately.
        label = "No recorded decision binds this file. Nearest by search -- may be unrelated:"
        self.assertTrue(ctx.startswith(label + "\n\n"), ctx)
        # The hit's own chain text (rationale) is present...
        self.assertIn("TOP-7001", ctx)
        self.assertIn("handle the widget case", ctx)
        # ...but the SNIPPET field (record_row's body prose, a DIFFERENT
        # field cmd_search also returns) must never appear -- a code
        # change that swapped chain_text for snippet in the injection
        # would still "have content" but fail this specific check. Genuinely
        # mutation-tested (not just asserted): temporarily making the
        # fallback inject `snippet` instead of/alongside `chain_text` was
        # verified to fail this exact assertion before this fix shipped.
        self.assertNotIn("Body prose mentioning needle handling in detail, at length", ctx)

        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback ", line)
        self.assertIn("hits=1", line)
        self.assertIn("ids=TOP-7001", line)
        self.assertIn("mode=fts", line)
        self.assertNotIn("outcome=no-match", line)

        # Session-state title storage: the look-back channel's own data
        # source (see TestUserPromptLookback in tests/test_write_hooks.py).
        state_file = Path(self.memtool_home) / "sessions" / self.project / "sess-fb-1.json"
        state = json.loads(state_file.read_text())
        fallbacks = state.get("search_fallbacks")
        self.assertTrue(fallbacks)
        self.assertEqual(fallbacks[0]["id"], "TOP-7001")
        self.assertEqual(fallbacks[0]["title"], "Needle handling policy")
        self.assertEqual(fallbacks[0]["file"], target)
        self.assertEqual(fallbacks[0]["hook"], "pre-edit-chain")

    def test_zero_hit_empty_outcome_no_injection(self):
        target = str(self.repo / "src" / "zzznohitzzz.py")
        proc, _ = run_hook(self._payload(target), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback-empty", line)
        self.assertIn("reason=no-hits", line)

    def test_no_query_after_filtering_empty_outcome(self):
        # "src"/"lib"/"index" are ALL generic stems -- nothing survives.
        target = str(self.repo / "src" / "lib" / "index.ts")
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        proc, _ = run_hook(self._payload(target), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback-empty", line)
        self.assertIn("reason=no-query", line)

    def test_store_root_file_skipped_with_reason_logged(self):
        target = str(self.store / "topics" / "unrelated-new.md")
        # MEMCONTINUUM_STRIP_PREFIX cleared: irrelevant to the store-root
        # skip itself, and leaving it set would route this miss through
        # the worktree-gap block first (this sandbox's ambient /tmp/.git
        # makes that block's own git resolution environment-dependent --
        # see the accepted test_outside_scope_edit_pays_no_git_call
        # failure elsewhere in this suite) -- unset, that block never
        # activates, keeping this test about the store-root check alone.
        proc, _ = run_hook(
            self._payload(target),
            self._env(MEMCONTINUUM_ROOT=str(self.store), MEMCONTINUUM_STRIP_PREFIX=""),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback-empty", line)
        self.assertIn("reason=store-root", line)

    def test_match_never_runs_the_fallback(self):
        """A real for-path match must never reach the fallback at all --
        no `search-fallback`/`search-fallback-empty` outcome, and no extra
        memidx invocation beyond the one `for-path` call (reusing the
        argv-recording python-shim pattern TestPreEditChainRootAndStale-
        Warning already established, rather than reimplementing it)."""
        (self.store / "topics" / "matcher.md").write_text(
            "---\ntype: topic\nid: TOP-8001\ntitle: Matcher topic\n"
            "code_refs:\n  - src/matched.py\n"
            "links:\n"
            '  - link: L1\n    status: active\n    '
            'ruling: {text: "r", authority: owner-verbatim, source: s}\n'
            "---\nBody.\n"
        )
        args = type(
            "Args", (), dict(
                root=str(self.store), project=self.project,
                db=str(Path(self.memtool_home) / f"{self.project}.sqlite"),
                full=True, no_embed=True,
            ),
        )()
        memidx.cmd_reindex(args)

        argv_log = Path(self.tmp) / "argv.jsonl"
        wrapper = memidx_wrapper_python(
            self.tmp, "argv-recorder",
            f'ARGV_LOG = "{argv_log}"\n'
            'import json\n'
            'with open(ARGV_LOG, "a") as _f:\n'
            '    _f.write(json.dumps(sys.argv[1:]))\n'
            '    _f.write(chr(10))\n',
        )
        target = str(self.repo / "src" / "matched.py")
        env = self._env(MEMCONTINUUM_PYTHON=str(wrapper))
        proc, _ = run_hook(self._payload(target), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        self.assertNotIn("search-fallback", log_text)
        calls = [json.loads(line) for line in argv_log.read_text().splitlines() if line.strip()]
        search_calls = [c for c in calls if c and c[0] == "search"]
        self.assertEqual(search_calls, [], "a match must never invoke `search`")

    def test_no_write_path_store_and_index_untouched(self):
        """Item 7, scope corrected (re-gate round 3, MAJOR 2): the fixture
        index here is ALREADY current/stamped (setUp's own cmd_reindex),
        so the sha staying unchanged across 50 runs proves the FALLBACK
        SEARCH itself adds no write -- it does NOT prove the whole hook
        run never writes the index. The same hook's own earlier `for-path`
        lookup (unchanged by this feature) still opens the index the
        ordinary rw way and CAN migrate/stamp a schema behind the current
        generation before the fallback ever runs -- see
        test_search_read_only_refuses_legacy_schema_without_writing in
        tests/test_memidx.py for that direct, isolated proof, and
        test_for_path_miss_still_migrates_before_the_fallback_runs below
        for the same claim exercised through this actual hook. 50 fallback
        runs against a fixture store that is a git repo -- `git status
        --short` stays empty and the index file's sha256 is unchanged.
        The one write this hook makes (session state, see
        test_miss_with_hit_label_and_chain_never_snippet) lives under
        MEMCONTINUUM_HOME/sessions/, never under the store or the index."""
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t.local"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=self.store, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.store, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=self.store, check=True)

        db_path = Path(self.memtool_home) / f"{self.project}.sqlite"
        sha_before = hashlib.sha256(db_path.read_bytes()).hexdigest()

        target = str(self.repo / "src" / "needle.py")
        for _ in range(50):
            proc, _ = run_hook(
                self._payload(target), self._env(MEMCONTINUUM_ROOT=str(self.store)),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

        sha_after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        self.assertEqual(sha_before, sha_after, "the index must never be written by the fallback")
        status = subprocess.run(
            ["git", "status", "--short"], cwd=self.store, capture_output=True, text=True, check=True,
        )
        self.assertEqual(status.stdout.strip(), "", "the store must never be written by the fallback")

    def test_for_path_miss_still_migrates_before_the_fallback_runs(self):
        """Re-gate round 3, MAJOR 2, exercised end to end through the real
        hook (not just memidx.py directly): a schema behind the current
        generation, ALSO missing its db_meta project stamp, gets that
        stamp written by this SAME hook run's own earlier `for-path`
        lookup (unchanged, pre-existing, correct behavior -- a match must
        keep seeing a migrated schema) -- the sha genuinely changes --
        while the fallback's own `--read-only` search, reached moments
        later in the SAME run, still correctly refuses with reason=
        index-needs-migration (the project-stamp guard and the
        generation-number stamp are different db_meta keys; only a real
        `reindex` bumps the latter, so for-path's own migration guards
        never silently paper over a stale generation number)."""
        db_path = Path(self.memtool_home) / f"{self.project}.sqlite"
        conn = sqlite3.connect(str(db_path))
        conn.execute("UPDATE db_meta SET value='1' WHERE key='index_generation'")
        conn.execute("DELETE FROM db_meta WHERE key='project'")
        conn.commit()
        conn.close()
        sha_before = hashlib.sha256(db_path.read_bytes()).hexdigest()

        target = str(self.repo / "src" / "needle.py")
        proc, _ = run_hook(self._payload(target), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

        sha_after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        self.assertNotEqual(sha_before, sha_after, "for-path's own rw open must still stamp the project row")

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT value FROM db_meta WHERE key='project'").fetchone()
        conn.close()
        self.assertEqual(row[0], self.project)

        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback-empty", line)
        self.assertIn("reason=index-needs-migration", line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_search_crash_names_search_failed_rc_not_no_hits(self):
        """MAJOR fix-round item b, pinning: a `search` subprocess that
        exits non-zero with NOTHING on stdout (a genuine crash, not a
        JSON refusal envelope) used to collapse into `reason=no-hits`
        (the old gate only ran the reason parser when stdout was
        non-empty) -- now names `reason=search-failed rc=N`."""
        wrapper = memidx_wrapper_python(
            self.tmp, "crash-search",
            'if sys.argv[1:2] == ["search"]:\n'
            '    sys.exit(7)\n',
        )
        target = str(self.repo / "src" / "needle.py")
        proc, _ = run_hook(self._payload(target), self._env(MEMCONTINUUM_PYTHON=str(wrapper)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback-empty", line)
        self.assertIn("reason=search-failed rc=7", line)
        self.assertNotIn("reason=no-hits", line)

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_flock_held_whole_run_still_emits_the_fallbacks_own_document_fast(self):
        """BLOCKER + re-gate round 3, MAJOR 1, pinning (docstring corrected
        to state what THIS code actually does -- the round-2 version's own
        claim, "printed LAST after the state write gives up per its own
        [2.0s] deadline", was true of the SYMPTOM (one document) but false
        about WHICH document: with a 2.0s state-write deadline racing the
        SAME 2s watchdog budget, the watchdog usually wins, killing the
        run and replacing stdout with ITS OWN generic timeout envelope --
        the already-computed guess is silently discarded and the model is
        told something false ("absence... not established") about a
        decision that was in fact found).
        Fixed by giving the state write a SHORT deadline (0.25s,
        mc_fallback_write_state) instead of the default 2.0s: hold the
        session-state file's own flock in THIS test process for the WHOLE
        run -- the hook must still finish well under the 2s watchdog
        budget, stdout must be exactly one document, and it must be the
        FALLBACK's OWN additionalContext (the label text is asserted
        present, not merely "some JSON with hookSpecificOutput" -- the
        watchdog's own fallback envelope has neither), with `outcome=
        search-fallback` (not `watchdog-killed`) and `fb_state=
        skipped-lock` naming the skipped titling write."""
        session_id = "sess-flock-1"
        state_dir = Path(self.memtool_home) / "sessions" / self.project
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / f"{session_id}.json"
        state_file.write_text("{}")
        lock_file = state_dir / f"{session_id}.json.lock"
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            target = str(self.repo / "src" / "needle.py")
            proc, elapsed = run_hook(
                self._payload(target, session_id=session_id), self._env(), timeout=8.0,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Re-gate round 4 (Opus): dropped the wall-clock `elapsed < 4.0`
        # bound entirely -- this hook is hard-bounded to ~2.03s by its OWN
        # watchdog regardless of what this fix does, so a 4.0s bar can
        # NEVER fail here and was pinning nothing. The deterministic,
        # load-independent proof that the fix landed is `outcome=search-
        # fallback` (never `watchdog-killed`) plus `fb_state=skipped-lock`
        # below; the one genuinely load-bearing timing pin is `fb_ms`
        # itself (the SEARCH subprocess's own millisecond timing, off the
        # log line, unrelated to wall-clock/CPU contention noise around
        # the whole test process) -- the ceiling asserted below is
        # the watchdog budget, not a speed claim.
        stdout = proc.stdout.strip()
        self.assertTrue(stdout, "expected an additionalContext envelope even under a held lock")
        # Exactly one JSON document: json.loads succeeds on the WHOLE
        # stdout, and there is exactly one line.
        parsed = json.loads(stdout)
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(
            ctx.startswith("No recorded decision binds this file. Nearest by search -- may be unrelated:"),
            "stdout must be the FALLBACK's own document, not the watchdog's generic timeout envelope",
        )
        self.assertIn("TOP-7001", ctx)
        self.assertEqual(len(stdout.splitlines()), 1, stdout)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback ", line)
        self.assertIn("fb_state=skipped-lock", line)
        self.assertNotIn("watchdog-killed", line)
        fb_ms_match = re.search(r"\bfb_ms=(\d+)", line)
        self.assertIsNotNone(fb_ms_match, line)
        # Behaviour, not a wall-clock race with the CI runner (it measured
        # 828, 901 and 1,107 ms here with the behaviour correct): the lock is
        # held for the WHOLE run, so a fallback that waited for it could only
        # end at the 2 s watchdog budget -- and `outcome=search-fallback` /
        # `fb_state=skipped-lock` above already prove it did not. This keeps
        # one generous ceiling, the watchdog budget itself, as the "did not
        # wait for the lock" bound.
        self.assertLess(int(fb_ms_match.group(1)), 2000, line)
        # The state file itself is genuinely untouched (the short-deadline
        # write really did skip, not silently succeed under a race).
        self.assertEqual(json.loads(state_file.read_text()), {})

    def _wait_for_fb_started_marker(self, appear: bool, timeout: float = 5.0) -> bool:
        """Poll for ANY `.fb-started.*` marker under this test's own
        MEMCONTINUUM_HOME (hooks/mc-watchdog.sh's own comment: written by
        the guarded hook for the duration of the search subprocess call
        only) -- `appear=True` waits for one to exist, `appear=False`
        waits for none to. The test's own process never knows the hook's
        real (multi-hop, re-exec'd-under-the-watchdog-launcher) pid in
        advance, so this globs rather than checking one exact name; a
        single-hook-run test never has more than one marker at a time."""
        pattern = str(Path(self.memtool_home) / ".fb-started.*")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            matches = glob.glob(pattern)
            if bool(matches) == appear:
                return True
            time.sleep(0.005)
        return False

    def _popen_hook(self, payload_text: str, env: dict):
        proc = subprocess.Popen(
            [MC_BASH, str(HOOK_SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
        )
        proc.stdin.write(payload_text)
        proc.stdin.close()
        # A later proc.communicate() (no `input=`) still tries to flush
        # and close stdin unconditionally when the attribute is not None
        # -- ValueError on an already-closed file. Already fed and
        # closed above; nothing left for communicate() to do with it.
        proc.stdin = None
        return proc

    def test_flock_contended_through_the_search_titling_skipped_gracefully(self):
        """Re-gate round 4 finding: the previous version of this pair's
        "written normally" test released its held lock at a fixed 0.05s
        -- well before the state write even starts (~0.30-0.45s into a
        real run) -- so it never contended at all; it passed unchanged
        against the OLD, unfixed 2.0s deadline too. This test makes the
        contention real and synchronized to the hook's ACTUAL progress,
        not a guessed wall-clock delay: hold the lock until the hook's
        own `.fb-started.*` marker (hooks/mc-watchdog.sh's own comment)
        is OBSERVED to appear (the search subprocess has just started),
        then hold a further ~0.7s -- comfortably past both the search
        itself (fts mode, well under 100ms) and the whole 0.25s state-
        write deadline that follows it -- before releasing. The titling
        write must have genuinely given up: `fb_state=skipped-lock`, the
        state file untouched, and the additionalContext guess unaffected."""
        session_id = "sess-flock-3"
        state_dir = Path(self.memtool_home) / "sessions" / self.project
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / f"{session_id}.json"
        state_file.write_text("{}")
        lock_file = state_dir / f"{session_id}.json.lock"
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            target = str(self.repo / "src" / "needle.py")
            proc = self._popen_hook(self._payload(target, session_id=session_id), self._env())
            self.assertTrue(
                self._wait_for_fb_started_marker(appear=True),
                "the .fb-started.* marker never appeared -- the search subprocess never ran",
            )
            time.sleep(0.7)
            stdout, stderr = proc.communicate(timeout=8.0)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        self.assertEqual(proc.returncode, 0, stderr)
        parsed = json.loads(stdout.strip())
        self.assertIn("TOP-7001", parsed["hookSpecificOutput"]["additionalContext"])
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback ", line)
        self.assertIn("fb_state=skipped-lock", line)
        self.assertEqual(json.loads(state_file.read_text()), {})

    def test_flock_briefly_contended_titling_still_written_normally(self):
        """Companion to the contended case above, synchronized the SAME
        way (not a guessed fixed delay): release the lock shortly (~0.05s)
        after the `.fb-started.*` marker is observed to appear -- by
        which point the (fts-mode, well under 100ms) search subprocess is
        still running or has just finished, and the lock is free well
        before the titling write's own first few ~20ms retry attempts
        would exhaust its 0.25s deadline. Pins that a brief, real
        contention window still lets the write land normally: no
        `fb_state=skipped-lock`, the state file genuinely populated."""
        session_id = "sess-flock-4"
        state_dir = Path(self.memtool_home) / "sessions" / self.project
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / f"{session_id}.json"
        state_file.write_text("{}")
        lock_file = state_dir / f"{session_id}.json.lock"
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            target = str(self.repo / "src" / "needle.py")
            proc = self._popen_hook(self._payload(target, session_id=session_id), self._env())
            self.assertTrue(
                self._wait_for_fb_started_marker(appear=True),
                "the .fb-started.* marker never appeared -- the search subprocess never ran",
            )
            time.sleep(0.05)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        stdout, stderr = proc.communicate(timeout=8.0)
        self.assertEqual(proc.returncode, 0, stderr)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback ", line)
        self.assertNotIn("fb_state=skipped-lock", line)
        state = json.loads(state_file.read_text())
        self.assertTrue(state.get("search_fallbacks"))

    @unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
    def test_ten_identical_misses_dedup_to_two_state_entries(self):
        """MAJOR fix-round item e, pinning: the SAME miss (one file, one
        hit id) run 10 times must leave exactly ONE entry for that
        (file, id) pair in session state, not 10 -- and running it
        against a SECOND file adds exactly one more (2 total), not 11."""
        session_id = "sess-dedup-1"
        target_a = str(self.repo / "src" / "needle.py")
        for _ in range(10):
            proc, _ = run_hook(self._payload(target_a, session_id=session_id), self._env())
            self.assertEqual(proc.returncode, 0, proc.stderr)

        target_b = str(self.repo / "src" / "needle_utils.py")
        proc, _ = run_hook(self._payload(target_b, session_id=session_id), self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        state_file = Path(self.memtool_home) / "sessions" / self.project / f"{session_id}.json"
        state = json.loads(state_file.read_text())
        fallbacks = state.get("search_fallbacks")
        self.assertEqual(len(fallbacks), 2, fallbacks)
        files = sorted(f["file"] for f in fallbacks)
        self.assertEqual(files, sorted([target_a, target_b]))

    def test_hybrid_envelope_dict_shape_is_parsed_not_treated_as_zero_hits(self):
        """Addendum (PR #21 merged): hybrid `search --json` now ALWAYS
        wraps hits in `{"results": [...], "fusion": ...}`, never a bare
        list -- this hook's own JSON parser must read `results` off the
        dict, not assume a list. `memidx.cmd_search` itself is
        monkeypatched (via memidx_wrapper_python's body-injection) to
        print exactly that envelope shape, argv-independent, pinning the
        PARSING rather than depending on a real embedding model to
        produce it."""
        wrapper = memidx_wrapper_python(
            self.tmp, "envelope-shape-search",
            'import json as _json\n'
            'def _fake_cmd_search(args):\n'
            '    print(_json.dumps({\n'
            '        "results": [{"id": "TOP-9222", "title": "Envelope shape topic", '
            '"path": "x.md", "chain_text": "TOP-9222 chain text here"}],\n'
            '        "fusion": "rrf",\n'
            '    }))\n'
            '    return 0\n'
            'memidx.cmd_search = _fake_cmd_search\n',
        )
        target = str(self.repo / "src" / "envelope.py")
        proc, _ = run_hook(self._payload(target), self._env(MEMCONTINUUM_PYTHON=str(wrapper)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TOP-9222", ctx)
        self.assertIn("chain text here", ctx)
        log_text = (Path(self.memtool_home) / "hook.log").read_text()
        line = self._last_outcome_line(log_text)
        self.assertIn("outcome=search-fallback ", line)
        self.assertIn("hits=1", line)


class TestF6RenderedTimeout(unittest.TestCase):
    """The OUTER Claude Code backstop: `code-root-filter-pair.json.tmpl`
    renders `"timeout": 5` on both the Edit and Write PreToolUse command
    entries pre-edit-chain.sh receives -- unaffected by whatever the inner
    watchdog measurement below produces (F6, external-review fix round)."""

    def test_template_carries_timeout_5(self):
        text = (TOOLS_DIR / "templates" / "code-root-filter-pair.json.tmpl").read_text()
        self.assertEqual(text.count('"timeout": 5'), 2)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreEditChainWatchdog(unittest.TestCase):
    """F6 (external-review fix round, coordinator ruling 67 + "Also binding
    from Codex"): pre-edit-chain.sh now runs under hooks/mc-watchdog.sh's
    own guard. WATCHDOG_BUDGET_S mirrors the unmodified default budget
    (MC_WATCHDOG_BUDGET is not overridden in hooks/pre-edit-chain.sh -- see
    its own header comment) -- confirmed, not assumed, against a real
    measurement of this hook's actual wired command line on three live
    stores: the engine's own, plus two other real, live projects, one of
    them hosted entirely on a slow drvfs (/mnt/c) mount, code root and
    store both. 34 timed samples, overall p95=0.198s / p99=0.206s /
    max=0.206s -- roughly 10x headroom under the 2s default, so it is kept
    rather than tightened or loosened. See this task's own report for the
    full per-store table."""

    WATCHDOG_BUDGET_S = 2.0

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-preedit-watchdog-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

    def _hang_python(self, name):
        hang_py = Path(self.td) / name
        hang_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *MC_WATCHDOG_LAUNCHER*) exec \"" + VENV_PYTHON + "\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            "sleep 6\n"
        )
        hang_py.chmod(0o755)
        return hang_py

    def test_a_hung_python_is_killed_within_budget_and_hook_exits_0(self):
        home = Path(self.td) / "home"; home.mkdir()
        # The `[ ! -f "$DB_PATH" ]` index-missing gate runs BEFORE any
        # for-path call -- an empty home would exit that gate in
        # milliseconds without ever reaching hang_py, which would falsely
        # look like this test passes for the wrong reason. A real (if
        # empty) db file lets the hang actually happen where for-path is
        # called, under the watchdog's own guard.
        (home / "wdtest.sqlite").touch()
        hang_py = self._hang_python("hang-python")
        env = clean_env(MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_PYTHON=str(hang_py),
                         MEMCONTINUUM_PROJECT="wdtest", MEMCONTINUUM_ROOT=str(SCHEMA_FIXTURE_ROOT))
        payload = json.dumps({
            "session_id": "s-preedit-wd", "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "cwd": str(SCHEMA_FIXTURE_ROOT),
            "tool_input": {"file_path": str(SCHEMA_FIXTURE_ROOT / "x.py")},
        })
        proc, elapsed = run_hook(payload, env, timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        deadline = self.WATCHDOG_BUDGET_S + 1.0  # generous slack for process-start overhead
        self.assertLess(elapsed, deadline,
                         f"took {elapsed:.3f}s -- the {self.WATCHDOG_BUDGET_S}s watchdog budget must bound this")
        log_text = (home / "hook.log").read_text()
        self.assertIn("outcome=watchdog-killed", log_text)
        self.assertIn("hook=pre-edit-chain.sh", log_text)

    def test_timeout_emits_a_minimal_valid_additional_context_not_silence(self):
        # Codex's addition: today the launcher exits 0 with EMPTY stdout on
        # timeout -- Claude Code then reads that as "retrieval ran and
        # found nothing," indistinguishable from a genuine no-match. A
        # timeout must produce a real, valid, honest uncertainty signal.
        home = Path(self.td) / "home2"; home.mkdir()
        (home / "wdtest.sqlite").touch()
        hang_py = self._hang_python("hang-python2")
        env = clean_env(MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_PYTHON=str(hang_py),
                         MEMCONTINUUM_PROJECT="wdtest", MEMCONTINUUM_ROOT=str(SCHEMA_FIXTURE_ROOT))
        payload = json.dumps({
            "session_id": "s-preedit-wd2", "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "cwd": str(SCHEMA_FIXTURE_ROOT),
            "tool_input": {"file_path": str(SCHEMA_FIXTURE_ROOT / "x.py")},
        })
        proc, elapsed = run_hook(payload, env, timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "a timeout must not leave stdout empty")
        payload_out = json.loads(proc.stdout)
        ctx = payload_out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("timed out", ctx.lower())
        self.assertIn("not established", ctx.lower())


ORACLE_COMMIT = "0732ac4"


def _oracle_commit_available() -> bool:
    """A shallow CI checkout (actions/checkout's default fetch-depth: 1)
    only has the tip commit -- once this round's own commit lands,
    ORACLE_COMMIT is its PARENT and absent from history entirely. `git
    show`/`cat-file` on a missing object fails outright rather than
    returning something empty, so this must be checked BEFORE setUpClass
    ever calls `git show`, not caught there."""
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{ORACLE_COMMIT}^{{commit}}"],
            cwd=str(TOOLS_DIR), capture_output=True, check=True,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


_ORACLE_COMMIT_AVAILABLE = _oracle_commit_available()
_SKIP_NO_ORACLE_COMMIT = (
    f"commit {ORACLE_COMMIT} is not present in this checkout's history "
    "(a shallow clone only fetches the tip commit) -- the oracle-parity "
    "check needs the actual pre-round-5 script, not a reimplementation, "
    "so it skips rather than fabricating one"
)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
@unittest.skipUnless(_ORACLE_COMMIT_AVAILABLE, _SKIP_NO_ORACLE_COMMIT)
class TestPreEditChainOracleParity(unittest.TestCase):
    """Round 5 rewrote pre-edit-chain.sh's candidate loop and chain-text
    fetch to make ONE `for-path` call (--with-chain-text) and ONE parser
    per hook run instead of two calls and three parsers. This class
    proves the rewrite is behavior-preserving: the oracle is the REAL
    pre-round-5 script -- the committed ORACLE_COMMIT blob, copied to a
    temp file, not a reimplementation of its logic -- run against the
    exact same inputs as the current script. Only the outcome NAME is
    compared out of hook.log (never the full line: elapsed=/timestamp
    naturally differ run to run).

    Round 6 fixed a real bug the frozen oracle still has: its
    "Decision-chain memory: N topic(s) reference this file." header
    always said "1" for ANY match, because the old `grep -c '"id":'`
    counted matching LINES of a one-line JSON dump (always exactly one
    line), never a real per-topic count. The current script now computes
    a real count of the DISTINCT topics whose chains actually get
    rendered (see hooks/pre-edit-chain.sh's own comment at the
    `topic_ids` loop) -- so on a multi-topic/concept match the oracle and
    the current script now legitimately print DIFFERENT header counts,
    by design, not by regression.
    `_assert_stdout_matches_except_topic_count` below normalises only
    that one digit before comparing, so every other byte of
    additionalContext (chain text, citation
    reminder, JSON structure) still has to match exactly."""

    _TOPIC_COUNT_HEADER_RE = re.compile(
        r"Decision-chain memory: \d+ topic\(s\) reference this file\."
    )

    def _assert_stdout_matches_except_topic_count(self, new_stdout, oracle_stdout):
        def _normalized(text):
            return self._TOPIC_COUNT_HEADER_RE.sub(
                "Decision-chain memory: N topic(s) reference this file.", text
            )

        self.assertEqual(
            _normalized(new_stdout), _normalized(oracle_stdout),
            "byte-identical stdout (topic-count header normalised -- round 6 "
            "made it a real count, so it may legitimately differ from the "
            "frozen pre-round-5 oracle's always-1 quirk)",
        )

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="memcontinuum-hook-oracle-test-")
        oracle_text = subprocess.run(
            ["git", "show", f"{ORACLE_COMMIT}:hooks/pre-edit-chain.sh"],
            cwd=str(TOOLS_DIR), capture_output=True, text=True, check=True,
        ).stdout
        # The oracle script computes MEMIDX/mc-watchdog.sh relative to its
        # OWN location ($SCRIPT_DIR/../memidx.py, $SCRIPT_DIR/mc-
        # watchdog.sh) -- it needs a sibling layout to resolve, not a bare
        # standalone file. Symlinked at the real (current) memidx.py and
        # mc-watchdog.sh: memidx.py's own for-path/--with-chain-text
        # change is additive (the pre-existing --json shape and the
        # two-call sequence the oracle script itself drives are both
        # unchanged), so this is exactly "the pre-round-5 script talking
        # to the same engine", not a different oracle.
        oracle_repo = Path(cls.tmp) / "oracle-repo"
        (oracle_repo / "hooks").mkdir(parents=True)
        (oracle_repo / "memidx.py").symlink_to(TOOLS_DIR / "memidx.py")
        (oracle_repo / "hooks" / "mc-watchdog.sh").symlink_to(TOOLS_DIR / "hooks" / "mc-watchdog.sh")
        cls.oracle_script = oracle_repo / "hooks" / "pre-edit-chain.sh"
        cls.oracle_script.write_text(oracle_text)
        cls.oracle_script.chmod(0o755)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _new_home(self, project):
        home = Path(self.tmp) / f"home-{project}"
        home.mkdir(exist_ok=True)
        return home

    def _run(self, script_path, payload_text, env, timeout=10.0):
        return subprocess.run(
            [MC_BASH, str(script_path)], input=payload_text,
            capture_output=True, text=True, env=env, timeout=timeout,
        )

    def _outcome_name(self, log_slice):
        matches = re.findall(r"outcome=(\S+)", log_slice)
        return matches[-1] if matches else None

    def _run_pair(self, home, payload, env_overrides):
        """Runs the oracle then the current script against the SAME
        MEMCONTINUUM_HOME/db (read-only queries, so no risk of one run's
        state affecting the other) and returns
        (oracle_proc, new_proc, oracle_outcome, new_outcome)."""
        log_path = home / "hook.log"
        env = clean_env(MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_PYTHON=VENV_PYTHON, **env_overrides)

        before = log_path.read_text() if log_path.exists() else ""
        oracle_proc = self._run(self.oracle_script, payload, env)
        after_oracle = log_path.read_text()
        oracle_outcome = self._outcome_name(after_oracle[len(before):])

        new_proc = self._run(HOOK_SCRIPT, payload, env)
        after_new = log_path.read_text()
        new_outcome = self._outcome_name(after_new[len(after_oracle):])

        return oracle_proc, new_proc, oracle_outcome, new_outcome

    def _matching_payload(self):
        return json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "cwd": "/some/other/unrelated/dir",
            "tool_input": {"file_path": "/fake/repo/src/core/scan/scan_plan.py"},
        })

    def _nonmatching_payload(self):
        return json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "cwd": "/nowhere",
            "tool_input": {"file_path": "/nowhere/near/anything.py"},
        })

    def _reindex(self, root, project, db):
        args = type(
            "Args", (), dict(root=str(root), project=project, db=str(db), full=True, no_embed=True),
        )()
        memidx.cmd_reindex(args)

    def _topic_with_code_ref(self, tid: str, code_ref: str) -> str:
        return (
            f"---\ntype: topic\nid: {tid}\ntitle: T-{tid}\n"
            f"code_refs:\n  - {code_ref}\n"
            "links:\n"
            '  - link: L1\n    status: active\n    ruling: {text: "r", authority: owner-verbatim, source: s}\n'
            "---\nBody.\n"
        )

    def _topic_with_nul_in_ruling(self, tid: str, code_ref: str) -> str:
        """Round 7 red test fixture: a YAML double-quoted `\\0` escape
        decodes (PyYAML) to a real NUL byte in the ruling text -- the
        rendered chain line then embeds that NUL, verbatim, in the middle
        of the plain-text chain_text this topic contributes."""
        return (
            f"---\ntype: topic\nid: {tid}\ntitle: T-{tid}\n"
            f"code_refs:\n  - {code_ref}\n"
            "links:\n"
            '  - link: L1\n    status: active\n    ruling: {text: "before\\0after", authority: owner-verbatim, source: s}\n'
            "---\nBody.\n"
        )

    def test_match_with_a_chain(self):
        project = "oracle-match"
        home = self._new_home(project)
        self._reindex(SCHEMA_FIXTURE_ROOT, project, home / f"{project}.sqlite")

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/"),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertTrue(oracle_proc.stdout.strip())
        self._assert_stdout_matches_except_topic_count(new_proc.stdout, oracle_proc.stdout)
        # The topic-count normalisation above widens the comparison from
        # a literal byte match -- pin the one-topic count explicitly here
        # so this scenario still proves "1", not just "whatever number
        # both scripts happen to agree on".
        self.assertIn("Decision-chain memory: 1 topic(s) reference this file.", new_proc.stdout)
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "matched")

    def test_match_with_two_topics_gets_a_real_topic_count(self):
        """Round 6 red test: a store where TWO topics both reference the
        same file is the one scenario that distinguishes a real per-topic
        COUNT from the old `grep -c '"id":'` quirk -- the other scenarios
        (one topic, no concepts) all print "1" either way and can't tell
        the difference. The frozen oracle (0732ac4) still has the old bug
        (`grep -c` counts matching LINES of a one-line JSON dump, always
        exactly one line, so its header always says "1" no matter how many
        topics matched) -- that is now an EXPECTED divergence from the
        current script, not a parity failure, so the raw byte-for-byte
        stdout comparison every other scenario in this class uses is
        replaced here with `_assert_stdout_matches_except_topic_count`,
        which normalises away only the header's digit before comparing
        everything else (chain text, citation reminder, JSON structure)."""
        project = "oracle-two-topics"
        home = self._new_home(project)
        root = Path(self.tmp) / f"{project}-store"
        (root / "topics").mkdir(parents=True)
        (root / "topics" / "one.md").write_text(
            self._topic_with_code_ref("TOP-2001", "src/core/scan/scan_plan.py")
        )
        (root / "topics" / "two.md").write_text(
            self._topic_with_code_ref("TOP-2002", "src/core/scan/scan_plan.py")
        )
        self._reindex(root, project, home / f"{project}.sqlite")

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/"),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertTrue(oracle_proc.stdout.strip())
        self._assert_stdout_matches_except_topic_count(new_proc.stdout, oracle_proc.stdout)
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "matched")
        # The oracle still has the old quirk: two topics matched, header
        # still says "1" -- exactly what 0732ac4 always said.
        self.assertIn("Decision-chain memory: 1 topic(s) reference this file.", oracle_proc.stdout)
        # The current script computes a real count: two topics matched,
        # header says "2" -- this is the fix this round makes.
        self.assertIn("Decision-chain memory: 2 topic(s) reference this file.", new_proc.stdout)

    def test_nul_in_ruling_text_does_not_truncate_or_drop_later_topics(self):
        """Round 7 red test (Codex MAJOR, hooks/pre-edit-chain.sh's own
        candidate-loop parser): chain_text crosses from memidx.py into
        this hook through a chr(0)-delimited stream, read back with
        `read -d ''` -- which treats ANY NUL byte as the end of the
        CURRENT field, not just the delimiter this loop itself appends.
        A record whose decoded ruling text embeds a real NUL (YAML
        `"before\\0after"`) used to truncate chain_text right there,
        silently dropping every topic whose rendered chain came after it
        in the SAME chain_text string -- even though the header (computed
        separately, from the untruncated `results` list) still reported
        the full topic count. The frozen pre-round-5 oracle never hit
        this: its own transport was three separate `$(...)` command
        substitutions, and plain command substitution in bash silently
        DROPS an embedded NUL byte from captured output rather than
        truncating the surrounding text -- so the oracle is also the
        correct-behavior reference here, not just a parity fixture: BOTH
        scripts must retain both topics and the concatenated
        "beforeafter" text (NUL dropped, nothing lost), not merely agree
        with each other."""
        project = "oracle-embedded-nul"
        home = self._new_home(project)
        root = Path(self.tmp) / f"{project}-store"
        (root / "topics").mkdir(parents=True)
        (root / "topics" / "one.md").write_text(
            self._topic_with_nul_in_ruling("TOP-3001", "src/core/scan/scan_plan.py")
        )
        (root / "topics" / "two.md").write_text(
            self._topic_with_code_ref("TOP-3002", "src/core/scan/scan_plan.py")
        )
        self._reindex(root, project, home / f"{project}.sqlite")

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/"),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "matched")
        self._assert_stdout_matches_except_topic_count(new_proc.stdout, oracle_proc.stdout)
        for label, stdout in (("oracle", oracle_proc.stdout), ("new", new_proc.stdout)):
            with self.subTest(label):
                self.assertIn("beforeafter", stdout)
                self.assertIn("TOP-3001", stdout)
                self.assertIn("TOP-3002", stdout)
        self.assertIn("Decision-chain memory: 2 topic(s) reference this file.", new_proc.stdout)

    def test_ungoverned_concept_multiline_owner_boundary_trailing_newline_parity(self):
        """Round 7 red test (Codex MINOR): for_path_chain_lines' concept
        line embeds owner_boundary verbatim -- f"{id} {title} -- {boundary}".
        A YAML `|` block scalar clips to exactly one trailing newline, so
        an UNGOVERNED concept (no governed_by topics, so this is the
        only/last chain_text line) makes chain_text itself end in a
        newline. The frozen pre-round-5 oracle captured chain_text
        through a `$(...)` command substitution, which strips every
        trailing newline unconditionally; the round-5 transport (read
        -d '' off a chr(0)-delimited stream) preserves it instead, so the
        final "\\n\\n".join(...) in the OUTPUT_JSON assembly step below
        inserted an extra blank line before CONSTRAINT that the oracle
        never produced. Byte-identical parity (topic-count header
        normalised only, same as every other case in this class) is the
        bar."""
        project = "oracle-concept-boundary-newline"
        home = self._new_home(project)
        root = Path(self.tmp) / f"{project}-store"
        (root / "concepts").mkdir(parents=True)
        (root / "concepts" / "c1.md").write_text(
            "---\ntype: concept\nid: CPT-1\ntitle: C-CPT-1\n"
            "implemented_by:\n  - src/core/scan/scan_plan.py\n"
            "owner_boundary: |\n  line one\n  line two\n"
            "---\nBody.\n"
        )
        self._reindex(root, project, home / f"{project}.sqlite")

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/"),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertTrue(oracle_proc.stdout.strip())
        self._assert_stdout_matches_except_topic_count(new_proc.stdout, oracle_proc.stdout)
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "matched")
        self.assertIn("line one\\nline two", new_proc.stdout)

    def test_no_match(self):
        """Search-fallback (TOP-0133 L1) is an EXPECTED divergence from the
        frozen oracle here, the same documented-divergence pattern
        test_match_with_two_topics_gets_a_real_topic_count already
        established for the topic-count fix: the oracle predates this
        feature and still `finish`es plain `no-match`; the current script
        instead runs the fallback search on the path's own words
        ("near anything" -- /nowhere/near/anything.py, cwd /nowhere) and
        finds nothing in SCHEMA_FIXTURE_ROOT, so BOTH scripts still agree
        on empty stdout (no chain, no fallback content either) -- only the
        logged outcome NAME differs, and only in the direction this
        feature intends."""
        project = "oracle-nomatch"
        home = self._new_home(project)
        self._reindex(SCHEMA_FIXTURE_ROOT, project, home / f"{project}.sqlite")

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._nonmatching_payload(), dict(MEMCONTINUUM_PROJECT=project),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertEqual(new_proc.stdout.strip(), oracle_proc.stdout.strip())
        self.assertEqual(new_proc.stdout.strip(), "")
        self.assertEqual(oracle_outcome, "no-match")
        self.assertEqual(new_outcome, "search-fallback-empty")

    def test_stale_index_with_root(self):
        project = "oracle-stale"
        home = self._new_home(project)
        root = Path(self.tmp) / f"{project}-store"
        shutil.copytree(SCHEMA_FIXTURE_ROOT, root)
        self._reindex(root, project, home / f"{project}.sqlite")
        (root / "topics" / "new-topic.md").write_text(
            "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
        )

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
                 MEMCONTINUUM_ROOT=str(root)),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertTrue(oracle_proc.stdout.strip())
        self._assert_stdout_matches_except_topic_count(new_proc.stdout, oracle_proc.stdout)
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "index-stale-served")

    def test_quarantined_index(self):
        project = "oracle-quarantine"
        home = self._new_home(project)
        root = Path(self.tmp) / f"{project}-store"
        shutil.copytree(SCHEMA_FIXTURE_ROOT, root)
        (root / "topics" / "bad.md").write_text(
            "---\ntype: topic\nid: TOP-9999\ntitle: Bad\nlinks: [\n---\nBody.\n"
        )
        db = home / f"{project}.sqlite"
        self._reindex(root, project, db)
        self.assertEqual(memidx.decision_index_state(db, project, root=root), "quarantined")

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
                 MEMCONTINUUM_ROOT=str(root)),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertTrue(oracle_proc.stdout.strip())
        self._assert_stdout_matches_except_topic_count(new_proc.stdout, oracle_proc.stdout)
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "matched")

    def test_missing_index(self):
        project = "oracle-missing"
        home = self._new_home(project)
        # no db ever created for this project -- both scripts' own
        # pre-loop [ ! -f "$DB_PATH" ] check must catch it before any
        # `for-path` call is attempted.

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/"),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertEqual(new_proc.stdout.strip(), oracle_proc.stdout.strip())
        self.assertEqual(new_proc.stdout.strip(), "")
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "index-missing")

    def test_index_error_rc4(self):
        project = "oracle-indexerror"
        home = self._new_home(project)
        db = home / f"{project}.sqlite"
        self._reindex(SCHEMA_FIXTURE_ROOT, project, db)
        conn = sqlite3.connect(str(db))
        conn.execute("ALTER TABLE records RENAME COLUMN path TO path_broken")
        conn.commit(); conn.close()

        def _restore():
            c = sqlite3.connect(str(db))
            c.execute("ALTER TABLE records RENAME COLUMN path_broken TO path")
            c.commit(); c.close()

        self.addCleanup(_restore)

        oracle_proc, new_proc, oracle_outcome, new_outcome = self._run_pair(
            home, self._matching_payload(),
            dict(MEMCONTINUUM_PROJECT=project, MEMCONTINUUM_STRIP_PREFIX="/fake/repo/"),
        )
        self.assertEqual(oracle_proc.returncode, 0, oracle_proc.stderr)
        self.assertEqual(new_proc.returncode, 0, new_proc.stderr)
        self.assertEqual(new_proc.stdout.strip(), oracle_proc.stdout.strip())
        self.assertEqual(new_proc.stdout.strip(), "")
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "index-error")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreEditChainTopicsLogging(unittest.TestCase):
    """eval-topic-logging: a `matched`/`index-stale-served` hook.log line now
    names WHICH topic ids were actually injected (`topics=<comma-separated,
    sorted ids>`), turning a matched edit into a gradeable eval sample (which
    decisions were shown, not just that something matched). Field order is
    `elapsed=Ns topics=... project=P file=F` -- topics= sits between
    elapsed= and project=, so project=/file= keep their existing position for
    memidx.py's own parser. Capped at 10 ids (`,+N` beyond that) so a
    pathological multi-topic file can't blow up hook.log with one line."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="memcontinuum-hook-topics-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _matching_payload(self):
        return json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "cwd": "/some/other/unrelated/dir",
            "tool_input": {"file_path": "/fake/repo/src/core/scan/scan_plan.py"},
        })

    def _nonmatching_payload(self):
        return json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "Edit",
            "cwd": "/nowhere",
            "tool_input": {"file_path": "/nowhere/near/anything.py"},
        })

    def _topic_with_code_ref(self, tid: str, code_ref: str) -> str:
        return (
            f"---\ntype: topic\nid: {tid}\ntitle: T-{tid}\n"
            f"code_refs:\n  - {code_ref}\n"
            "links:\n"
            '  - link: L1\n    status: active\n    ruling: {text: "r", authority: owner-verbatim, source: s}\n'
            "---\nBody.\n"
        )

    def _build_home(self, project: str, topic_ids):
        root = Path(self.tmp) / f"{project}-store"
        (root / "topics").mkdir(parents=True)
        for i, tid in enumerate(topic_ids):
            (root / "topics" / f"t{i}.md").write_text(
                self._topic_with_code_ref(tid, "src/core/scan/scan_plan.py")
            )
        home = Path(self.tmp) / f"home-{project}"
        home.mkdir()
        args = type(
            "Args", (), dict(
                root=str(root), project=project, db=str(home / f"{project}.sqlite"),
                full=True, no_embed=True,
            ),
        )()
        memidx.cmd_reindex(args)
        return home

    def _run(self, home, project, payload=None, **env_overrides):
        env = clean_env(
            MEMCONTINUUM_HOME=str(home),
            MEMCONTINUUM_PROJECT=project,
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
            MEMCONTINUUM_STRIP_PREFIX="/fake/repo/",
            **env_overrides,
        )
        return run_hook(payload or self._matching_payload(), env)

    def _last_outcome_line(self, home):
        log_text = (home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "outcome=" in l]
        self.assertTrue(matching, log_text)
        return matching[-1]

    def test_single_topic_logs_its_id_between_elapsed_and_project(self):
        project = "topics-one"
        home = self._build_home(project, ["TOP-9001"])
        proc, _elapsed = self._run(home, project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_outcome_line(home)
        self.assertIn("outcome=matched", line)
        self.assertRegex(
            line, r"elapsed=\d+s topics=TOP-9001 project=\S+ file=",
            line,
        )

    def test_backward_clock_never_produces_a_negative_elapsed(self):
        """Fix round (Codex 6 / Grok 6): a fake `date +%s` that steps
        BACKWARDS between START_TS and finish()'s own `now` reproduces the
        real CI flake (a negative elapsed) deterministically, no reliance
        on an actual NTP correction firing mid-run. Every `date +%s` call
        after the first one here returns the SAME stepped-back value (not
        just the second), so this is robust regardless of how many such
        calls this script makes before finish()."""
        project = "elapsed-clamp"
        home = self._build_home(project, ["TOP-9001"])
        marker = Path(self.tmp) / "date-called-once"
        real_date = shutil.which("date") or "/bin/date"
        fake_bin = Path(self.tmp) / "fakebin"
        fake_bin.mkdir()
        fake_date = fake_bin / "date"
        fake_date.write_text(
            "#!/usr/bin/env bash\n"
            'if [ "$1" = "+%s" ]; then\n'
            f'    if [ -e "{marker}" ]; then\n'
            '        echo 1000000100\n'
            '    else\n'
            f'        : > "{marker}"\n'
            '        echo 1000000200\n'
            '    fi\n'
            '    exit 0\n'
            'fi\n'
            f'exec "{real_date}" "$@"\n'
        )
        fake_date.chmod(0o755)
        proc, _elapsed = self._run(
            home, project,
            PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Grok 4 (fix round 2, external re-gate): the two assertions below
        # this one used to be the ONLY assertions, and both would also pass
        # on a genuinely fast real clock that never went through the fake
        # `date` at all (a sub-second real run also logs "elapsed=0s" and
        # never contains "elapsed=-") -- so a silent failure of the PATH
        # override (the hook calling `date` by an absolute path, say) would
        # give a false pass instead of a loud one. The marker proves the
        # fake `date +%s` was actually invoked at least twice (once to
        # create it and return START_TS, once more -- after it exists -- to
        # return the stepped-back value finish() sees), i.e. that the
        # backward step was genuinely exercised, not just that the hook
        # happened to run quickly.
        self.assertTrue(marker.exists(),
                         "fake date was never called a second time -- the backward-clock "
                         "scenario was not exercised, this test proves nothing")
        line = self._last_outcome_line(home)
        self.assertRegex(line, r"elapsed=\d+s", line)
        self.assertNotIn("elapsed=-", line)
        # And the clamp's own arithmetic: 1000000100 - 1000000200 = -100
        # unclamped, so a genuine clamp must show exactly 0, not merely
        # "some non-negative number" (which a fast real run would also
        # produce, coincidentally, even with no fake date involved at all).
        self.assertRegex(line, r"elapsed=0s", line)

    def test_two_topics_logged_sorted_comma_separated(self):
        project = "topics-two"
        # deliberately out of sorted order on disk
        home = self._build_home(project, ["TOP-9002", "TOP-9001"])
        proc, _elapsed = self._run(home, project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_outcome_line(home)
        self.assertIn("topics=TOP-9001,TOP-9002", line)

    def test_no_match_never_logs_a_topics_field(self):
        """Search-fallback (TOP-0133 L1): this branch's `finish` outcome is
        now `search-fallback-empty` (the fixture's single topic has no
        content overlapping "near"/"anything", the fallback query for
        /nowhere/near/anything.py) -- `topics=` is a pre-edit-chain match/
        index-stale-served-only field either way, so the real assertion
        this test exists for (no topics= field on a non-match) still
        holds."""
        project = "topics-none"
        home = self._build_home(project, ["TOP-9001"])
        proc, _elapsed = self._run(home, project, payload=self._nonmatching_payload())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_outcome_line(home)
        self.assertIn("outcome=search-fallback-empty", line)
        self.assertNotIn("topics=", line)

    def test_other_failure_outcomes_never_log_a_topics_field(self):
        """no-file-path never even reaches the candidate loop -- topics=
        must not appear there either."""
        home = Path(self.tmp) / "home-nofile"
        home.mkdir()
        env = clean_env(
            MEMCONTINUUM_HOME=str(home),
            MEMCONTINUUM_PROJECT="topics-nofile",
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        proc, _elapsed = run_hook("{}", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_outcome_line(home)
        self.assertIn("outcome=no-file-path", line)
        self.assertNotIn("topics=", line)

    def test_pathological_many_topics_caps_at_ten_with_plus_n_marker(self):
        project = "topics-forty"
        ids = [f"TOP-{9100 + i}" for i in range(40)]
        home = self._build_home(project, ids)
        proc, _elapsed = self._run(home, project)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_outcome_line(home)
        self.assertIn("outcome=matched", line)
        m = re.search(r"topics=(\S+)", line)
        self.assertIsNotNone(m, line)
        parts = m.group(1).split(",")
        self.assertEqual(len(parts), 11, parts)  # 10 ids + one "+N" marker
        self.assertEqual(parts[-1], "+30", parts)
        self.assertEqual(parts[:-1], sorted(ids)[:10], parts)
        self.assertLess(
            len(line.encode()), 500,
            f"a 40-topic match must not blow up hook.log with one huge line: {len(line)} bytes",
        )

    def test_stale_served_outcome_also_carries_topics(self):
        """index-stale-served shares the same matched-candidate path as
        `matched` -- it must carry topics= too, not just the plain-matched
        outcome."""
        root = Path(self.tmp) / "stale-store"
        (root / "topics").mkdir(parents=True)
        (root / "topics" / "one.md").write_text(
            self._topic_with_code_ref("TOP-9500", "src/core/scan/scan_plan.py")
        )
        home = Path(self.tmp) / "home-stale"
        home.mkdir()
        project = "topics-stale"
        args = type(
            "Args", (), dict(
                root=str(root), project=project, db=str(home / f"{project}.sqlite"),
                full=True, no_embed=True,
            ),
        )()
        memidx.cmd_reindex(args)
        (root / "topics" / "new-topic.md").write_text(
            "---\ntype: topic\nid: TOP-NEW\ntitle: New\nlinks: []\n---\nBody.\n"
        )
        proc, _elapsed = self._run(home, project, MEMCONTINUUM_ROOT=str(root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_outcome_line(home)
        self.assertIn("outcome=index-stale-served", line)
        self.assertIn("topics=TOP-9500", line)


POST_COMMIT_HOOK = TOOLS_DIR / "hooks" / "post-commit-reindex.sh"


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPostCommitReindexHook(unittest.TestCase):
    def test_bash_syntax_is_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(POST_COMMIT_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_root_not_set_skip_line_carries_project(self):
        """Round-2 review finding: this fail-open skip line (no
        MEMCONTINUUM_ROOT -- the hook can't derive PROJECT the normal way,
        since basename(MEMCONTINUUM_ROOT) needs a ROOT it doesn't have)
        used to log with no project= at all -- "every hook.log line
        carries project=" was still false for it. Falls back to
        MEMCONTINUUM_PROJECT/"default", same as every other hook's
        fallback chain."""
        tmp = tempfile.mkdtemp(prefix="memcontinuum-postcommit-project-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        env = clean_env(HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_PROJECT="pc-proj")
        env.pop("MEMCONTINUUM_ROOT", None)
        proc = subprocess.run([MC_BASH, str(POST_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("MEMCONTINUUM_ROOT not set, skipping", log_text)
        self.assertIn("project=pc-proj", log_text)

    def test_root_not_set_skip_line_defaults_project_when_unset(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-postcommit-project-default-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        env = clean_env(HOME=str(tmp), MEMCONTINUUM_HOME=str(home))
        env.pop("MEMCONTINUUM_ROOT", None)
        env.pop("MEMCONTINUUM_PROJECT", None)
        proc = subprocess.run([MC_BASH, str(POST_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("project=default", log_text)

    def test_r2_resolves_python_via_pointer_config_at_custom_home(self):
        """R2 regression, round 4 gate: see TestPreEditChainHook's twin --
        same pointer-then-follow-through chain, this time for the store's
        git post-commit hook. With no MEMCONTINUUM_PYTHON/HOME in the
        environment and no engine .venv, only the REAL config.sh at the
        pointed-at custom home can resolve a working python; the old
        single-source step stopped at the pointer and never found it, so
        the reindex silently failed (logged, never blocking the commit --
        but the index then never actually updates)."""
        if (TOOLS_DIR / ".venv" / "bin" / "python").exists():
            self.skipTest(
                "an engine .venv exists in this checkout (INC-0125) -- this "
                "test can only prove python resolves through the "
                "config-pointer chain when no engine venv exists to mask a "
                "broken chain; see tests/test_env_gate.py"
            )
        tmp = tempfile.mkdtemp(prefix="memcontinuum-postcommit-test-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        fake_home = Path(tmp) / "fake-home"
        default_mc_home = fake_home / ".memcontinuum"
        default_mc_home.mkdir(parents=True)
        custom_home = Path(tmp) / "custom-mc-home"
        custom_home.mkdir()
        (default_mc_home / "config.sh").write_text(f"MEMCONTINUUM_HOME='{custom_home}'\n")
        (custom_home / "config.sh").write_text(
            f"MEMCONTINUUM_PYTHON='{VENV_PYTHON}'\nMEMCONTINUUM_HOME='{custom_home}'\n"
        )

        store_root = Path(tmp) / "store"
        (store_root / "topics").mkdir(parents=True)
        (store_root / "topics" / "T-0001.md").write_text(
            "---\nid: T-0001\ntitle: Test\nstatus: active\n---\nbody\n"
        )

        # Design R8: this store's one topic has no vector yet, so the
        # content pass leaves E>0 and the hook would otherwise spawn a
        # REAL background embed-worker (a real python resolved via the
        # pointer chain, genuinely embedding) that would outlive this
        # test. MEMCONTINUUM_EMBED_WORKER=0 disables the spawn -- the
        # marker is still touched, the content pass (this test's actual
        # subject) is unaffected.
        env = clean_env(HOME=str(fake_home), MEMCONTINUUM_ROOT=str(store_root), MEMCONTINUUM_PROJECT="pc-test",
                         MEMCONTINUUM_EMBED_WORKER="0")
        env.pop("MEMCONTINUUM_HOME", None)
        env.pop("MEMCONTINUUM_PYTHON", None)
        proc = subprocess.run(
            [MC_BASH, str(POST_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            (custom_home / "pc-test.sqlite").exists(),
            "the reindex must have actually run against a python resolved via the pointer chain",
        )
        log_text = (custom_home / "hook.log").read_text()
        self.assertIn("rc=0", log_text, log_text)
        self.assertFalse((default_mc_home / "pc-test.sqlite").exists())
        self.assertFalse(
            (custom_home / "pc-test.embed.lock").exists(),
            "no worker was ever spawned (MEMCONTINUUM_EMBED_WORKER=0) -- no lock file either",
        )


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPostCommitReindexEmbedWorker(unittest.TestCase):
    """Design R8 (audit MC-P2-02, TOP-0123 L7): the redesigned hook runs a
    bounded content-only pass (`reindex --no-embed --auto`, through the
    shared watchdog launcher) and, only when rows are left without a fresh
    vector, touches a marker and spawns a detached `embed-worker`. Every
    test here sets a temp MEMCONTINUUM_HOME and MEMCONTINUUM_EMBED_WORKER=0
    unless it is specifically testing the spawn (test_h below)."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-postcommit-embed-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.home = Path(self.td) / "home"
        self.home.mkdir()

    def tearDown(self):
        # No test here may leave a real embed-worker holding its lock.
        lock_paths = list(self.home.glob("*.embed.lock")) if self.home.exists() else []
        for lock_path in lock_paths:
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
            except BlockingIOError:
                self.fail(f"{lock_path} is still held by a worker after the test")
            finally:
                os.close(fd)

    def _store_with_one_unembedded_topic(self, project="pc-embed"):
        # Same minimal frontmatter shape TestPostCommitReindexHook's own
        # test_r2_resolves_python_via_pointer_config_at_custom_home
        # already proves reindexes cleanly (id/title/status, no type/area).
        root = Path(self.td) / f"{project}-store"
        (root / "topics").mkdir(parents=True)
        (root / "topics" / "T-0001.md").write_text(
            "---\nid: T-0001\ntitle: Widget cache\nstatus: active\n---\n"
            "the widget cache invalidates on write\n"
        )
        return root

    def _wrapper_python(self, name, body):
        """A bash impersonation of MEMCONTINUUM_PYTHON: the module-level
        `memidx_wrapper_python` dispatch idiom, extended to intercept BOTH
        `-c` shapes this hook now uses -- the watchdog launcher's own `-c
        "$MC_WATCHDOG_LAUNCHER_PY"` call AND the hook's own worker-spawn
        `-c '...Popen(...)...'` call -- passed straight through to the
        real venv python unmodified (both need the real interpreter to
        run); only a script-path invocation ("$PY" "$MEMIDX" <subcommand>
        ...) is intercepted, running a real-python `-c` snippet that
        monkeypatches memidx.compute_embeddings with `body` before handing
        off to memidx.main(). Launching the embed-worker with THIS SAME
        $PY (not sys.executable -- see the hook's own comment) is what
        lets the spawned worker subprocess hit this same interception."""
        wrapper = memidx_wrapper_python(self.td, name, body)
        return wrapper

    def _hang_stub(self):
        return self._wrapper_python(
            "embed-stub-hang",
            "def _hang(texts, model=None):\n"
            "    import time\n"
            "    time.sleep(60)\n"
            "    return []\n"
            "memidx.compute_embeddings = _hang\n",
        )

    def _fast_stub(self):
        return self._wrapper_python(
            "embed-stub-fast",
            "def _fake(texts, model=None):\n"
            "    return [[0.01] * memidx.EMBED_DIM for _ in texts]\n"
            "memidx.compute_embeddings = _fake\n",
        )

    def _run(self, env, timeout=15.0):
        return subprocess.run(
            [MC_BASH, str(POST_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=timeout,
        )

    # -- 1: a hung embedding backend never delays the hook -----------------

    def test_hung_embedding_backend_does_not_delay_the_hook(self):
        """Red today: the unmodified hook runs a FULL (embedding) reindex
        synchronously, so a hung `compute_embeddings` blocks the whole
        `git commit`. After the fix, the content pass is `--no-embed
        --auto` and never calls the embedding backend at all -- a hung
        backend (real or stubbed) can never delay it. The FTS index must
        already find the committed content the moment the hook returns."""
        root = self._store_with_one_unembedded_topic("pc-hang")
        hang_py = self._hang_stub()
        env = clean_env(MEMCONTINUUM_HOME=str(self.home), MEMCONTINUUM_PYTHON=str(hang_py),
                         MEMCONTINUUM_PROJECT="pc-hang", MEMCONTINUUM_ROOT=str(root),
                         MEMCONTINUUM_EMBED_WORKER="0")
        start = time.monotonic()
        proc = self._run(env, timeout=10.0)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 5.0, f"the hook took {elapsed:.1f}s -- must return within the content pass")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("rc=0", log_text, log_text)
        self.assertIn("embed=pending", log_text, log_text)

        # The FTS index already finds the committed content (`search
        # --mode fts` semantics -- queried directly against the `fts`
        # table here rather than via cmd_search, which prints instead of
        # returning).
        conn = sqlite3.connect(str(self.home / "pc-hang.sqlite"))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT path FROM fts WHERE fts MATCH ? AND project = ?", ("widget", "pc-hang"),
        ).fetchall()
        conn.close()
        self.assertTrue(rows, "FTS must already find the committed content when the hook returns")

    # -- 2: marker present iff E > 0 ----------------------------------------

    def test_marker_present_when_pending_absent_when_clean(self):
        root = self._store_with_one_unembedded_topic("pc-marker")
        env = clean_env(MEMCONTINUUM_HOME=str(self.home), MEMCONTINUUM_PYTHON=VENV_PYTHON,
                         MEMCONTINUUM_PROJECT="pc-marker", MEMCONTINUUM_ROOT=str(root),
                         MEMCONTINUUM_EMBED_WORKER="0")
        proc = self._run(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marker = self.home / "pc-marker.embed-pending"
        self.assertTrue(marker.is_file(), "a brand-new store has E>0 -- the marker must be touched")

        # A second commit with nothing new to embed (still --no-embed, so
        # the vector stays missing -- E stays > 0) keeps the marker. To
        # observe E==0/no-marker, embed for real once in-process (a mocked
        # compute_embeddings, deterministic vectors), then run the hook
        # again on unchanged content.
        args = SimpleNamespace(root=str(root), project="pc-marker", db=str(self.home / "pc-marker.sqlite"),
                                full=False, no_embed=False)
        with mock.patch.object(memidx, "compute_embeddings", side_effect=lambda texts, model=None: [
            [0.01] * memidx.EMBED_DIM for _ in texts
        ]):
            memidx.cmd_reindex(args)
        marker.unlink()

        env2 = clean_env(MEMCONTINUUM_HOME=str(self.home), MEMCONTINUUM_PYTHON=VENV_PYTHON,
                          MEMCONTINUUM_PROJECT="pc-marker", MEMCONTINUUM_ROOT=str(root),
                          MEMCONTINUUM_EMBED_WORKER="0")
        proc2 = self._run(env2)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertFalse(marker.is_file(), "every record has a fresh vector -- E==0, no marker")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("embed=clean", log_text, log_text)

    def test_a_refused_marker_is_logged_as_embed_failed_not_pending(self):
        root = self._store_with_one_unembedded_topic("pc-refused")
        victim = Path(self.td) / "victim.txt"
        victim.write_text("keep")
        (self.home / "pc-refused.embed-pending").symlink_to(victim)
        env = clean_env(MEMCONTINUUM_HOME=str(self.home), MEMCONTINUUM_PYTHON=VENV_PYTHON,
                         MEMCONTINUUM_PROJECT="pc-refused", MEMCONTINUUM_ROOT=str(root),
                         MEMCONTINUUM_EMBED_WORKER="0")
        proc = self._run(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = [l for l in (self.home / "hook.log").read_text().splitlines() if "post-commit-reindex" in l][-1]
        self.assertIn(" embed=failed:marker ", line)
        self.assertNotIn("embed=pending", line)
        self.assertEqual(victim.read_text(), "keep")

    # -- 7 (existing pins) is covered by TestPostCommitReindexHook's own
    # tests above (test_r2_resolves_..., unchanged assertions plus the
    # MEMCONTINUUM_EMBED_WORKER=0 addition).

    # -- 8: the worker actually spawns, detached, and clears the marker ----

    @unittest.skipUnless(hasattr(fcntl, "flock"), "fcntl.flock required")
    def test_h_worker_spawns_detached_and_clears_the_marker(self):
        root = self._store_with_one_unembedded_topic("pc-spawn")
        fast_py = self._fast_stub()
        env = clean_env(MEMCONTINUUM_HOME=str(self.home), MEMCONTINUUM_PYTHON=str(fast_py),
                         MEMCONTINUUM_PROJECT="pc-spawn", MEMCONTINUUM_ROOT=str(root))
        env.pop("MEMCONTINUUM_EMBED_WORKER", None)
        proc = self._run(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        marker = self.home / "pc-spawn.embed-pending"
        log_path = self.home / "pc-spawn.embed.log"
        self.assertIn("embed=pending", (self.home / "hook.log").read_text())

        # claim-first protocol: the worker renames the marker to <marker>.claim.<pid> when
        # its pass STARTS, so "done" means neither the marker nor a claim remains
        def in_flight():
            return marker.exists() or list(self.home.glob("pc-spawn.embed-pending.claim.*"))
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if log_path.exists() and not in_flight():
                break
            time.sleep(0.2)
        self.assertTrue(log_path.exists(), "the detached worker must have written its own log file")
        self.assertFalse(in_flight(), "the detached worker must clear the marker within 30s")

        # tearDown asserts the lock is free -- give the worker a moment to
        # release it after clearing the marker.
        lock_path = self.home / "pc-spawn.embed.lock"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and lock_path.exists():
            fd = os.open(str(lock_path), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fd, fcntl.LOCK_UN)
                break
            except BlockingIOError:
                time.sleep(0.2)
            finally:
                os.close(fd)

    # -- extra: the watchdog budget actually bounds this hook ---------------

    def test_watchdog_kills_a_hung_content_pass_within_budget(self):
        """Beyond the brief's own item 1 (which the redesigned hook makes
        moot for embeddings specifically, since the content pass never
        touches them): this proves the watchdog wiring itself -- a
        content pass that hangs for ANY reason is still bounded by
        MEMCONTINUUM_POST_COMMIT_BUDGET, via the same shared watchdog
        launcher every other guarded hook uses."""
        root = self._store_with_one_unembedded_topic("pc-wd")
        always_hang = Path(self.td) / "always-hang"
        always_hang.write_text(
            "#!/usr/bin/env bash\n"
            f'REAL_PY="{VENV_PYTHON}"\n'
            'if [ "$1" = "-c" ]; then\n'
            '    exec "$REAL_PY" "$@"\n'
            'fi\n'
            'sleep 6\n'
        )
        always_hang.chmod(0o755)
        env = clean_env(MEMCONTINUUM_HOME=str(self.home), MEMCONTINUUM_PYTHON=str(always_hang),
                         MEMCONTINUUM_PROJECT="pc-wd", MEMCONTINUUM_ROOT=str(root),
                         MEMCONTINUUM_EMBED_WORKER="0", MEMCONTINUUM_POST_COMMIT_BUDGET="2")
        start = time.monotonic()
        proc = self._run(env, timeout=10.0)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 4.0, f"took {elapsed:.1f}s -- the 2s watchdog budget must bound this")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=watchdog-killed", log_text)
        self.assertIn("hook=post-commit-reindex.sh", log_text)


PRE_COMMIT_HOOK = TOOLS_DIR / "hooks" / "pre-commit-append-only.sh"

_TOPIC_TWO_LINKS = (
    "---\n"
    "type: topic\n"
    "id: TOP-9001\n"
    "title: Fixture topic\n"
    "area: memory\n"
    "current: L2\n"
    "tags: []\n"
    "links:\n"
    "  - link: L2\n"
    "    date: '2024-01-02'\n"
    "    status: active\n"
    "    kind: adopted\n"
    "    ruling:\n"
    "      text: \"second ruling\"\n"
    "      authority: agent-inference\n"
    "  - link: L1\n"
    "    date: '2024-01-01'\n"
    "    status: historical\n"
    "    kind: adopted\n"
    "    ruling:\n"
    "      text: \"first ruling\"\n"
    "      authority: agent-inference\n"
    "---\n\nBody.\n"
)

_NEW_LINK_PREPEND = (
    "  - link: L3\n"
    "    date: '2024-01-03'\n"
    "    status: active\n"
    "    kind: adopted\n"
    "    ruling:\n"
    "      text: \"third ruling\"\n"
    "      authority: agent-inference\n"
)


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git"] + list(args), cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _init_topic_store(root):
    """A throwaway store repo (never fixtures/ or the engine's own store):
    one committed topic file with two links, isolated gitconfig identity."""
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", str(root)], cwd=root)
    _git(["config", "user.email", "test@example.com"], cwd=root)
    _git(["config", "user.name", "Test"], cwd=root)
    (root / "topics").mkdir()
    (root / "topics" / "foo.md").write_text(_TOPIC_TWO_LINKS)
    _git(["add", "-A"], cwd=root)
    _git(["commit", "-q", "-m", "base"], cwd=root)
    return root


def _prepend_new_link(text):
    return text.replace("links:\n", "links:\n" + _NEW_LINK_PREPEND).replace(
        "current: L2", "current: L3"
    )


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreCommitAppendOnlyHook(unittest.TestCase):
    """Task A2-1 (TOP-0122 L1 rule 3): hooks/pre-commit-append-only.sh --
    direct subprocess invocations of the script (fail-open infrastructure
    cases, and the clean/violating outcomes against a staged edit). The
    real-`git commit` path (git actually invoking the installed wrapper)
    is TestPreCommitAppendOnlyRealCommit below."""

    def test_bash_syntax_is_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(PRE_COMMIT_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_root_not_set_skips_and_names_project(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-root-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        env = clean_env(HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_PROJECT="pre-proj")
        env.pop("MEMCONTINUUM_ROOT", None)
        proc = subprocess.run([MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("skipped=root-unset", log_text)
        self.assertIn("project=pre-proj", log_text)

    def test_root_not_set_defaults_project_when_unset(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-root-default-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        env = clean_env(HOME=str(tmp), MEMCONTINUUM_HOME=str(home))
        env.pop("MEMCONTINUUM_ROOT", None)
        env.pop("MEMCONTINUUM_PROJECT", None)
        proc = subprocess.run([MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("project=default", log_text)

    def test_unborn_head_skips(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-unborn-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = Path(tmp) / "store"
        store.mkdir()
        _git(["init", "-q", str(store)], cwd=store)
        env = clean_env(
            HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_ROOT=str(store),
            MEMCONTINUUM_PROJECT="unborn-proj", MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        # cwd=store (fix wave 1 G1, Codex 1): git always invokes a
        # pre-commit hook with cwd at the repo's own working-tree root --
        # the hook's own not-the-store guard compares `git rev-parse
        # --show-toplevel` (no `-C`, reading cwd) against MEMCONTINUUM_ROOT,
        # so a direct subprocess invocation must reproduce that same cwd
        # to exercise anything past that guard.
        proc = subprocess.run(
            [MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=10, cwd=store,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("skipped=unborn-head", log_text)

    def test_missing_python_skips(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-nopython-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        env = clean_env(
            HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_ROOT=str(store),
            MEMCONTINUUM_PROJECT="nopy-proj", MEMCONTINUUM_PYTHON="/nonexistent/python",
        )
        # cwd=store: see test_unborn_head_skips's comment above.
        proc = subprocess.run(
            [MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=10, cwd=store,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("skipped=no-python", log_text)

    def test_clean_staged_edit_exits_0_and_logs_rc0(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-clean-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        text = _prepend_new_link((store / "topics" / "foo.md").read_text())
        (store / "topics" / "foo.md").write_text(text)
        _git(["add", "-A"], cwd=store)
        env = clean_env(
            HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_ROOT=str(store),
            MEMCONTINUUM_PROJECT="clean-proj", MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        # cwd=store: see test_unborn_head_skips's comment above.
        proc = subprocess.run(
            [MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=15, cwd=store,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("pre-commit-append-only: rc=0", log_text)
        self.assertIn("changed=1", log_text)
        self.assertIn("project=clean-proj", log_text)

    def test_violating_staged_edit_exits_1_and_logs_rc1(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-violation-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        text = (store / "topics" / "foo.md").read_text().replace("first ruling", "EDITED first ruling")
        (store / "topics" / "foo.md").write_text(text)
        _git(["add", "-A"], cwd=store)
        env = clean_env(
            HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_ROOT=str(store),
            MEMCONTINUUM_PROJECT="bad-proj", MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        # cwd=store: see test_unborn_head_skips's comment above.
        proc = subprocess.run(
            [MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=15, cwd=store,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("changed after being recorded", proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("pre-commit-append-only: rc=1", log_text)
        self.assertIn("changed=1", log_text)
        self.assertIn("project=bad-proj", log_text)

    def test_reformatted_history_staged_edit_exits_1_and_logs_rc1(self):
        """Acceptance 8 / INC-0124: the hook path for a REFORMAT, not just
        an ordinary field edit -- a whole-file `yaml.safe_dump` re-render
        of the fixture topic with every parsed link field unchanged. Before
        this fix this staged edit passed the hook (`rc=0`) exactly as it
        passed the unpatched `memlint.py` in INC-0124's own repro; this
        confirms the new check blocks the commit through the real
        pre-commit path, not only via a direct `memlint.py` invocation."""
        import yaml

        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-reformat-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        original = (store / "topics" / "foo.md").read_text()
        fm = yaml.safe_load(original.split("---\n", 2)[1])
        regenerated = "---\n" + yaml.safe_dump(fm) + "---\n\nBody.\n"
        self.assertNotEqual(regenerated, original, "fixture stale: dump matches source verbatim")
        self.assertEqual(
            yaml.safe_load(regenerated.split("---\n", 2)[1]), fm,
            "fixture invalid: regenerated blob must parse to the same dict as the original",
        )
        (store / "topics" / "foo.md").write_text(regenerated)
        _git(["add", "-A"], cwd=store)
        env = clean_env(
            HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_ROOT=str(store),
            MEMCONTINUUM_PROJECT="reformat-proj", MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        # cwd=store: see test_unborn_head_skips's comment above.
        proc = subprocess.run(
            [MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=15, cwd=store,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("reformatted, not appended", proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("pre-commit-append-only: rc=1", log_text)
        self.assertIn("changed=1", log_text)
        self.assertIn("project=reformat-proj", log_text)

    def test_unfenced_topic_added_is_blocked_by_the_schema_check(self):
        """INC-0127: the hook now ALSO runs the plain schema check
        (`memlint.py ROOT`, no `--against-ref`) and blocks on any ERROR
        from it. A brand-new (status `A` in the diff) unfenced topic is
        free under the append-only diff itself (no history to protect --
        `check_append_only` continues on status "A"), so before this fix
        round nothing in this hook would have caught it at all."""
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-unfenced-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        (store / "topics" / "bar.md").write_text(
            "type: topic\nid: TOP-9600\ntitle: Unfenced\nlinks:\n  - link: L1\n    status: active\n"
        )
        _git(["add", "-A"], cwd=store)
        env = clean_env(
            HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_ROOT=str(store),
            MEMCONTINUUM_PROJECT="unfenced-proj", MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        # cwd=store: see test_unborn_head_skips's comment above.
        proc = subprocess.run(
            [MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=15, cwd=store,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("missing opening frontmatter fence", proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("pre-commit-append-only: schema-rc=1", log_text)
        self.assertIn("project=unfenced-proj", log_text)

    def test_broken_python_crash_is_fail_open_not_a_schema_block(self):
        """Re-gate MAJOR: `$MEMCONTINUUM_PYTHON` pointing at an executable
        that CAN be launched but crashes before printing memlint's own
        summary line (the reviewer's own repro -- a venv missing PyYAML,
        reproduced here as a fake python that always prints a traceback-
        shaped stderr and exits 1, the finding's own offered alternative
        to building a real broken venv) must never read as a schema ERROR.
        Store is schema-clean with one legitimate NEW link staged (the
        reviewer's exact fixture) -- before this fix, rc=1 with `schema-
        rc=1` in hook.log, indistinguishable from a real fence violation;
        after it, both memlint invocations fail the same way (schema AND
        append-only alike -- the crash has nothing to do with argv), each
        recognized as an engine failure by its own missing summary-line
        marker, and the commit is let through."""
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-crash-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        text = _prepend_new_link((store / "topics" / "foo.md").read_text())
        (store / "topics" / "foo.md").write_text(text)
        _git(["add", "-A"], cwd=store)
        fake_py = Path(tmp) / "fake-python-crashes-on-import"
        fake_py.write_text(
            "#!/usr/bin/env bash\n"
            'echo "Traceback (most recent call last):" >&2\n'
            'echo \'  File "memlint.py", line 33, in <module>\' >&2\n'
            'echo "    from memidx import (" >&2\n'
            'echo "ModuleNotFoundError: No module named '"'"'yaml'"'"'" >&2\n'
            "exit 1\n"
        )
        fake_py.chmod(0o755)
        env = clean_env(
            HOME=str(tmp), MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_ROOT=str(store),
            MEMCONTINUUM_PROJECT="crash-proj", MEMCONTINUUM_PYTHON=str(fake_py),
        )
        # cwd=store: see test_unborn_head_skips's comment above.
        proc = subprocess.run(
            [MC_BASH, str(PRE_COMMIT_HOOK)], capture_output=True, text=True, env=env, timeout=15, cwd=store,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("schema=skipped-engine-failure", log_text)
        self.assertIn("skipped=engine-failure", log_text)
        self.assertIn("project=crash-proj", log_text)
        self.assertNotIn("schema-rc=1", log_text)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPreCommitAppendOnlyRealCommit(unittest.TestCase):
    """Task A2-1 red test: a real `git commit`, with the generated wrapper
    installed as .git/hooks/pre-commit (same shape scripts/repo-init.sh
    generates), actually gets refused for an edited old link and succeeds
    for a new one -- exercising git's own hook-invocation path, not a
    direct subprocess call of the script."""

    def _install_wrapper(self, store, home):
        hooks_dir = Path(_git(["rev-parse", "--git-path", "hooks"], cwd=store).stdout.strip())
        if not hooks_dir.is_absolute():
            hooks_dir = store / hooks_dir
        wrapper = hooks_dir / "pre-commit"
        # exec's the real script under MC_BASH (bash 3.2 during the
        # tests/run_bash32.sh harness), never a bare "bash" that would
        # resolve back to whatever modern bash is on PATH -- otherwise a
        # bash32 run of this test would never actually exercise the
        # script's own bash-3.2-safe claim.
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f"export MEMCONTINUUM_ROOT={shlex.quote(str(store))}\n"
            "export MEMCONTINUUM_PROJECT=realcommit-proj\n"
            f"export MEMCONTINUUM_PYTHON={shlex.quote(VENV_PYTHON)}\n"
            f"export MEMCONTINUUM_HOME={shlex.quote(str(home))}\n"
            f'exec "{MC_BASH}" {shlex.quote(str(PRE_COMMIT_HOOK))}\n'
        )
        wrapper.chmod(0o755)
        return wrapper

    def _commit_env(self, home):
        env = clean_env(HOME=str(home))
        for k in list(env):
            if k.startswith("MEMCONTINUUM_"):
                del env[k]
        return env

    def _head(self, store):
        return _git(["rev-parse", "HEAD"], cwd=store).stdout.strip()

    def _commit(self, store, env, message, extra_args=()):
        return subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
             "commit", "-q", "-m", message] + list(extra_args),
            cwd=str(store), capture_output=True, text=True, env=env, timeout=20,
        )

    def test_real_commit_with_edited_old_link_is_refused(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-realcommit-refuse-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        self._install_wrapper(store, home)
        head_before = self._head(store)

        text = (store / "topics" / "foo.md").read_text().replace("first ruling", "EDITED first ruling")
        (store / "topics" / "foo.md").write_text(text)
        _git(["add", "-A"], cwd=store)
        proc = self._commit(store, self._commit_env(home), "bad edit")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(head_before, self._head(store), "the refused commit must not have been created")

    def test_real_commit_with_new_link_succeeds(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-realcommit-ok-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        self._install_wrapper(store, home)
        head_before = self._head(store)

        text = _prepend_new_link((store / "topics" / "foo.md").read_text())
        (store / "topics" / "foo.md").write_text(text)
        _git(["add", "-A"], cwd=store)
        proc = self._commit(store, self._commit_env(home), "good edit")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotEqual(head_before, self._head(store), "the clean commit must have been created")

    def test_no_verify_bypasses(self):
        tmp = tempfile.mkdtemp(prefix="memcontinuum-precommit-realcommit-noverify-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        home = Path(tmp) / "home"
        home.mkdir()
        store = _init_topic_store(Path(tmp) / "store")
        self._install_wrapper(store, home)
        head_before = self._head(store)

        text = (store / "topics" / "foo.md").read_text().replace("first ruling", "EDITED first ruling")
        (store / "topics" / "foo.md").write_text(text)
        _git(["add", "-A"], cwd=store)
        proc = self._commit(store, self._commit_env(home), "bad edit bypassed", extra_args=["--no-verify"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotEqual(head_before, self._head(store), "--no-verify must let the commit through")


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestPostCommitRelintAndBackup(unittest.TestCase):
    """0.4.0: the post-commit hook's re-lint (a `--no-verify` commit of a bad
    record is still named) and store backup bundle, through real `git commit`
    runs against throwaway stores and a throwaway MEMCONTINUUM_HOME."""

    PROJECT = "bk-proj"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="memcontinuum-postcommit-bk-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.store = _init_topic_store(self.tmp / "store")
        self.backup = self.tmp / "backup"
        self.backup.mkdir()
        self._install_wrappers(self.store)

    def _install_wrappers(self, store):
        hooks_dir = Path(_git(["rev-parse", "--git-path", "hooks"], cwd=store).stdout.strip())
        if not hooks_dir.is_absolute():
            hooks_dir = store / hooks_dir
        for name, script in (("post-commit", POST_COMMIT_HOOK), ("pre-commit", PRE_COMMIT_HOOK)):
            wrapper = hooks_dir / name
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                f"export MEMCONTINUUM_ROOT={shlex.quote(str(store))}\n"
                f"export MEMCONTINUUM_PROJECT={self.PROJECT}\n"
                f"export MEMCONTINUUM_PYTHON={shlex.quote(VENV_PYTHON)}\n"
                f"export MEMCONTINUUM_HOME={shlex.quote(str(self.home))}\n"
                "export MEMCONTINUUM_EMBED_WORKER=0\n"
                f'exec "{MC_BASH}" {shlex.quote(str(script))}\n'
            )
            wrapper.chmod(0o755)

    def _edit_wrapper(self, name, old, new):
        wrapper = self.store / ".git" / "hooks" / name
        text = wrapper.read_text()
        self.assertIn(old, text)
        wrapper.write_text(text.replace(old, new))

    def _env(self, **extra):
        env = clean_env(HOME=str(self.home))
        for k in list(env):
            if k.startswith("MEMCONTINUUM_"):
                del env[k]
        env.update(extra)
        return env

    def _commit(self, message, env=None, no_verify=False):
        args = ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test",
                "commit", "-q", "-m", message]
        if no_verify:
            args.append("--no-verify")
        return subprocess.run(args, cwd=str(self.store), capture_output=True, text=True,
                              env=env or self._env(), timeout=60)

    def _good_commit(self, env=None):
        topic = self.store / "topics" / "foo.md"
        topic.write_text(_prepend_new_link(topic.read_text()))
        _git(["add", "topics/foo.md"], cwd=self.store)
        return self._commit("good edit", env)

    def _log(self):
        return (self.home / "hook.log").read_text()

    def _last_postcommit_line(self):
        return [l for l in self._log().splitlines() if "post-commit-reindex:" in l][-1]

    def _marker(self):
        return self.home / f"{self.PROJECT}.relint-failed"

    # -- backup -------------------------------------------------------------

    def test_backup_off_when_unset(self):
        proc = self._good_commit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_postcommit_line()
        self.assertIn("backup=off", line)
        self.assertIn("relint=ok", line)
        self.assertEqual(list(self.backup.iterdir()), [])
        self.assertFalse((self.backup / self.PROJECT).exists())

    def test_backup_bundle_written_verified_and_manifested(self):
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backup=ok", self._last_postcommit_line())
        sub = self.backup / self.PROJECT
        bundles = sorted(sub.glob(f"{self.PROJECT}-[0-9]*-[0-9]*.bundle"))
        self.assertEqual(len(bundles), 1, list(sub.iterdir()))
        self.assertEqual(list(sub.glob("*.part*")), [])
        _git(["bundle", "verify", str(bundles[0])], cwd=self.store)
        head = _git(["rev-parse", "HEAD"], cwd=self.store).stdout.strip()
        heads = _git(["bundle", "list-heads", str(bundles[0])], cwd=self.store).stdout
        self.assertIn(head, heads)
        manifest = (sub / "MANIFEST.txt").read_text().splitlines()
        self.assertEqual(len(manifest), 1)
        fields = [f.strip() for f in manifest[0].split(" | ")]
        self.assertEqual(fields[0], self.PROJECT)
        self.assertEqual(fields[1], f"source={self.store}")
        self.assertEqual(fields[2], f"HEAD={head}")
        self.assertEqual(fields[3], "commits=2")
        self.assertEqual(fields[4], f"bundle={bundles[0].name}")
        self.assertEqual(fields[5], f"size={bundles[0].stat().st_size}")
        import hashlib
        self.assertEqual(fields[6], "sha256=" + hashlib.sha256(bundles[0].read_bytes()).hexdigest())

    def test_backup_dir_from_config_sh_and_env_wins(self):
        (self.home / "config.sh").write_text(
            f"if [ -z \"${{MEMCONTINUUM_BACKUP_DIR:-}}\" ]; then MEMCONTINUUM_BACKUP_DIR={shlex.quote(str(self.backup))}; fi\n"
        )
        proc = self._good_commit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backup=ok", self._last_postcommit_line())
        self.assertTrue((self.backup / self.PROJECT / "MANIFEST.txt").exists())
        # env wins over config.sh
        other = self.tmp / "other"
        other.mkdir()
        proc = subprocess.run(
            ["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q",
             "--allow-empty", "-m", "second"],
            cwd=str(self.store), capture_output=True, text=True, timeout=60,
            env=self._env(MEMCONTINUUM_BACKUP_DIR=str(other)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((other / self.PROJECT / "MANIFEST.txt").exists())

    def test_backup_prune_keeps_newest_n_and_manifest_grows(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        for i in range(1, 6):
            (sub / f"{self.PROJECT}-2020010{i}-0900.bundle").write_text("old")
        (sub / "unrelated.txt").write_text("keep me")
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup),
                                           MEMCONTINUUM_BACKUP_KEEP="3"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        names = sorted(p.name for p in sub.glob(f"{self.PROJECT}-[0-9]*-[0-9]*.bundle"))
        self.assertEqual(len(names), 3, names)
        self.assertEqual(names[0], f"{self.PROJECT}-20200104-0900.bundle")
        self.assertEqual(names[1], f"{self.PROJECT}-20200105-0900.bundle")
        self.assertTrue((sub / "unrelated.txt").exists())
        self.assertEqual(len((sub / "MANIFEST.txt").read_text().splitlines()), 1)

    def test_backup_dir_missing_is_skipped_and_never_created(self):
        missing = self.tmp / "not-mounted"
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(missing)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backup=skipped:dir-missing", self._last_postcommit_line())
        self.assertFalse(missing.exists())

    def test_backup_failure_never_changes_the_commit(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        (sub / "MANIFEST.txt").mkdir()  # an append to a directory fails; the commit must not
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backup=ok-manifest-refused", self._last_postcommit_line())
        self.assertEqual(list(sub.glob("*.part*")), [])
        self.assertEqual(len(list(sub.glob("*.bundle"))), 1)

    def _minute_names(self):
        import datetime
        now = datetime.datetime.now()
        return [f"{self.PROJECT}-{(now + datetime.timedelta(minutes=m)).strftime('%Y%m%d-%H%M')}.bundle"
                for m in (0, 1)]

    def _failing_bundle_git(self):
        """A GIT_EXEC_PATH whose `git` fails for `bundle create` and otherwise
        runs the real one (git prepends its exec-path to PATH, so a plain PATH
        shim would lose)."""
        import shutil as _sh
        real = _sh.which("git")
        core = _git(["--exec-path"], cwd=self.store).stdout.strip()
        bindir = self.tmp / "shim-exec"
        bindir.mkdir()
        for entry in Path(core).iterdir():
            if entry.name != "git":
                (bindir / entry.name).symlink_to(entry)
        shim = bindir / "git"
        shim.write_text(
            "#!/bin/sh\n"
            'case " $* " in *" bundle create "*) exit 1;; esac\n'
            f'exec {shlex.quote(real)} "$@"\n')
        shim.chmod(0o755)
        return bindir

    def test_real_bundle_failure_is_logged_and_never_blocks(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        bindir = self._failing_bundle_git()
        env = self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["GIT_EXEC_PATH"] = str(bindir)
        before = _git(["rev-parse", "HEAD"], cwd=self.store).stdout.strip()
        proc = self._good_commit(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = _git(["rev-parse", "HEAD"], cwd=self.store).stdout.strip()
        self.assertNotEqual(before, after)
        self.assertIn("backup=failed:bundle-create", self._last_postcommit_line())
        self.assertEqual(list(sub.glob("*.part*")), [])
        self.assertEqual(list(sub.glob("*.bundle")), [])

    def test_planted_part_symlink_is_unlinked_not_followed(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        sentinel = self.tmp / "outside-sentinel"
        sentinel.write_text("untouched")
        for n in self._minute_names():
            (sub / (n + ".part.777")).symlink_to(sentinel)
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup),
                                           MEMCONTINUUM_BACKUP_PART_SUFFIX="777"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sentinel.read_text(), "untouched")
        # The hook `rm -f`s the part first (unlinks the symlink, never follows
        # it); noclobber then guards only the rm-to-open race window.
        self.assertIn("backup=ok", self._last_postcommit_line())
        self.assertEqual(len(list(sub.glob("*.bundle"))), 1)
        # only the next-minute plant (never opened by this run) may remain
        self.assertTrue(all(p.is_symlink() for p in sub.glob("*.part*")))

    def _age(self, path, seconds):
        t = time.time() - seconds
        os.utime(path, (t, t), follow_symlinks=False)

    def test_stale_part_lock_is_swept(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        stale = sub / f"{self.PROJECT}-20200101-0900.bundle.part.lock"
        stale.write_text("x")
        self._age(stale, 600)
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(stale.exists())
        self.assertIn("part-swept=1", self._last_postcommit_line())

    def test_fresh_part_of_another_pid_survives_and_old_one_is_swept(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        fresh = sub / f"{self.PROJECT}-20200101-0900.bundle.part.4242"
        fresh.write_text("live")
        old = sub / f"{self.PROJECT}-20200101-0901.bundle.part.4243"
        old.write_text("dead")
        self._age(old, 600)
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(fresh.read_text(), "live")
        self.assertFalse(old.exists())
        self.assertIn("part-swept=1", self._last_postcommit_line())

    def _sweep_with_budget(self, raw, maxage):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        fresh = sub / f"{self.PROJECT}-20200101-0900.bundle.part.4242"
        fresh.write_text("live")
        old = sub / f"{self.PROJECT}-20200101-0901.bundle.part.4243"
        old.write_text("dead")
        self._age(fresh, maxage - 8)
        self._age(old, maxage + 15)
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup),
                                           MEMCONTINUUM_POST_COMMIT_BUDGET=raw))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())
        self.assertIn("backup=ok", self._last_postcommit_line())
        self.assertEqual(len(list(sub.glob(f"{self.PROJECT}-2*-*.bundle"))), 1)

    # budget 08 -> 16 s and 010 -> 20 s (decimal, never octal); 120.5 and
    # empty fall back to 30 -> 60 s.
    def test_sweep_budget_08(self):
        self._sweep_with_budget("08", 16)

    def test_sweep_budget_010(self):
        self._sweep_with_budget("010", 20)

    def test_sweep_budget_fraction(self):
        self._sweep_with_budget("120.5", 60)

    def test_sweep_budget_empty(self):
        self._sweep_with_budget("", 60)

    def test_directory_at_final_bundle_name_is_refused(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        for n in self._minute_names():
            (sub / n).mkdir()
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backup=failed:dest", self._last_postcommit_line())
        self.assertEqual(list(sub.glob("*.part*")), [])
        for n in self._minute_names():
            self.assertEqual(list((sub / n).iterdir()), [])

    def test_bundle_to_stdout_form_is_cloneable(self):
        out = self.tmp / "stdout.bundle"
        with open(out, "wb") as fh:
            subprocess.run(["git", "-C", str(self.store), "bundle", "create", "-", "--all"],
                           stdout=fh, check=True, timeout=60)
        _git(["bundle", "verify", str(out)], cwd=self.store)
        dest = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", str(out), str(dest)], check=True, timeout=60,
                       capture_output=True)
        self.assertTrue((dest / "topics" / "foo.md").exists())

    def test_directory_at_marker_path_is_write_failed(self):
        self._marker().mkdir()
        (self.store / "topics" / "nofence.md").write_text("just prose, no frontmatter\n")
        _git(["add", "topics/nofence.md"], cwd=self.store)
        proc = self._commit("unfenced", no_verify=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("marker=write-failed", self._last_postcommit_line())
        self.assertTrue(self._marker().is_dir())
        self.assertEqual(list(self._marker().iterdir()), [])
        self.assertEqual(list(self.home.glob(f"{self.PROJECT}.relint-failed.new*")), [])

    def test_symlinked_marker_path_is_write_failed(self):
        sentinel = self.tmp / "marker-sentinel"
        sentinel.write_text("untouched")
        self._marker().symlink_to(sentinel)
        (self.store / "topics" / "nofence.md").write_text("just prose, no frontmatter\n")
        _git(["add", "topics/nofence.md"], cwd=self.store)
        proc = self._commit("unfenced", no_verify=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("marker=write-failed", self._last_postcommit_line())
        self.assertEqual(sentinel.read_text(), "untouched")
        self.assertTrue(self._marker().is_symlink())
        self.assertEqual(list(self.home.glob(f"{self.PROJECT}.relint-failed.new*")), [])

    # -- re-lint ------------------------------------------------------------

    def _assert_flagged(self, proc, kinds):
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("skipped the commit checks and would have been refused", proc.stderr)
        self.assertIn(f"in store {self.PROJECT}", proc.stderr)
        line = self._last_postcommit_line()
        self.assertTrue(any(f"relint=failed:{k}" in line for k in kinds), line)
        self.assertEqual(len([l for l in self._log().splitlines() if "post-commit-reindex:" in l]), 1)
        marker = self._marker().read_text()
        sha = _git(["rev-parse", "HEAD"], cwd=self.store).stdout.strip()
        self.assertIn(f"sha={sha}", marker)
        self.assertIn("summary=memlint: ", marker)

    def test_no_verify_edited_old_link_is_flagged(self):
        topic = self.store / "topics" / "foo.md"
        topic.write_text(topic.read_text().replace("first ruling", "EDITED first ruling"))
        _git(["add", "topics/foo.md"], cwd=self.store)
        proc = self._commit("bad edit", no_verify=True)
        self._assert_flagged(proc, ["append-only", "both"])

    def test_no_verify_record_without_frontmatter_fence_is_flagged(self):
        (self.store / "topics" / "nofence.md").write_text("just prose, no frontmatter\n")
        _git(["add", "topics/nofence.md"], cwd=self.store)
        proc = self._commit("unfenced", no_verify=True)
        self._assert_flagged(proc, ["schema", "both"])

    def test_next_clean_commit_clears_the_marker(self):
        (self.store / "topics" / "nofence.md").write_text("just prose, no frontmatter\n")
        _git(["add", "topics/nofence.md"], cwd=self.store)
        self._commit("unfenced", no_verify=True)
        self.assertTrue(self._marker().exists())
        _git(["rm", "-q", "-f", "topics/nofence.md"], cwd=self.store)
        proc = self._commit("fix: remove the unfenced record")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("relint=ok", self._last_postcommit_line())
        self.assertFalse(self._marker().exists())

    def test_clean_commit_is_silent_and_relint_ok(self):
        proc = self._good_commit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("would have been refused", proc.stderr)
        self.assertIn("relint=ok", self._last_postcommit_line())
        self.assertFalse(self._marker().exists())

    def test_engine_crash_is_skipped_not_a_finding(self):
        env = self._env()
        broken = self.tmp / "broken-python"
        broken.write_text(
            "#!/bin/sh\n"
            'case "$1" in *memlint.py) echo "Traceback: boom" >&2; exit 1;; esac\n'
            f'exec {shlex.quote(VENV_PYTHON)} "$@"\n')
        broken.chmod(0o755)
        wrapper = self.store / ".git" / "hooks" / "post-commit"
        text = wrapper.read_text().replace(shlex.quote(VENV_PYTHON), shlex.quote(str(broken)))
        wrapper.write_text(text)
        proc = self._good_commit(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("would have been refused", proc.stderr)
        self.assertIn("relint=skipped:engine-failure", self._last_postcommit_line())
        self.assertFalse(self._marker().exists())

    def _plant_marker(self):
        (self.store / "topics" / "nofence.md").write_text("just prose, no frontmatter\n")
        _git(["add", "topics/nofence.md"], cwd=self.store)
        self._commit("unfenced", no_verify=True)
        self.assertTrue(self._marker().exists())

    def _broken_python(self):
        broken = self.tmp / "broken-python"
        broken.write_text(
            "#!/bin/sh\n"
            'case "$1" in *memlint.py) echo "Traceback: boom" >&2; exit 1;; esac\n'
            f'exec {shlex.quote(VENV_PYTHON)} "$@"\n')
        broken.chmod(0o755)
        return broken

    def test_engine_crash_never_clears_a_sticky_marker(self):
        self._plant_marker()
        before = self._marker().read_text()
        self._edit_wrapper("post-commit", shlex.quote(VENV_PYTHON), shlex.quote(str(self._broken_python())))
        proc = subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q",
                               "--allow-empty", "--no-verify", "-m", "empty"], cwd=str(self.store),
                              capture_output=True, text=True, env=self._env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("relint=skipped:engine-failure", self._last_postcommit_line())
        self.assertEqual(self._marker().read_text(), before)

    def test_marker_records_root_and_a_foreign_marker_is_not_cleared(self):
        self._plant_marker()
        lines = self._marker().read_text().splitlines()
        self.assertEqual(lines[0], f"root={self.store.resolve()}")
        foreign = "root=/some/other/store\n" + "".join(l + "\n" for l in lines[1:])
        self._marker().write_text(foreign)
        _git(["rm", "-q", "-f", "topics/nofence.md"], cwd=self.store)
        proc = self._commit("fix")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("relint=ok marker=foreign", self._last_postcommit_line())
        self.assertEqual(self._marker().read_text(), foreign)

    def test_root_commit_gets_schema_pass_only(self):
        solo_root = self.tmp / "solo"
        _git(["init", "-q", str(solo_root)], cwd=self.tmp)
        self._install_wrappers(solo_root)
        (solo_root / "topics").mkdir()
        (solo_root / "topics" / "bad.md").write_text("no fence\n")
        _git(["add", "topics/bad.md"], cwd=solo_root)
        proc = subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q",
                               "-m", "root", "--no-verify"], cwd=str(solo_root), env=self._env(),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("relint=failed:schema", self._last_postcommit_line())

    def test_missing_lint_lib_fails_open_in_both_hooks(self):
        env = self._env(MC_STORE_LINT_LIB_PATH=str(self.tmp / "no-such-lint.sh"))
        proc = self._good_commit(env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log = self._log()
        self.assertIn("pre-commit-append-only: skipped=no-lint-lib project=" + self.PROJECT, log)
        self.assertIn("relint=skipped:no-lint-lib", self._last_postcommit_line())

    # -- hardening ----------------------------------------------------------

    def test_no_watchdog_skips_relint_and_backup(self):
        self._edit_wrapper("post-commit", shlex.quote(VENV_PYTHON), shlex.quote(str(self.tmp / "no-python")))
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=str(self.backup)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_postcommit_line()
        self.assertIn("relint=skipped:no-watchdog", line)
        self.assertIn("backup=skipped:no-watchdog", line)
        self.assertEqual(list(self.backup.iterdir()), [])

    def _backup_line_after(self, project=None, **env):
        if project is not None:
            wrapper = self.store / ".git" / "hooks" / "post-commit"
            wrapper.write_text(re.sub(r"(?m)^export MEMCONTINUUM_PROJECT=.*$",
                                      lambda m: f"export MEMCONTINUUM_PROJECT={shlex.quote(project)}",
                                      wrapper.read_text()))
        proc = subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q",
                               "--allow-empty", "-m", "empty"], cwd=str(self.store),
                              capture_output=True, text=True, env=self._env(**env), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self._last_postcommit_line()

    def test_project_dotdot_cannot_escape_the_backup_dir(self):
        inner = self.tmp / "inner"
        inner.mkdir()
        line = self._backup_line_after("..", MEMCONTINUUM_BACKUP_DIR=str(inner))
        self.assertIn("backup=skipped:bad-project", line)
        self.assertEqual([p.name for p in self.tmp.glob("*.bundle*")], [])
        self.assertEqual(list(inner.iterdir()), [])

    def test_project_dot_or_slash_is_refused(self):
        line = self._backup_line_after(".", MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        self.assertIn("backup=skipped:bad-project", line)
        self.assertEqual(list(self.backup.iterdir()), [])
        line = self._backup_line_after("a/b", MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        self.assertIn("backup=skipped:bad-project", line)
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_symlinked_project_folder_pointing_outside_is_refused(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.backup / self.PROJECT).symlink_to(outside)
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        self.assertIn("backup=failed:bad-project", line)
        self.assertEqual(list(outside.iterdir()), [])

    def test_relative_backup_dir_is_refused(self):
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR="relative/dir")
        self.assertIn("backup=skipped:bad-dir", line)

    def test_backup_dir_with_a_space(self):
        spaced = self.tmp / "back up dir"
        spaced.mkdir()
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(spaced))
        self.assertIn("backup=ok", line)
        self.assertEqual(len(list((spaced / self.PROJECT).glob("*.bundle"))), 1)

    def test_backup_keep_zero_padded_and_octal_looking_values(self):
        sub = self.backup / self.PROJECT
        for keep, expect_ok in (("00", False), ("0", False), ("abc", False), ("08", True), ("010", True)):
            with self.subTest(keep=keep):
                shutil.rmtree(sub, ignore_errors=True)
                sub.mkdir()
                for i in range(1, 13):
                    (sub / f"{self.PROJECT}-2020{i:02d}01-0900.bundle").write_text("old")
                line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup),
                                               MEMCONTINUUM_BACKUP_KEEP=keep)
                bundles = sorted(sub.glob("*.bundle"))
                self.assertNotIn("syntax error", line)
                self.assertTrue(any(b.read_bytes()[:1] != b"o" for b in bundles), "new bundle survives")
                if expect_ok:
                    self.assertIn("backup=ok", line)
                    self.assertEqual(len(bundles), int(keep.lstrip("0")))
                else:
                    self.assertIn("backup=failed:bad-keep", line)
                    self.assertEqual(len(bundles), 13)  # no prune at all

    def test_prune_never_deletes_the_bundle_just_written(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        future = sub / f"{self.PROJECT}-29991231-2359.bundle"
        future.write_text("future")
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup),
                                       MEMCONTINUUM_BACKUP_KEEP="1")
        self.assertIn("backup=ok", line)
        names = [p.name for p in sub.glob("*.bundle")]
        self.assertEqual(len(names), 1, names)
        self.assertNotEqual(names[0], future.name)
        self.assertFalse(future.exists())

    def test_failed_prune_is_reported(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        old = sub / f"{self.PROJECT}-20200101-0900.bundle"
        old.mkdir()  # rm -f on a directory fails
        (old / "x").write_text("x")
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup),
                                       MEMCONTINUUM_BACKUP_KEEP="1")
        self.assertIn("backup=ok-prune-failed", line)

    def test_stale_part_is_swept_and_reported(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        stale = sub / f"{self.PROJECT}-20200101-0900.bundle.part"
        stale.write_text("half a bundle")
        self._age(stale, 600)
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        self.assertIn("backup=ok part-swept=1", line)
        self.assertFalse(stale.exists())
        # ... and no note at all when nothing was swept.
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        self.assertNotIn("part-swept", line)

    # -- fix round 2 --------------------------------------------------------

    def _snapshot(self, root):
        return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))

    def test_bad_project_with_a_finding_writes_no_marker_anywhere(self):
        victim_parent = self.tmp / "victimparent"
        victim_parent.mkdir()
        # HOME is tmp/home, so ../victim lands in tmp: outside HOME.
        before = self._snapshot(self.tmp)
        (self.store / "topics" / "nofence.md").write_text("just prose, no frontmatter\n")
        _git(["add", "topics/nofence.md"], cwd=self.store)
        wrapper = self.store / ".git" / "hooks" / "post-commit"
        wrapper.write_text(re.sub(r"(?m)^export MEMCONTINUUM_PROJECT=.*$",
                                  "export MEMCONTINUUM_PROJECT=../victim", wrapper.read_text()))
        proc = self._commit("unfenced", no_verify=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([p for p in self.tmp.rglob("*victim*") if p != victim_parent], [])
        self.assertEqual([p for p in self.tmp.rglob("*relint-failed*")], [])
        self.assertEqual([p for p in self.tmp.rglob("*.sqlite")], [])
        line = self._last_postcommit_line()
        self.assertIn("relint=skipped:bad-project backup=skipped:bad-project", line)
        del before

    def test_project_with_a_space_still_reindexes_but_skips_relint_and_backup(self):
        line = self._backup_line_after("my store", MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        self.assertIn("relint=skipped:bad-project", line)
        self.assertIn("backup=skipped:bad-project", line)
        self.assertEqual(list(self.backup.iterdir()), [])
        self.assertTrue((self.home / "my store.sqlite").exists())

    def test_backup_dir_with_a_newline_is_refused_and_nothing_is_deleted(self):
        weird = self.tmp / "evil\nname"
        weird.mkdir()
        sentinel = self.tmp / "sentinel.bundle"
        sentinel.write_text("keep")
        sub = weird / self.PROJECT
        sub.mkdir()
        old = sub / f"{self.PROJECT}-20200101-0900.bundle"
        old.write_text("old")
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(weird), MEMCONTINUUM_BACKUP_KEEP="1")
        self.assertIn("backup=skipped:bad-dir", line)
        self.assertEqual(old.read_text(), "old")
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertFalse((sub / "MANIFEST.txt").exists())

    def test_symlinked_manifest_is_not_appended_through(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        outside = self.tmp / "outside-sentinel.txt"
        outside.write_text("untouched\n")
        (sub / "MANIFEST.txt").symlink_to(outside)
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup))
        self.assertIn("backup=ok-manifest-refused", line)
        self.assertEqual(outside.read_text(), "untouched\n")
        self.assertEqual(len(list(sub.glob("*.bundle"))), 1)

    def test_foreign_marker_survives_a_second_stores_finding_and_clean_run(self):
        self._plant_marker()
        lines = self._marker().read_text().splitlines()
        foreign = "root=/some/other/store\n" + "".join(l + "\n" for l in lines[1:])
        self._marker().write_text(foreign)
        (self.store / "topics" / "nofence2.md").write_text("still no frontmatter\n")
        _git(["add", "topics/nofence2.md"], cwd=self.store)
        proc = self._commit("another bad one", no_verify=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("would have been refused", proc.stderr)
        self.assertRegex(self._last_postcommit_line(), r"relint=failed:\w[\w-]* marker=foreign")
        self.assertEqual(self._marker().read_text(), foreign)
        _git(["rm", "-q", "-f", "topics/nofence.md", "topics/nofence2.md"], cwd=self.store)
        self._commit("fix")
        self.assertEqual(self._marker().read_text(), foreign)

    def test_stub_interpreter_with_empty_output_never_clears_a_marker(self):
        self._plant_marker()
        before = self._marker().read_text()
        stub = self.tmp / "true-python"
        # memlint "succeeds" silently (what /bin/true would do); everything else
        # (the watchdog guard, reindex) stays real.
        stub.write_text("#!/bin/sh\n"
                        'case "$1" in *memlint.py) exit 0;; esac\n'
                        f'exec {shlex.quote(VENV_PYTHON)} "$@"\n')
        stub.chmod(0o755)
        self._edit_wrapper("post-commit", shlex.quote(VENV_PYTHON), shlex.quote(str(stub)))
        proc = subprocess.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=T", "commit", "-q",
                               "--allow-empty", "--no-verify", "-m", "empty"], cwd=str(self.store),
                              capture_output=True, text=True, env=self._env(), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("relint=skipped:engine-failure", self._last_postcommit_line())
        self.assertEqual(self._marker().read_text(), before)

    def test_bundle_name_grammar_ignores_decoys(self):
        sub = self.backup / self.PROJECT
        sub.mkdir()
        decoys = [f"{self.PROJECT}-1notes-2manual.bundle", f"{self.PROJECT}-202001-0900.bundle",
                  f"{self.PROJECT}-20200101-09000.bundle", f"{self.PROJECT}-1notes-2manual.bundle.part"]
        for d in decoys:
            (sub / d).write_text("decoy")
        real = sub / f"{self.PROJECT}-20200101-0900.bundle"
        real.write_text("old")
        line = self._backup_line_after(MEMCONTINUUM_BACKUP_DIR=str(self.backup),
                                       MEMCONTINUUM_BACKUP_KEEP="1")
        self.assertIn("backup=ok", line)
        self.assertNotIn("part-swept", line)
        for d in decoys:
            self.assertEqual((sub / d).read_text(), "decoy", d)
        self.assertFalse(real.exists())

    def test_marker_write_failure_leaves_the_old_marker(self):
        self._plant_marker()
        before = self._marker().read_text()
        # A read-only home: the unique temp cannot be created, the old marker stays.
        self._marker().chmod(0o444)
        self.home.chmod(0o555)
        self.addCleanup(self.home.chmod, 0o755)
        (self.store / "topics" / "nofence2.md").write_text("still no frontmatter\n")
        _git(["add", "topics/nofence2.md"], cwd=self.store)
        proc = self._commit("another bad one", no_verify=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("marker=write-failed", self._last_postcommit_line())
        self.assertEqual(self._marker().read_text(), before)

    def test_empty_env_backup_dir_overrides_config_sh(self):
        (self.home / "config.sh").write_text(
            f"if [ -z \"${{MEMCONTINUUM_BACKUP_DIR+x}}\" ]; then MEMCONTINUUM_BACKUP_DIR={shlex.quote(str(self.backup))}; fi\n"
        )
        proc = self._good_commit(self._env(MEMCONTINUUM_BACKUP_DIR=""))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("backup=off", self._last_postcommit_line())
        self.assertEqual(list(self.backup.iterdir()), [])


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
