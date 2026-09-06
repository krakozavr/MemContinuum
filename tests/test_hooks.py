"""Tests for hooks/pre-edit-chain.sh (the PreToolUse retrieval-surface hook).

These exercise the real shell script via subprocess -- not a reimplementation
of its logic -- because the thing under test is the script's environment
handling (the PYTHONPATH trap, env-driven project/root resolution, fail-open
behavior) as much as its output shape.
"""
import fcntl
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
        self.assertFalse(
            (TOOLS_DIR / ".venv" / "bin" / "python").exists(),
            "this test relies on no engine .venv existing in this checkout",
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
        self.assertFalse(
            (TOOLS_DIR / ".venv" / "bin" / "python").exists(),
            "this test relies on no engine .venv existing in this checkout",
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
        self.assertEqual(new_outcome, oracle_outcome)
        self.assertEqual(new_outcome, "no-match")

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
        self.assertFalse(
            (TOOLS_DIR / ".venv" / "bin" / "python").exists(),
            "this test relies on no engine .venv existing in this checkout",
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
        self.assertTrue(marker.is_file(), "content pass alone must not clear the marker")

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if log_path.exists() and not marker.exists():
                break
            time.sleep(0.2)
        self.assertTrue(log_path.exists(), "the detached worker must have written its own log file")
        self.assertFalse(marker.exists(), "the detached worker must clear the marker within 30s")

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
