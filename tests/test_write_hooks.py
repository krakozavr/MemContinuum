"""Tests for the write-side reminder hooks: memidx.py's `unmapped` engine
addition, hooks/memlib.sh, and the five hook scripts (ledger-post-edit.sh,
precompact-persist.sh, sessionstart-remind.sh, userprompt-remind.sh,
sessionend-stamp.sh) -- per docs/!_MEM/docs/DESIGN.md.

`unmapped` is exercised in-process (memidx.cmd_unmapped) except for the
subprocess-isolated no-fastembed-import check. The hook scripts are always
exercised as real subprocesses (bash + the venv python) with a poisoned
PYTHONPATH in the environment, per test_hooks.py's own pattern -- the thing
under test is real environment handling, not a reimplementation of the shell
logic in Python.
"""
import contextlib
import fcntl
import hashlib
import inspect
import io
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import chunkers  # noqa: E402
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
HOOKS_DIR = TOOLS_DIR / "hooks"
FIXTURES_DIR = TOOLS_DIR / "fixtures" / "payloads"
LEDGER_HOOK = HOOKS_DIR / "ledger-post-edit.sh"
PRECOMPACT_HOOK = HOOKS_DIR / "precompact-persist.sh"
SESSIONSTART_HOOK = HOOKS_DIR / "sessionstart-remind.sh"
USERPROMPT_HOOK = HOOKS_DIR / "userprompt-remind.sh"
SESSIONEND_HOOK = HOOKS_DIR / "sessionend-stamp.sh"
CODE_REFRESH_HOOK = HOOKS_DIR / "code-refresh.sh"
MEMLIB = HOOKS_DIR / "memlib.sh"
MC_PATH_LIB = HOOKS_DIR / "mc-path-lib.sh"
# A decoy PYTHONPATH entry ahead of a synthetic poison dir elsewhere in this
# file. It does not need to exist on disk -- Python silently skips a missing
# PYTHONPATH entry -- it just needs to have no `yaml` module in it.
POISONED_SITE_PACKAGES = os.environ.get(
    "MEMCONTINUUM_TEST_DECOY_SITE_PACKAGES", "/nonexistent/decoy-site-packages"
)

FORBIDDEN_LINE_PREFIXES = ("- link:", "ruling:", "quote:", "authority:")


# ---------------------------------------------------------------------------
# shared fixtures / helpers
# ---------------------------------------------------------------------------


def ns(**kw):
    base = dict(project=memidx.DEFAULT_PROJECT, db=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def reindex(root, db, project=memidx.DEFAULT_PROJECT, full=False, no_embed=True):
    args = ns(root=str(root), db=str(db), project=project, full=full, no_embed=no_embed)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memidx.cmd_reindex(args)
    return rc


TOPIC_MD = """---
type: topic
id: TOP-9001
title: Mapped thing
area: testing/area
current: L1
code_refs:
  - src/mapped.py
links:
  - link: L1
    date: 2026-08-29
    status: active
    kind: adopted
    ruling:
      text: "test ruling text"
      authority: owner-verbatim
      source: "test"
    rationale:
      text: "test rationale text"
      authority: agent-inference
    recorded_by: agent
    recorded_at: 2026-08-29
---

Body.
"""

CONCEPT_MD = """---
type: concept
id: CON-9001
title: Concept only thing
owner_boundary: "src/ -- test boundary"
implemented_by:
  - src/concept_only.py
tested_by: []
governed_by: []
involved_in: []
---

Body.
"""


def build_store_root(root: Path) -> None:
    _write(root / "topics" / "testing" / "mapped-topic.md", TOPIC_MD)
    _write(root / "concepts" / "concept-only.md", CONCEPT_MD)


def git_init(repo: Path, commit: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    if commit:
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init", "--allow-empty"], cwd=repo, check=True)


def git_is_clean(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip() == ""


def git_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return result.stdout.strip()


def clean_env(**overrides):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(overrides)
    return env


def run_script(script: Path, payload_text: str, env: dict, timeout: float = 6.0, cwd=None):
    """`cwd` defaults to the test process's own (subprocess.run's default) --
    pass one explicitly for a test that needs the hook to run with specific
    files sitting in the working directory (the noglob-guard test, T1)."""
    start = time.monotonic()
    proc = subprocess.run(
        [MC_BASH, str(script)],
        input=payload_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=None if cwd is None else str(cwd),
    )
    elapsed = time.monotonic() - start
    return proc, elapsed


def _poisoned_pythonpath(tmp_dir: Path) -> str:
    poison_dir = Path(tmp_dir) / "poison-site-packages"
    poison_dir.mkdir(exist_ok=True)
    (poison_dir / "yaml.py").write_text('raise RuntimeError("poisoned PYTHONPATH not cleared")\n')
    return f"{POISONED_SITE_PACKAGES}:{poison_dir}"


def poisoned_env(tmp_dir: Path, **overrides):
    env = clean_env(PYTHONPATH=_poisoned_pythonpath(tmp_dir))
    env.update(overrides)
    return env


def scan_forbidden_lines(text: str):
    hits = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(FORBIDDEN_LINE_PREFIXES):
            hits.append(line)
    return hits


# ---------------------------------------------------------------------------
# 1. memidx.py `unmapped`
# ---------------------------------------------------------------------------


class TestUnmappedCommand(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-unmapped-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.store_root = Path(self.td) / "store"
        build_store_root(self.store_root)
        self.code_root = Path(self.td) / "code"
        _write(self.code_root / "src" / "mapped.py", "# mapped\n")
        _write(self.code_root / "src" / "concept_only.py", "# concept only\n")
        _write(self.code_root / "src" / "nothing.py", "# nothing\n")
        self.db = Path(self.td) / "idx.sqlite"
        self.project = "unmappedtest"

    def _args(self, paths, code_root=None, json_out=True):
        return ns(
            command="unmapped",
            paths=list(paths),
            root=str(self.store_root),
            code_root=str(code_root) if code_root else None,
            project=self.project,
            db=str(self.db),
            json=json_out,
        )

    def _run(self, paths, code_root=None):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = memidx.cmd_unmapped(self._args(paths, code_root=code_root))
        return rc, json.loads(buf.getvalue())

    def test_mapped_topic(self):
        reindex(self.store_root, self.db, project=self.project)
        rc, out = self._run(["src/mapped.py"])
        self.assertEqual(rc, 0)
        self.assertEqual(out["mapped_topic"], ["src/mapped.py"])
        self.assertEqual(out["mapped_concept_only"], [])
        self.assertEqual(out["unmapped"], [])
        self.assertEqual(out["coverage_status"], "ok")

    def test_mapped_concept_only(self):
        reindex(self.store_root, self.db, project=self.project)
        rc, out = self._run(["src/concept_only.py"])
        self.assertEqual(out["mapped_topic"], [])
        self.assertEqual(out["mapped_concept_only"], ["src/concept_only.py"])
        self.assertEqual(out["unmapped"], [])
        self.assertEqual(out["coverage_status"], "ok")

    def test_none_is_unmapped(self):
        reindex(self.store_root, self.db, project=self.project)
        rc, out = self._run(["src/nothing.py"])
        self.assertEqual(out["mapped_topic"], [])
        self.assertEqual(out["mapped_concept_only"], [])
        self.assertEqual(out["unmapped"], ["src/nothing.py"])
        self.assertEqual(out["coverage_status"], "ok")

    def test_code_root_relative_matching_and_display(self):
        reindex(self.store_root, self.db, project=self.project)
        abs_path = str(self.code_root / "src" / "mapped.py")
        rc, out = self._run([abs_path], code_root=self.code_root)
        self.assertEqual(out["mapped_topic"], ["src/mapped.py"])

    def test_first_run_is_uninitialized_not_self_built(self):
        # F1: a wholly missing db is no longer silently built by unmapped's
        # self-heal -- only genuine same-generation on-disk drift ("stale")
        # still self-heals; missing/uninitialized/upgrade-required do not.
        self.assertFalse(self.db.exists())
        rc, out = self._run(["src/mapped.py", "src/nothing.py"])
        self.assertEqual(rc, 1)
        self.assertEqual(out["coverage_status"], "uninitialized")
        self.assertEqual(out["mapped_topic"], [])
        self.assertEqual(out["unmapped"], [])
        self.assertFalse(self.db.exists(), "an uninitialized read must never create the db")

    def test_drift_triggers_incremental_reindex(self):
        reindex(self.store_root, self.db, project=self.project)
        # add a new topic after the initial index -- this is drift.
        new_topic = """---
type: topic
id: TOP-9002
title: Second mapped thing
area: testing/area
current: L1
code_refs:
  - src/nothing.py
links:
  - link: L1
    date: 2026-08-29
    status: active
    kind: adopted
    ruling:
      text: "second"
      authority: owner-verbatim
      source: "test"
    recorded_by: agent
    recorded_at: 2026-08-29
---

Body.
"""
        _write(self.store_root / "topics" / "testing" / "second-topic.md", new_topic)
        rc, out = self._run(["src/nothing.py"])
        self.assertEqual(out["coverage_status"], "ok")
        self.assertEqual(out["mapped_topic"], ["src/nothing.py"])

    def test_stale_index_yields_unknown_and_no_unmapped_claims(self):
        reindex(self.store_root, self.db, project=self.project)
        # Corrupt the sqlite file so check/reindex cannot succeed -- the
        # index is unreadable, which must never be reported as "no topic
        # covers this file".
        self.db.write_bytes(b"not a sqlite file at all")
        rc, out = self._run(["src/mapped.py", "src/nothing.py"])
        self.assertEqual(out["coverage_status"], "unknown")
        self.assertEqual(out["unmapped"], [])
        # Design R7 (audit MC-P2-03, TOP-0123 L7): a corrupt-blob
        # sqlite3.DatabaseError is not an OperationalError, so it lands in
        # the broad except arm and now carries a typed `degraded` object
        # too -- unknown stays unknown, but no longer silent about why.
        self.assertIn("degraded", out)
        self.assertEqual(out["degraded"]["reason_code"], "internal-error")

    def test_no_paths_matches_in_unknown_status_still_absent_from_unmapped(self):
        self.db.write_bytes(b"not a sqlite file at all")
        rc, out = self._run(["src/totally/never/seen.py"])
        self.assertEqual(out["coverage_status"], "unknown")
        self.assertNotIn("src/totally/never/seen.py", out["unmapped"])
        self.assertNotIn("src/totally/never/seen.py", out["mapped_topic"])
        self.assertNotIn("src/totally/never/seen.py", out["mapped_concept_only"])

    def test_o_of_paths_not_a_tree_walk(self):
        """Sanity check that classification is purely DB-driven per path (no
        re-walk of the code tree): a path that was never written to disk at
        all is still correctly classified via its string alone."""
        reindex(self.store_root, self.db, project=self.project)
        rc, out = self._run(["src/mapped.py", "src/does/not/exist/on/disk.py"])
        self.assertEqual(out["mapped_topic"], ["src/mapped.py"])
        self.assertEqual(out["unmapped"], ["src/does/not/exist/on/disk.py"])

    def test_does_not_import_fastembed(self):
        reindex(self.store_root, self.db, project=self.project)
        script = (
            "import sys; sys.path.insert(0, %r); import memidx; "
            "memidx.main(['unmapped', 'src/mapped.py', '--root', %r, "
            "'--project', %r, '--db', %r, '--json']); "
            "assert 'fastembed' not in sys.modules, 'fastembed was imported'; "
            "assert 'numpy' not in sys.modules, 'numpy was imported'"
        ) % (str(TOOLS_DIR), str(self.store_root), self.project, str(self.db))
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_root_side_effects_no_markdown_written(self):
        before = sorted(p.name for p in (self.store_root / "topics" / "testing").glob("*"))
        reindex(self.store_root, self.db, project=self.project)
        self._run(["src/mapped.py", "src/nothing.py"], code_root=self.code_root)
        after = sorted(p.name for p in (self.store_root / "topics" / "testing").glob("*"))
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# shared harness for the shell hooks
# ---------------------------------------------------------------------------


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class HookTestBase(unittest.TestCase):
    """Builds a code-root git repo, a store-root git repo (with the same
    TOPIC_MD/CONCEPT_MD fixtures as TestUnmappedCommand), a MEMCONTINUUM_HOME,
    and a poisoned-PYTHONPATH-safe env for every hook subclass."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-hooks-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

        self.code_root = Path(self.td) / "code"
        self.code_root.mkdir()
        _write(self.code_root / "src" / "mapped.py", "# mapped\n")
        _write(self.code_root / "src" / "unmapped.py", "# unmapped\n")
        git_init(self.code_root)

        self.store_root = Path(self.td) / "store"
        build_store_root(self.store_root)
        git_init(self.store_root)

        self.home = Path(self.td) / "home"
        self.home.mkdir()
        self.project = "hooktest"

        # pre-build the index once so hooks that touch it aren't paying the
        # "first ever build" cost inside every single test.
        db = self.home / f"{self.project}.sqlite"
        reindex(self.store_root, db, project=self.project)

    def base_env(self, **overrides):
        env = clean_env(
            MEMCONTINUUM_HOME=str(self.home),
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_ROOT=str(self.store_root),
            MEMCONTINUUM_CODE_ROOT=str(self.code_root),
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        env.update(overrides)
        return env

    def poisoned_base_env(self, **overrides):
        # Build on the already-correctly-scoped base_env rather than a
        # second, fresh `clean_env()` snapshot -- that fresh snapshot's
        # ambient MEMCONTINUUM_HOME (whatever the invoking shell exports)
        # used to overwrite this class's own MEMCONTINUUM_HOME=self.home
        # (task-8-review.md LOW-2).
        env = dict(self.base_env())
        env["PYTHONPATH"] = _poisoned_pythonpath(self.td)
        env.update(overrides)
        return env

    def state_file(self, session_id: str) -> Path:
        return self.home / "sessions" / self.project / f"{session_id}.json"

    def load_state(self, session_id: str) -> dict:
        f = self.state_file(session_id)
        if not f.exists():
            return {}
        return json.loads(f.read_text())

    # ---- payload builders -------------------------------------------

    def post_tool_use_payload(self, session_id, file_path, tool_name="Edit", agent_id=None):
        d = {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "cwd": str(self.code_root),
            "tool_input": {"file_path": file_path},
        }
        if agent_id:
            d["agent_id"] = agent_id
            d["agent_type"] = "Explore"
        return json.dumps(d)

    def pre_compact_payload(self, session_id, trigger="auto"):
        return json.dumps(
            {
                "session_id": session_id,
                "hook_event_name": "PreCompact",
                "trigger": trigger,
                "cwd": str(self.code_root),
            }
        )

    def session_start_payload(self, session_id, source="startup"):
        return json.dumps(
            {
                "session_id": session_id,
                "hook_event_name": "SessionStart",
                "source": source,
                "cwd": str(self.code_root),
            }
        )

    def user_prompt_payload(
        self, session_id, source=None, agent_id=None, agent_type=None, prompt_id=None,
        prompt_key="prompt", prompt_text=None,
    ):
        """Real-shaped by default (fix-round 2026-08-31): a live
        UserPromptSubmit payload carries no `source` field at all -- that
        field belongs to SessionStart -- so `source` is omitted unless a
        test explicitly passes one (to prove its presence, if it ever
        shows up again, is a no-op the hook ignores).

        `prompt_key` defaults to "prompt" (TOP-0133 L2, Addendum A): real
        UserPromptSubmit payloads on this machine carry the prompt under
        `prompt`, never `user_input` -- hook.log's own `payload_keys=`
        lines are the evidence (see docs/INTERNALS.md). A test that needs
        the OTHER key (proving that one is equally never dereferenced)
        passes prompt_key="user_input" explicitly.

        `prompt_text` (TOP-0133 L2): the value at `prompt_key`, default a
        sentinel string standing in for real content ("this text must
        never be read by the hook") for every test that predates the
        prompt-query channel and just needs SOME string there. A
        prompt-query test passes a real (or deliberately term-poor/
        stopword-only) sentence instead."""
        d = {
            "session_id": session_id,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.code_root),
            prompt_key: prompt_text if prompt_text is not None else "this text must never be read by the hook",
        }
        if source is not None:
            d["source"] = source
        if agent_id:
            d["agent_id"] = agent_id
            d["agent_type"] = agent_type or "Explore"
        if prompt_id:
            d["prompt_id"] = prompt_id
        return json.dumps(d)

    def session_end_payload(self, session_id, reason="other"):
        return json.dumps(
            {
                "session_id": session_id,
                "hook_event_name": "SessionEnd",
                "reason": reason,
                "cwd": str(self.code_root),
            }
        )

    # ---- convenience: seed a ledger directly (skip N ledger-hook calls) --

    def seed_ledger(self, session_id, entries):
        """entries: list of (path, kind, content) or (path, kind) tuples."""
        state_file = self.state_file(session_id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if state_file.exists():
            state = json.loads(state_file.read_text())
        import hashlib

        ledger = state.setdefault("ledger", [])
        for entry in entries:
            path, kind = entry[0], entry[1]
            content = entry[2] if len(entry) > 2 else path
            sha = hashlib.sha256(content.encode()).hexdigest()
            ledger.append({"path": path, "kind": kind, "content_sha256": sha, "seen_at": time.time()})
        state.setdefault("session_id", session_id)
        state.setdefault("project", self.project)
        state.setdefault("user_turn_count", 0)
        state.setdefault("last_inject_turn", -999)
        state.setdefault("last_inject_time", 0)
        state.setdefault("last_injected_pairs", [])
        state_file.write_text(json.dumps(state))
        return state_file

    def patch_state(self, session_id, **fields):
        """Directly overwrite/merge fields into a session's state file --
        used by look-back tests to engineer specific turn/timestamp
        baselines without running N real hook invocations."""
        state_file = self.state_file(session_id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if state_file.exists():
            state = json.loads(state_file.read_text())
        state.update(fields)
        state_file.write_text(json.dumps(state))
        return state_file


# ---------------------------------------------------------------------------
# 2. ledger-post-edit.sh
# ---------------------------------------------------------------------------


class TestLedgerPostEdit(HookTestBase):
    def test_bash_syntax_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(LEDGER_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backward_clock_never_produces_a_negative_elapsed(self):
        """Fix round (Codex 6 / Grok 6 / Grok 15): the same elapsed-clamp
        flake hooks/pre-edit-chain.sh's own finish() had -- a fake `date
        +%s` that steps BACKWARDS between START_TS and finish()'s own `now`
        reproduces the real CI flake (a negative elapsed) deterministically,
        no reliance on an actual NTP correction firing mid-run. Every
        `date +%s` call after the first one returns the SAME stepped-back
        value here (not just the second), so this is robust regardless of
        how many such calls this script makes before finish() (it makes
        three: START_TS, MC_NOW, and finish()'s own `now`)."""
        marker = Path(self.td) / "date-called-once"
        real_date = shutil.which("date") or "/bin/date"
        fake_bin = Path(self.td) / "fakebin"
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
        session_id = "s-ledger-elapsed-clamp"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        env = self.base_env(PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Grok 4 (fix round 2, external re-gate): prove the fake `date` was
        # actually invoked (see the matching comment in test_hooks.py's own
        # copy of this test) -- otherwise a fast real run passes the two
        # checks below for an unrelated reason and the backward step was
        # never exercised.
        self.assertTrue(marker.exists(),
                         "fake date was never called a second time -- the backward-clock "
                         "scenario was not exercised, this test proves nothing")
        log_text = (self.home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "ledger outcome=" in l]
        self.assertTrue(matching, log_text)
        for line in matching:
            self.assertRegex(line, r"elapsed=\d+s", line)
            self.assertNotIn("elapsed=-", line)
            # 1000000100 - 1000000200 = -100 unclamped; a genuine clamp
            # shows exactly 0, not merely "some non-negative number" (which
            # a fast real run would also produce with no fake date at all).
            self.assertRegex(line, r"elapsed=0s", line)

    def test_silent_always(self):
        session_id = "s-ledger-silent"
        payload = self.post_tool_use_payload(session_id, str(self.code_root / "src" / "mapped.py"))
        proc, elapsed = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_appends_path_under_code_root(self):
        session_id = "s-ledger-code"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertIn(fpath, paths)
        entry = [e for e in state["ledger"] if e["path"] == fpath][0]
        self.assertEqual(entry["kind"], "code")
        self.assertTrue(entry.get("content_sha256"))

    def test_log_line_carries_project(self):
        """Liveness metric fix: every ledger-post-edit.sh outcome line must
        carry project=<MC_PROJECT> (via mc_log in memlib.sh)."""
        session_id = "s-ledger-project"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        run_script(LEDGER_HOOK, payload, self.base_env())
        log_text = (self.home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "ledger" in l]
        self.assertTrue(matching)
        for line in matching:
            self.assertIn(f"project={self.project}", line, line)

    def test_appends_path_under_store_root(self):
        session_id = "s-ledger-store"
        fpath = str(self.store_root / "topics" / "testing" / "mapped-topic.md")
        payload = self.post_tool_use_payload(session_id, fpath, tool_name="Write")
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == fpath][0]
        self.assertEqual(entry["kind"], "store")

    def test_ignores_path_outside_both_roots(self):
        session_id = "s-ledger-outside"
        fpath = "/tmp/somewhere/else/file.py"
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertNotIn(fpath, paths)

    def test_appends_path_under_a_symlinked_parent_into_the_store(self):
        # mc_path_under_root (hooks/memlib.sh): a path that reaches the
        # store only through a symlinked ancestor -- every segment lexically
        # OUTSIDE MEMCONTINUUM_ROOT's own string, but resolving physically
        # into it -- must still count as under-store. The old lexical
        # `case "$FILE_PATH" in "$MEMCONTINUUM_ROOT"/*` prefix match could
        # never see this (it only ever matched a literal prefix), the
        # mirror-image gap of newfile-nudge.sh's own symlink fix.
        store_link = Path(self.td) / "store-link"
        store_link.symlink_to(self.store_root, target_is_directory=True)
        session_id = "s-ledger-symlinked-store"
        fpath = str(store_link / "topics" / "testing" / "mapped-topic.md")
        payload = self.post_tool_use_payload(session_id, fpath, tool_name="Write")
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == fpath][0]
        self.assertEqual(entry["kind"], "store")

    def test_silent_for_a_symlinked_parent_escaping_the_code_root(self):
        # Mirror-image of newfile-nudge.sh's own
        # test_silent_for_a_symlinked_parent_escaping_the_code_root: a path
        # that is lexically under the code root at every segment, but whose
        # nearest EXISTING ancestor directory is actually a symlink pointing
        # OUTSIDE it, must stay out-of-scope -- never appended to the
        # ledger, never advancing the growth signal.
        real_outside = Path(self.td) / "real-outside"
        real_outside.mkdir()
        escape_link = self.code_root / "escape-link"
        escape_link.symlink_to(real_outside, target_is_directory=True)
        session_id = "s-ledger-escape-link"
        fpath = str(escape_link / "Escaped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertNotIn(fpath, paths)

    def test_silent_for_a_dot_dot_traversal_path(self):
        # Mirror-image of newfile-nudge.sh's own
        # test_silent_for_a_dot_dot_traversal_path: `<code_root>/../outside/x.py`
        # starts with the code-root string textually while actually
        # resolving to a sibling directory OUTSIDE it. Must stay
        # out-of-scope.
        outside_sibling = Path(self.td) / "outside"
        outside_sibling.mkdir()
        session_id = "s-ledger-traversal"
        fpath = f"{self.code_root}/../outside/Escaped.py"
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertNotIn(fpath, paths)

    def test_dedupes_same_path(self):
        session_id = "s-ledger-dedupe"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        run_script(LEDGER_HOOK, payload, self.base_env())
        _write(Path(fpath), "# mapped changed\n")
        run_script(LEDGER_HOOK, payload, self.base_env())
        state = self.load_state(session_id)
        matching = [e for e in state["ledger"] if e["path"] == fpath]
        self.assertEqual(len(matching), 1)

    def test_runs_in_subagents_too(self):
        session_id = "s-ledger-subagent"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath, agent_id="agent-xyz")
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertIn(fpath, paths)

    def test_isolation_between_sessions(self):
        fpath_a = str(self.code_root / "src" / "mapped.py")
        fpath_b = str(self.code_root / "src" / "unmapped.py")
        run_script(LEDGER_HOOK, self.post_tool_use_payload("s-A", fpath_a), self.base_env())
        run_script(LEDGER_HOOK, self.post_tool_use_payload("s-B", fpath_b), self.base_env())
        state_a = self.load_state("s-A")
        state_b = self.load_state("s-B")
        self.assertEqual([e["path"] for e in state_a["ledger"]], [fpath_a])
        self.assertEqual([e["path"] for e in state_b["ledger"]], [fpath_b])

    def test_parallel_sessions_no_cross_talk_concurrent(self):
        """Two sessions' ledger hooks fired concurrently must not corrupt or
        merge each other's state (lock + atomic rename, keyed by session_id)."""
        import threading

        results = {}

        def fire(sid, fpath):
            payload = self.post_tool_use_payload(sid, fpath)
            proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
            results[sid] = proc.returncode

        threads = []
        for i in range(6):
            sid = f"s-par-{i % 2}"
            fpath = str(self.code_root / "src" / f"file{i}.py")
            _write(Path(fpath), f"# {i}\n")
            t = threading.Thread(target=fire, args=(sid, fpath))
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        for rc in results.values():
            self.assertEqual(rc, 0)
        state0 = self.load_state("s-par-0")
        state1 = self.load_state("s-par-1")
        paths0 = {e["path"] for e in state0.get("ledger", [])}
        paths1 = {e["path"] for e in state1.get("ledger", [])}
        self.assertEqual(len(paths0), 3)
        self.assertEqual(len(paths1), 3)
        self.assertEqual(paths0 & paths1, set())

    def test_fail_open_on_missing_python(self):
        session_id = "s-ledger-nopython"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        env = self.base_env(MEMCONTINUUM_PYTHON="/no/such/python/binary")
        proc, elapsed = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_fail_open_on_malformed_payload(self):
        env = self.base_env()
        proc, _ = run_script(LEDGER_HOOK, "{ not json ]]", env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_store_byte_identical_before_and_after(self):
        session_id = "s-ledger-bytesafe"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertTrue(git_is_clean(self.store_root))

    def test_completes_under_poisoned_pythonpath(self):
        session_id = "s-ledger-poison"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, elapsed = run_script(LEDGER_HOOK, payload, self.poisoned_base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.0)
        state = self.load_state(session_id)
        self.assertIn(fpath, [e["path"] for e in state.get("ledger", [])])

    # ---- look-back addendum: last_growth_turn / last_growth_ts bookkeeping --

    def test_new_pair_on_fresh_session_sets_growth_turn_zero(self):
        session_id = "s-ledger-growth-fresh"
        fpath = str(self.code_root / "src" / "mapped.py")
        before = time.time()
        proc, _ = run_script(
            LEDGER_HOOK, self.post_tool_use_payload(session_id, fpath), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        self.assertEqual(state.get("last_growth_turn"), 0)
        # MC_NOW is whole-second `date +%s` (matches the rest of the
        # codebase's epoch convention) -- allow 1s of truncation slack.
        self.assertGreaterEqual(state.get("last_growth_ts", 0), before - 1)

    def test_new_pair_stamps_growth_at_current_user_turn_count(self):
        session_id = "s-ledger-growth-turn"
        self.patch_state(session_id, user_turn_count=4)
        fpath = str(self.code_root / "src" / "mapped.py")
        proc, _ = run_script(
            LEDGER_HOOK, self.post_tool_use_payload(session_id, fpath), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        self.assertEqual(state.get("last_growth_turn"), 4)

    def test_identical_rewrite_of_same_path_is_not_growth(self):
        session_id = "s-ledger-growth-samesha"
        fpath = self.code_root / "src" / "mapped.py"
        payload = self.post_tool_use_payload(session_id, str(fpath))
        self.patch_state(session_id, user_turn_count=3)
        run_script(LEDGER_HOOK, payload, self.base_env())
        state = self.load_state(session_id)
        # dual-gate finding 6: pin that the FIRST write actually stamped
        # growth (from the hook, not from a value we seeded ourselves) --
        # otherwise the final assertEqual below would pass vacuously on a
        # hook that never touches last_growth_turn at all.
        self.assertEqual(state.get("last_growth_turn"), 3)
        self.patch_state(session_id, user_turn_count=10)
        # same content -> same sha -> re-appending is a no-op, not growth
        run_script(LEDGER_HOOK, payload, self.base_env())
        state = self.load_state(session_id)
        self.assertEqual(state.get("last_growth_turn"), 3)

    def test_rewrite_with_new_content_on_same_path_is_growth(self):
        session_id = "s-ledger-growth-newsha"
        fpath = self.code_root / "src" / "mapped.py"
        payload = self.post_tool_use_payload(session_id, str(fpath))
        run_script(LEDGER_HOOK, payload, self.base_env())
        self.patch_state(session_id, user_turn_count=7, last_growth_turn=0)
        _write(fpath, "# mapped changed for growth test\n")
        run_script(LEDGER_HOOK, payload, self.base_env())
        state = self.load_state(session_id)
        self.assertEqual(state.get("last_growth_turn"), 7)

    def test_watchdog_guard_resolves_python_via_config_sh(self):
        """F6 regression, round 4: the watchdog guard's own MC_GUARD_PY
        resolution (hooks/mc-watchdog.sh) used to be two-step only (env
        $MEMCONTINUUM_PYTHON, else the engine's <engine>/.venv/bin/python)
        -- skipping the config.sh middle step hooks/memlib.sh already
        consults for MC_PY. A non-default venv left the guard itself dead:
        it would fall through UNGUARDED (per its own header comment)
        straight to `source memlib.sh`, which DOES still find the right
        python via its own config.sh step -- so the hook would still work,
        just with no watchdog wrapped around it at all.

        Proven by boundedness, not by inspecting a resolved path: with
        MEMCONTINUUM_PYTHON unset and no engine .venv in this checkout, a
        config.sh at $MEMCONTINUUM_HOME/config.sh is the ONLY way
        MC_GUARD_PY can resolve to an executable. Point it at a python
        wrapper that execs the real venv python immediately for the
        launcher's own `-c $MC_WATCHDOG_LAUNCHER_PY` call (so the launcher
        itself always starts fine) but sleeps 6s for every OTHER call --
        i.e. every real hook-work call memlib.sh's helpers make. If the
        guard actually engaged (MC_GUARD_PY resolved via config.sh), the
        whole run is bounded to the watchdog's own ~2s budget; if the guard
        fell through unguarded, nothing bounds the 6s sleep and the run
        takes ~6s instead."""
        if (HOOKS_DIR / ".." / ".venv" / "bin" / "python").resolve().exists():
            self.skipTest(
                "an engine .venv exists in this checkout (INC-0125) -- this "
                "test can only prove python resolves through the "
                "config-pointer chain when no engine venv exists to mask a "
                "broken chain; see tests/test_env_gate.py"
            )
        hang_py = Path(self.td) / "hang-python-config-guard"
        hang_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *MC_WATCHDOG_LAUNCHER*) exec \"" + VENV_PYTHON + "\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            "sleep 6\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        hang_py.chmod(0o755)
        (self.home / "config.sh").write_text(f'MEMCONTINUUM_PYTHON="{hang_py}"\n')

        env = self.base_env()
        del env["MEMCONTINUUM_PYTHON"]
        session_id = "s-ledger-config-guard"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, elapsed = run_script(LEDGER_HOOK, payload, env, timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(
            elapsed,
            2.5,
            f"the watchdog guard must have resolved MC_GUARD_PY via config.sh and bounded "
            f"the run to its own ~2s budget, took {elapsed:.3f}s",
        )

    def test_control_watchdog_guard_without_config_sh_step_is_unbounded(self):
        """Control experiment (project rule: a fix's test must fail on the
        pre-fix code, or it's decoration): a copy of mc-watchdog.sh with
        the config.sh resolution step stripped back out (MC_WATCHDOG_LIB_PATH
        seam, same one every hook's guard preamble already reads) leaves
        MC_GUARD_PY unresolvable (env unset, no engine .venv) -- the guard
        then falls through UNGUARDED and the hang wrapper's 6s sleep is NOT
        bounded by any watchdog, proving the boundedness assertion above is
        load-bearing, not decoration.

        Carries the same no-engine-.venv precondition as the test above
        (found missing here, INC-0125): with a real engine .venv present,
        the broken watchdog's own final fallback (<engine>/.venv/bin/python)
        resolves to a working interpreter and the run IS bounded, which
        would read as this control failing to reproduce the pre-fix
        behavior rather than as an unmet precondition."""
        if (HOOKS_DIR / ".." / ".venv" / "bin" / "python").resolve().exists():
            self.skipTest(
                "an engine .venv exists in this checkout (INC-0125) -- this "
                "test can only prove the guard falls through unbounded when "
                "no engine venv exists for its own final fallback to find; "
                "see tests/test_env_gate.py"
            )
        original = (HOOKS_DIR / "mc-watchdog.sh").read_text()
        needle = (
            'MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"\n'
            'MC_HOME_CONFIG_1="$MEMCONTINUUM_HOME/config.sh"\n'
            'if [ -f "$MC_HOME_CONFIG_1" ]; then\n'
            '    # shellcheck source=/dev/null\n'
            '    . "$MC_HOME_CONFIG_1" 2>/dev/null || true\n'
            'fi\n'
            '# A damaged-but-sourceable config may have `unset MEMCONTINUUM_HOME` --\n'
            '# re-default after every source so `set -u` can never trip on it (regate\n'
            '# round 2).\n'
            'MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"\n'
            'if [ "$MEMCONTINUUM_HOME/config.sh" != "$MC_HOME_CONFIG_1" ] && [ -f "$MEMCONTINUUM_HOME/config.sh" ]; then\n'
            '    # shellcheck source=/dev/null\n'
            '    . "$MEMCONTINUUM_HOME/config.sh" 2>/dev/null || true\n'
            '    MEMCONTINUUM_HOME="${MEMCONTINUUM_HOME:-$HOME/.memcontinuum}"\n'
            'fi\n'
            'unset MC_HOME_CONFIG_1\n'
        )
        self.assertIn(needle, original)
        broken = original.replace(needle, "", 1)
        self.assertNotEqual(broken, original)
        broken_watchdog = Path(self.td) / "mc-watchdog-broken-control.sh"
        broken_watchdog.write_text(broken)

        hang_py = Path(self.td) / "hang-python-control"
        hang_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *MC_WATCHDOG_LAUNCHER*) exec \"" + VENV_PYTHON + "\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            "sleep 6\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        hang_py.chmod(0o755)
        (self.home / "config.sh").write_text(f'MEMCONTINUUM_PYTHON="{hang_py}"\n')

        env = self.base_env(MC_WATCHDOG_LIB_PATH=str(broken_watchdog))
        del env["MEMCONTINUUM_PYTHON"]
        session_id = "s-ledger-config-guard-control"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, elapsed = run_script(LEDGER_HOOK, payload, env, timeout=20.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreaterEqual(
            elapsed,
            4.0,
            "with the config.sh step stripped from the guard, the run must NOT be "
            f"bounded by any watchdog -- proving the real test's assertion is "
            f"load-bearing, took only {elapsed:.3f}s",
        )

    def test_fail_open_when_watchdog_lib_missing(self):
        """R1 regression, round 4 gate: mc-watchdog.sh missing/unsourceable
        (e.g. the engine moved) used to leave MC_GUARD_PY completely unset
        -- under `set -u`, the guard preamble's `[ -x "$MC_GUARD_PY" ]` then
        aborted the hook with 'unbound variable' instead of falling through
        unguarded, turning a PostToolUse hook into a real failure instead
        of failing open. Must still exit 0 and do the real ledger work."""
        session_id = "s-ledger-watchdog-lib-missing"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        env = self.base_env(MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh")
        proc, _elapsed = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        self.assertIn(fpath, [e["path"] for e in state.get("ledger", [])])

    def test_regate2_damaged_config_unsetting_home_still_fails_open(self):
        """Regate round 2 (Codex): a damaged-but-sourceable config.sh that
        does `unset MEMCONTINUUM_HOME` used to leave the next bare
        $MEMCONTINUUM_HOME expansion to abort the hook under `set -u`.
        Every post-source use must re-default instead: exit 0, no 'unbound
        variable', real work still done (under the $HOME fallback)."""
        session_id = "s-ledger-damaged-config"
        fake_home = Path(self.td) / "regate2-fake-home"
        fake_home.mkdir()
        (self.home / "config.sh").write_text("unset MEMCONTINUUM_HOME\n")
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        env = self.base_env(HOME=str(fake_home))
        proc, _elapsed = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        # MEMCONTINUUM_HOME env is still set here (base_env), so the unset
        # only bites INSIDE the sourcing chain; the state must land under
        # the re-defaulted home, not vanish.
        state_file = fake_home / ".memcontinuum" / "sessions" / self.project / f"{session_id}.json"
        state = json.loads(state_file.read_text()) if state_file.exists() else self.load_state(session_id)
        self.assertIn(fpath, [e["path"] for e in state.get("ledger", [])])

    def test_regate2_watchdog_exports_resolved_home_for_reexec_child(self):
        """Regate round 2 (Codex): the guard resolved MEMCONTINUUM_HOME
        (possibly via the pointer) but never exported it, so the re-exec'd
        child re-defaulted HOME and -- with PYTHON baked -- wrote hook.log
        and session state under ~/.memcontinuum again. After sourcing
        mc-watchdog.sh, MEMCONTINUUM_HOME must be exported (visible in
        `env`), carrying the pointer-resolved value."""
        real_home = Path(self.td) / "regate2-real-home"
        default_home = Path(self.td) / "regate2-default-home" / ".memcontinuum"
        real_home.mkdir()
        default_home.mkdir(parents=True)
        (default_home / "config.sh").write_text(
            f"MEMCONTINUUM_HOME='{real_home}'\n"
        )
        probe = (
            "set -u; "
            f"SCRIPT_DIR='{HOOKS_DIR}'; "
            f"HOME='{default_home.parent}'; "
            "unset MEMCONTINUUM_HOME 2>/dev/null; "
            f"source '{HOOKS_DIR}/mc-watchdog.sh'; "
            "env | grep '^MEMCONTINUUM_HOME='"
        )
        env = clean_env(MEMCONTINUUM_PYTHON=VENV_PYTHON)
        env.pop("MEMCONTINUUM_HOME", None)
        proc = subprocess.run(
            [MC_BASH, "-c", probe], capture_output=True, text=True, env=env, timeout=10
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), f"MEMCONTINUUM_HOME={real_home}")

    def test_r3_session_state_lands_under_custom_home_with_baked_python(self):
        """R3 regression, round 4 gate: installer-rendered hook lines bake
        MEMCONTINUUM_PYTHON directly, so the old single-source-if-PYTHON-
        unset guard in the watchdog guard AND memlib.sh never even looked
        at config.sh on a freshly installer-wired repo -- MEMCONTINUUM_HOME
        stayed at the fixed default even though the registry (and the
        pointer at that default path) say the real home is elsewhere.
        Session state, hook.log, and the sqlite index all landed under the
        WRONG (default) home. HOME resolution must run unconditionally,
        independent of whether PYTHON already resolved."""
        fake_home = Path(self.td) / "r3-fake-home"
        default_mc_home = fake_home / ".memcontinuum"
        default_mc_home.mkdir(parents=True)
        custom_home = Path(self.td) / "r3-custom-mc-home"
        custom_home.mkdir()
        (default_mc_home / "config.sh").write_text(
            f"MEMCONTINUUM_HOME='{custom_home}'\n"
        )
        # The real config.sh at the custom home -- deliberately carries NO
        # MEMCONTINUUM_PYTHON of its own; this test's env bakes PYTHON
        # directly (matching an installer-rendered hook line), so PYTHON
        # resolution must NOT be what triggers HOME to follow the pointer.
        (custom_home / "config.sh").write_text(f"MEMCONTINUUM_HOME='{custom_home}'\n")

        session_id = "s-ledger-r3-custom-home"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        env = clean_env(
            HOME=str(fake_home),
            MEMCONTINUUM_PROJECT=self.project,
            MEMCONTINUUM_ROOT=str(self.store_root),
            MEMCONTINUUM_CODE_ROOT=str(self.code_root),
            MEMCONTINUUM_PYTHON=VENV_PYTHON,  # baked, like a real hook line
        )
        env.pop("MEMCONTINUUM_HOME", None)
        proc, _elapsed = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state_file = custom_home / "sessions" / self.project / f"{session_id}.json"
        self.assertTrue(
            state_file.exists(),
            f"session state must land under the REAL custom home ({custom_home}), "
            f"not the default one the pointer lives at",
        )
        default_state_file = (
            default_mc_home / "sessions" / self.project / f"{session_id}.json"
        )
        self.assertFalse(default_state_file.exists())
        self.assertTrue((custom_home / "hook.log").exists())
        self.assertFalse((default_mc_home / "hook.log").exists())


# ---------------------------------------------------------------------------
# 2b. Mutation-surface honesty (design R6, audit MC-P1-04, TOP-0123 L6)
# ---------------------------------------------------------------------------


class TestLedgerPostEditWorktreeGap(HookTestBase):
    """Worktree gap fix (docs/internal/SESSION-HANDOFF-releases-0.3-to-0.6.md
    SS"0.2.0 final" item 1): mc_remap_worktree_path (hooks/mc-path-lib.sh),
    tried only once the existing root-containment loop has already found
    nothing for FILE_PATH itself. self.code_root is the wired root
    (HookTestBase.setUp); repo_b below is a second, real, never-wired repo."""

    def setUp(self):
        super().setUp()
        self.code_root_wt = Path(self.td) / "code-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-a", str(self.code_root_wt)],
            cwd=self.code_root, check=True,
        )
        self.repo_b = Path(self.td) / "repo-b"
        _write(self.repo_b / "src" / "other.py", "# b\n")
        git_init(self.repo_b)
        self.repo_b_wt = Path(self.td) / "repo-b-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-b", str(self.repo_b_wt)],
            cwd=self.repo_b, check=True,
        )

    def _env(self, **overrides):
        # Resolved, same convention test_bash_edit_under_a_worktree_code_
        # root_is_still_diffed already uses (and real repo-init.sh wiring
        # always does, via os.path.realpath at render time): on macOS CI,
        # tempfile's own tmpdir sits behind a /var -> /private/var symlink,
        # so an UNRESOLVED MEMCONTINUUM_CODE_ROOT here would silently diverge
        # from mc_remap_worktree_path's own `cd && pwd -P` resolution the
        # instant it's compared -- resolving up front keeps this test class
        # platform-independent rather than accidentally Linux-only.
        return self.base_env(
            MEMCONTINUUM_CODE_ROOT=str(self.code_root.resolve()),
            MEMCONTINUUM_CODE_ROOTS=json.dumps([str(self.code_root.resolve())]),
            **overrides,
        )

    def _last_outcome_line(self, log_text):
        matching = [l for l in log_text.splitlines() if "ledger outcome=" in l]
        self.assertTrue(matching, log_text)
        return matching[-1]

    def test_worktree_of_wired_repo_gets_same_root_as_main(self):
        session_id = "s-ledger-wt-wired"
        wt_file = str(self.code_root_wt / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, wt_file)
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        main_path = str((self.code_root / "src" / "mapped.py").resolve())
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == main_path]
        self.assertEqual(len(entry), 1, state.get("ledger"))
        entry = entry[0]
        self.assertEqual(entry["kind"], "code")
        self.assertEqual(entry["root"], str(self.code_root.resolve()))
        self.assertEqual(entry["worktree_path"], wt_file)
        self.assertTrue(entry.get("content_sha256"))

        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=appended kind=code remap=1", self._last_outcome_line(log_text))

    def test_content_hash_reads_the_real_worktree_file_not_the_main_copy(self):
        """The main checkout's own src/mapped.py must never be read for a
        worktree edit's content hash -- the worktree can (and typically
        does) hold different, uncommitted content at the same relative
        path."""
        session_id = "s-ledger-wt-content"
        wt_target = self.code_root_wt / "src" / "mapped.py"
        wt_target.write_text("# mapped, changed ONLY in the worktree\n")
        payload = self.post_tool_use_payload(session_id, str(wt_target))
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        main_path = str((self.code_root / "src" / "mapped.py").resolve())
        entry = [e for e in state["ledger"] if e["path"] == main_path][0]
        expected_sha = hashlib.sha256(wt_target.read_bytes()).hexdigest()
        self.assertEqual(entry["content_sha256"], expected_sha)

    def test_worktree_path_cleared_on_a_later_main_checkout_edit(self):
        session_id = "s-ledger-wt-clear"
        wt_file = str(self.code_root_wt / "src" / "mapped.py")
        run_script(LEDGER_HOOK, self.post_tool_use_payload(session_id, wt_file), self._env())
        main_path = str((self.code_root / "src" / "mapped.py").resolve())
        state = self.load_state(session_id)
        self.assertIn("worktree_path", [e for e in state["ledger"] if e["path"] == main_path][0])

        run_script(LEDGER_HOOK, self.post_tool_use_payload(session_id, main_path), self._env())
        state2 = self.load_state(session_id)
        entry2 = [e for e in state2["ledger"] if e["path"] == main_path][0]
        self.assertNotIn("worktree_path", entry2)

    def test_worktree_of_unwired_repo_distinct_outcome_no_ledger_entry(self):
        session_id = "s-ledger-wt-unwired"
        wt_file = str(self.repo_b_wt / "src" / "other.py")
        payload = self.post_tool_use_payload(session_id, wt_file)
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertNotIn(wt_file, paths)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=worktree-unwired", self._last_outcome_line(log_text))

    def test_main_checkout_edit_pays_no_git_call(self):
        fake_git_dir = Path(self.td) / "fake-git-bin-noop"
        fake_git_dir.mkdir()
        marker = Path(self.td) / "git-was-called"
        fake_git = fake_git_dir / "git"
        fake_git.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        fake_git.chmod(0o755)

        session_id = "s-ledger-wt-nogit"
        fpath = str(self.code_root / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        env = self._env(PATH=f"{fake_git_dir}:{os.environ.get('PATH', '')}")
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker.exists(), "git must not be invoked for a path already under a configured root")
        state = self.load_state(session_id)
        # No remap fires here (already under the configured root) -- the
        # ledger row's path is FILE_PATH exactly as passed in, same as
        # every pre-existing (non-worktree) ledger test already asserts.
        self.assertIn(fpath, [e["path"] for e in state.get("ledger", [])])

    def _git_free_root(self):
        """A fresh directory none of whose ancestors (itself included) holds a
        `.git` entry -- the precondition of the bash-only gate in
        mc_remap_worktree_path. Tries the usual temp roots in turn; skips the
        test when this machine has none, rather than asserting on a path the
        gate would legitimately probe with git."""
        def clean(d):
            probe = Path(d)
            while True:
                if os.path.lexists(probe / ".git"):
                    return False
                if probe == probe.parent:
                    return True
                probe = probe.parent
        for base in (tempfile.gettempdir(), "/dev/shm", "/var/tmp", str(Path.home())):
            if not os.path.isdir(base) or not os.access(base, os.W_OK) or not clean(base):
                continue
            root = Path(tempfile.mkdtemp(prefix="mc-no-git-", dir=base))
            self.addCleanup(shutil.rmtree, str(root), True)
            if clean(root):
                return root
        self.skipTest("no writable temp root without a .git ancestor on this machine")

    def test_outside_scope_edit_pays_no_git_call(self):
        """A path outside every root and not in any git repo at all (the
        vast majority of real out-of-scope edits) must also cost nothing:
        the bash-only `.git`-entry gate inside mc_remap_worktree_path
        rules it out before ever spawning git."""
        fake_git_dir = Path(self.td) / "fake-git-bin-noop2"
        fake_git_dir.mkdir()
        marker = Path(self.td) / "git-was-called-2"
        fake_git = fake_git_dir / "git"
        fake_git.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\nexit 1\n")
        fake_git.chmod(0o755)

        session_id = "s-ledger-wt-nogit-outside"
        # The gate walks every ancestor of the edited path up to "/" looking
        # for a `.git` entry, so a path under /tmp is only "in no repo" while
        # no stray /tmp/.git exists (something on a dev machine recreates an
        # empty one, INC-0130 follow-up). Build the path under a root whose
        # whole ancestor chain is verified .git-free instead of assuming it.
        fpath = str(self._git_free_root() / "somewhere" / "else" / "not-in-any-repo.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        env = self._env(PATH=f"{fake_git_dir}:{os.environ.get('PATH', '')}")
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(marker.exists())
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=out-of-scope", self._last_outcome_line(log_text))

    def test_git_failure_on_worktree_gives_unresolved_not_crash(self):
        fake_git_dir = Path(self.td) / "fake-git-bin-fail"
        fake_git_dir.mkdir()
        fake_git = fake_git_dir / "git"
        fake_git.write_text("#!/bin/sh\nexit 128\n")
        fake_git.chmod(0o755)

        session_id = "s-ledger-wt-gitfail"
        wt_file = str(self.code_root_wt / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, wt_file)
        env = self._env(PATH=f"{fake_git_dir}:{os.environ.get('PATH', '')}")
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        self.assertEqual(state.get("ledger", []), [])
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=worktree-unresolved", self._last_outcome_line(log_text))

    def test_in_root_worktree_ledger_remapped(self):
        """Round 2 BLOCKER (Grok): a worktree checked out INSIDE the wired
        code root (e.g. `<root>/.worktrees/feat/`) used to be skipped by
        the remap outright -- FILE_PATH is physically under the root by
        plain containment (the pre-fix gate treated "already under a
        configured root" as "nothing left to check"), so the ledger row
        used to be recorded under the WORKTREE's own path/root annotation
        instead of the main-checkout-equivalent one."""
        in_root_wt = self.code_root / ".worktrees" / "feat"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-in-root", str(in_root_wt)],
            cwd=self.code_root, check=True,
        )
        session_id = "s-ledger-wt-inroot"
        wt_file = str(in_root_wt / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, wt_file)
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        main_path = str((self.code_root / "src" / "mapped.py").resolve())
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == main_path]
        self.assertEqual(len(entry), 1, state.get("ledger"))
        entry = entry[0]
        self.assertEqual(entry["kind"], "code")
        self.assertEqual(entry["root"], str(self.code_root.resolve()))
        self.assertEqual(entry["worktree_path"], wt_file)
        self.assertTrue(entry.get("content_sha256"))

        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=appended kind=code remap=1", self._last_outcome_line(log_text))

    def test_configured_root_that_is_itself_a_linked_worktree(self):
        """Grok's second MAJOR / Codex's MAJOR: the configured code root
        can itself be a linked worktree -- its own `.git` is a FILE, not a
        directory, so the old `cd "$root/.git"` identity check silently
        skipped this root for every worktree of the same repo.
        self.code_root_wt (already a sibling worktree of self.code_root,
        HookTestBase.setUp) is wired as the root here; a SECOND sibling
        worktree of the same repo is edited."""
        second_wt = Path(self.td) / "code-second-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-second", str(second_wt)],
            cwd=self.code_root, check=True,
        )
        env = self.base_env(
            MEMCONTINUUM_CODE_ROOT=str(self.code_root_wt.resolve()),
            MEMCONTINUUM_CODE_ROOTS=json.dumps([str(self.code_root_wt.resolve())]),
        )
        session_id = "s-ledger-wt-root-is-wt"
        wt_file = str(second_wt / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, wt_file)
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        main_path = str((self.code_root_wt / "src" / "mapped.py").resolve())
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == main_path]
        self.assertEqual(len(entry), 1, state.get("ledger"))
        self.assertEqual(entry[0]["root"], str(self.code_root_wt.resolve()))

    def test_separate_git_dir_root_remaps_worktree(self):
        """A configured root created via `git init --separate-git-dir`
        also has a `.git` FILE at its own top -- the same layout defect
        as a root that is itself a linked worktree, covered by the same
        fix."""
        sep_root = Path(self.td) / "code-sep"
        sep_gitdir = Path(self.td) / "code-sep-gitdir"
        sep_root.mkdir()
        subprocess.run(["git", "init", "-q", f"--separate-git-dir={sep_gitdir}"], cwd=sep_root, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=sep_root, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=sep_root, check=True)
        _write(sep_root / "src" / "mapped.py", "# mapped\n")
        subprocess.run(["git", "add", "-A"], cwd=sep_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=sep_root, check=True)
        sep_wt = Path(self.td) / "code-sep-wt"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-sep", str(sep_wt)], cwd=sep_root, check=True,
        )

        env = self.base_env(
            MEMCONTINUUM_CODE_ROOT=str(sep_root.resolve()),
            MEMCONTINUUM_CODE_ROOTS=json.dumps([str(sep_root.resolve())]),
        )
        session_id = "s-ledger-wt-sep"
        wt_file = str(sep_wt / "src" / "mapped.py")
        payload = self.post_tool_use_payload(session_id, wt_file)
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        main_path = str((sep_root / "src" / "mapped.py").resolve())
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == main_path]
        self.assertEqual(len(entry), 1, state.get("ledger"))

    def test_git_dir_env_poisoning_unwired_worktree_not_remapped(self):
        """Grok's first MAJOR: mc_remap_worktree_path's own `git` calls
        must ignore an ambient GIT_DIR/GIT_WORK_TREE/GIT_COMMON_DIR --
        here GIT_DIR points at the WIRED code root while the edited file
        is a worktree of the UNWIRED repo_b; a leaked GIT_DIR must never
        turn that into a false match onto the wired root's records."""
        env = self._env(GIT_DIR=str(self.code_root.resolve() / ".git"))
        session_id = "s-ledger-wt-gitdir-poison"
        wt_file = str(self.repo_b_wt / "src" / "other.py")
        payload = self.post_tool_use_payload(session_id, wt_file)
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        self.assertNotIn(wt_file, [e["path"] for e in state.get("ledger", [])])
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=worktree-unwired", self._last_outcome_line(log_text))

    def test_junk_gitfile_below_unwired_dir_gives_unresolved(self):
        junk_dir = Path(self.td) / "junk-unwired"
        (junk_dir / "sub").mkdir(parents=True)
        (junk_dir / ".git").write_text("not a real gitfile\n")
        target = junk_dir / "sub" / "file.py"
        target.write_text("# x\n")
        session_id = "s-ledger-wt-junkgit"
        payload = self.post_tool_use_payload(session_id, str(target))
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        self.assertEqual(state.get("ledger", []), [])
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=worktree-unresolved", self._last_outcome_line(log_text))

    def test_ordinary_checkout_of_unwired_repo_outcome_unchanged(self):
        """Requirement 5: a plain (non-worktree) checkout of a repo that
        was never wired must keep today's plain `out-of-scope` outcome,
        never a new worktree-* one -- this is repo_b's OWN main checkout,
        not repo_b_wt."""
        session_id = "s-ledger-wt-ordinary-unwired"
        main_path = str(self.repo_b / "src" / "other.py")
        payload = self.post_tool_use_payload(session_id, main_path)
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        self.assertNotIn(main_path, [e["path"] for e in state.get("ledger", [])])
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=out-of-scope", self._last_outcome_line(log_text))


class TestLedgerNarrowRootWorktreeRemap(HookTestBase):
    """A wired code root narrower than its checkout (<repo>/Sources): a
    worktree edit maps onto the checkout's toplevel, never onto the root, so
    the recorded path is the real file or nothing."""

    def setUp(self):
        super().setUp()
        self.repo = Path(self.td) / "narrow-repo"
        _write(self.repo / "Sources" / "Foo.swift", "// foo\n")
        _write(self.repo / "README.md", "readme\n")
        git_init(self.repo)
        self.wt = self.repo / ".worktrees" / "x"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-narrow", str(self.wt)],
            cwd=self.repo, check=True,
        )
        self.root = (self.repo / "Sources").resolve()

    def _env(self):
        return self.base_env(
            MEMCONTINUUM_CODE_ROOT=str(self.root),
            MEMCONTINUUM_CODE_ROOTS=json.dumps([str(self.root)]),
        )

    def test_worktree_edit_under_narrow_root_is_recorded_at_real_path(self):
        session_id = "s-narrow-code"
        payload = self.post_tool_use_payload(session_id, str(self.wt / "Sources" / "Foo.swift"))
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        paths = [e["path"] for e in self.load_state(session_id)["ledger"]]
        self.assertEqual(paths, [str(self.repo.resolve() / "Sources" / "Foo.swift")])

    def test_worktree_file_outside_narrow_root_is_out_of_scope(self):
        session_id = "s-narrow-readme"
        payload = self.post_tool_use_payload(session_id, str(self.wt / "README.md"))
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        self.assertEqual(state.get("ledger", []), [])
        log_text = (self.home / "hook.log").read_text()
        last = [l for l in log_text.splitlines() if "ledger outcome=" in l][-1]
        self.assertIn("outcome=out-of-scope", last)

    def test_remap_prints_checkout_toplevel_path(self):
        script = (
            f'. {shlex.quote(str(HOOKS_DIR / "mc-path-lib.sh"))}; '
            'mc_remap_worktree_path "$1" "$2"'
        )
        for rel, root, want in (
            ("Sources/Foo.swift", self.root, self.repo.resolve() / "Sources" / "Foo.swift"),
            ("README.md", self.root, self.repo.resolve() / "README.md"),
            ("README.md", self.repo.resolve(), self.repo.resolve() / "README.md"),
        ):
            proc = subprocess.run(
                [MC_BASH, "-c", script, "x", str(self.wt / rel), str(root)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), str(want))

    def test_worktree_physically_beneath_the_narrow_root_is_recorded_at_real_path(self):
        """The ledger's MC_WAS_UNDER_ROOT=1 branch: the worktree sits inside
        the wired root itself (<repo>/Sources/.worktrees/x)."""
        inner = self.root / ".worktrees" / "x"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-inner", str(inner)],
            cwd=self.repo, check=True,
        )
        session_id = "s-narrow-inner"
        payload = self.post_tool_use_payload(session_id, str(inner / "Sources" / "Foo.swift"))
        proc, _ = run_script(LEDGER_HOOK, payload, self._env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        paths = [e["path"] for e in self.load_state(session_id)["ledger"]]
        self.assertEqual(paths, [str(self.repo.resolve() / "Sources" / "Foo.swift")])

    def test_matched_root_whose_toplevel_cannot_be_resolved_is_unresolved_not_unwired(self):
        real_git = shutil.which("git")
        fake_dir = Path(self.td) / "fake-git-toplevel-fails"
        fake_dir.mkdir()
        fake = fake_dir / "git"
        fake.write_text(
            "#!/bin/sh\n"
            f'root={shlex.quote(str(self.root))}\n'
            'dir=""; prev=""\n'
            'for a in "$@"; do\n'
            '  [ "$prev" = "-C" ] && dir="$a"\n'
            '  prev="$a"\n'
            'done\n'
            'case "$*" in *"rev-parse --show-toplevel") [ "$dir" = "$root" ] && exit 1 ;; esac\n'
            f"exec {shlex.quote(real_git)} \"$@\"\n"
        )
        fake.chmod(0o755)
        script = (
            f'. {shlex.quote(str(HOOKS_DIR / "mc-path-lib.sh"))}; '
            'mc_remap_worktree_path "$1" "$2"; echo "rc=$?"'
        )
        env = dict(os.environ, PATH=f"{fake_dir}:{os.environ.get('PATH', '')}")
        proc = subprocess.run(
            [MC_BASH, "-c", script, "x", str(self.wt / "Sources" / "Foo.swift"), str(self.root)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(proc.stdout.strip(), "rc=2", proc.stderr)


class TestMutationSurface(HookTestBase):
    """The PostToolUse group carries no settings-level matcher any more (see
    tests.test_repo_init for the rendering pin) -- ledger-post-edit.sh fires
    for every tool and does its OWN gating in-script: a cheap bash-only
    prefilter exits before the watchdog for the read-only built-ins;
    Edit/Write/MultiEdit/NotebookEdit still ledger the tool's own file_path
    (source: tool); Bash and any tool this hook has no dedicated branch for
    fall through to a shell-diff (git status) tree comparison
    (source: shell-diff), logging outcome=unsupported-mutation-surface for
    the latter two."""

    def bash_payload(self, session_id, command="true"):
        return json.dumps({
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "cwd": str(self.code_root),
            "tool_input": {"command": command},
            "tool_response": {"stdout": "", "stderr": "", "interrupted": False},
        })

    def read_only_payload(self, session_id, tool_name="Read", file_path=None):
        d = {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "cwd": str(self.code_root),
            "tool_input": ({"file_path": file_path} if file_path else {"pattern": "x"}),
        }
        return json.dumps(d)

    def notebook_edit_payload(self, session_id, notebook_path, file_path=None):
        ti = {"notebook_path": notebook_path, "new_source": "# x", "cell_type": "code"}
        if file_path:
            ti["file_path"] = file_path
        return json.dumps({
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "NotebookEdit",
            "cwd": str(self.code_root),
            "tool_input": ti,
        })

    def unknown_tool_payload(self, session_id, tool_name="SomeMcpTool"):
        return json.dumps({
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "cwd": str(self.code_root),
            "tool_input": {"whatever": "x"},
        })

    def no_tool_name_payload(self, session_id):
        return json.dumps({
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "cwd": str(self.code_root),
            "tool_input": {},
        })

    # -- 2: cheap prefilter exits before the watchdog for read-only tools ---

    def test_read_only_tool_exits_fast_with_no_log_line(self):
        session_id = "s-mutation-readonly"
        payload = self.read_only_payload(
            session_id, "Read", file_path=str(self.code_root / "src" / "mapped.py")
        )
        proc, elapsed = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertLess(elapsed, 0.05, elapsed)
        log_path = self.home / "hook.log"
        self.assertFalse(
            log_path.exists() and log_path.read_text().strip(),
            "hook.log must stay untouched for a read-only tool",
        )

    def test_grep_tool_also_exits_fast_with_no_log_line(self):
        session_id = "s-mutation-grep"
        payload = self.read_only_payload(session_id, "Grep")
        proc, elapsed = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertLess(elapsed, 0.05, elapsed)
        log_path = self.home / "hook.log"
        self.assertFalse(log_path.exists() and log_path.read_text().strip())

    # -- 4: NotebookEdit ledgered via notebook_path when file_path is absent -

    def test_notebook_edit_uses_notebook_path_when_file_path_absent(self):
        session_id = "s-mutation-notebook"
        nb = self.code_root / "nb.ipynb"
        _write(nb, "{}")
        payload = self.notebook_edit_payload(session_id, str(nb))
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertIn(str(nb), paths)
        entry = [e for e in state["ledger"] if e["path"] == str(nb)][0]
        self.assertEqual(entry["source"], "tool")
        self.assertEqual(entry["kind"], "code")

    # -- 5: Bash overwrite -- baseline call, then a shell-diff row ----------

    def test_bash_overwrite_lands_as_shell_diff_row(self):
        session_id = "s-mutation-bash"
        target = self.code_root / "src" / "mapped.py"

        proc1, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        state1 = self.load_state(session_id)
        self.assertEqual(state1.get("ledger", []), [])
        self.assertIn(str(self.code_root.resolve()), state1.get("shell_baseline", {}))
        log1 = (self.home / "hook.log").read_text()
        self.assertIn("outcome=shell-diff appended=0", log1)

        _write(target, "# mapped, changed from the shell\n")
        proc2, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        state2 = self.load_state(session_id)
        rows = [e for e in state2["ledger"] if e.get("source") == "shell-diff"]
        self.assertEqual(len(rows), 1, state2["ledger"])
        # Fix round 3: the shell-diff baseline is keyed (and rows are
        # filed) by the PHYSICAL root -- str(target) itself may still be
        # spelled through a symlinked ancestor (macOS's TMPDIR under
        # /var/folders/..., a symlink to /private/var/folders/...), so
        # the row's path is compared in its resolved form.
        self.assertEqual(rows[0]["path"], str(target.resolve()))
        self.assertEqual(rows[0]["kind"], "code")
        self.assertEqual(rows[0]["root"], str(self.code_root.resolve()))
        log2 = (self.home / "hook.log").read_text()
        self.assertIn("ledger outcome=appended kind=code source=shell-diff file=", log2)
        self.assertIn("outcome=shell-diff appended=1", log2)

        # A second identical payload appends nothing more.
        proc3, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        state3 = self.load_state(session_id)
        rows3 = [e for e in state3["ledger"] if e.get("source") == "shell-diff"]
        self.assertEqual(len(rows3), 1, state3["ledger"])

    # -- fix wave 1, G3 (Grok MINOR 5 / whole-branch-review LOW-2 /
    # task-8-review LOW-3): a nested git repo created AFTER the baseline
    # must never ledger a deletion-shaped row (a directory path with an
    # empty content_sha256).

    def test_nested_git_repo_created_after_baseline_produces_no_deletion_shaped_row(self):
        session_id = "s-mutation-nested-repo"

        proc1, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        state1 = self.load_state(session_id)
        self.assertEqual(state1.get("ledger", []), [])

        nested = self.code_root / "vendored-repo"
        _write(nested / "f.py", "# vendored\n")
        git_init(nested)  # a genuine nested git repo -- git status on the
                           # OUTER root now collapses it to one "?? vendored-repo/" entry

        proc2, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        state2 = self.load_state(session_id)
        rows = [e for e in state2.get("ledger", []) if e.get("source") == "shell-diff"]
        self.assertEqual(rows, [], f"a directory entry must never be ledgered: {rows}")
        self.assertFalse(
            any(e.get("content_sha256") == "" for e in state2.get("ledger", [])),
            "no ledger row may carry an empty content_sha256 for what is actually a live directory",
        )
        log2 = (self.home / "hook.log").read_text()
        self.assertNotIn("ledger outcome=appended kind=code source=shell-diff", log2)

    # -- codex re-gate MINOR 2: the G3 directory guard above must not also
    # discard a genuine TRACKED-FILE DELETION -- when a directory now
    # occupies the exact path a tracked file was deleted from, porcelain
    # still reports the deletion (` D`), and that row must still be
    # ledgered (content_sha256=""), alongside the new child file underneath.

    def test_tracked_file_replaced_by_directory_still_ledgers_the_deletion(self):
        session_id = "s-mutation-file-to-dir"

        proc1, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        state1 = self.load_state(session_id)
        self.assertEqual(state1.get("ledger", []), [])

        target = self.code_root / "src" / "mapped.py"
        target.unlink()
        child = target / "child.py"
        _write(child, "# child\n")  # target is now a directory

        proc2, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        state2 = self.load_state(session_id)
        rows = {e["path"]: e for e in state2.get("ledger", []) if e.get("source") == "shell-diff"}

        # Fix round 3: shell-diff rows are filed under the PHYSICAL root
        # (self.code_root.resolve()) -- see test_bash_overwrite_lands_as_shell_diff_row.
        target_r = str(target.resolve())
        child_r = str(child.resolve())
        self.assertIn(
            target_r, rows,
            f"the tracked-file deletion must still be ledgered even though a directory now "
            f"occupies its path: {rows}",
        )
        self.assertEqual(rows[target_r]["content_sha256"], "")

        self.assertIn(child_r, rows, f"the new child file must also be ledgered: {rows}")
        self.assertNotEqual(rows[child_r]["content_sha256"], "")

        log2 = (self.home / "hook.log").read_text()
        self.assertIn("ledger outcome=appended kind=code source=shell-diff file=" + target_r, log2)

    # -- codex re-gate MINOR 2, mirror-image case caught at review: the
    # BASELINE loop has its own copy of the G3 directory guard, and it
    # must record a pre-baseline tracked-file-to-directory deletion as ""
    # (matching an ordinary pre-baseline deletion, which already lands as
    # "" via sha_of() on a missing path) -- never simply drop the path
    # from the baseline entirely, or the very same deletion the per-call
    # fix above now preserves gets falsely attributed as NEW dirt on the
    # very first post-baseline call, violating the same invariant
    # test_dirt_before_baseline_is_not_attributed protects for every
    # other pre-baseline change shape.

    def test_file_to_directory_before_baseline_is_not_attributed(self):
        session_id = "s-mutation-predirt-dir"
        target = self.code_root / "src" / "mapped.py"
        target.unlink()
        _write(target / "child.py", "# child\n")  # replaced BEFORE the baseline call

        proc1, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        state1 = self.load_state(session_id)
        self.assertEqual(state1.get("ledger", []), [])

        proc2, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        state2 = self.load_state(session_id)
        rows = [e for e in state2.get("ledger", []) if e.get("source") == "shell-diff"]
        self.assertEqual(
            rows, [],
            f"a file-to-directory replacement that happened BEFORE the baseline call must "
            f"never be attributed as new dirt on the next call: {rows}",
        )

    # -- writable-surface regression guard: the shell-diff branch's own
    # `git status` calls must be read-only -- INTERNALS.md's writable-
    # surface claim depends on this staying true, since this hook now runs
    # `git status` in every configured code root and the store root on
    # every Bash (or unrecognized-tool) invocation.

    def test_shell_diff_git_status_calls_leave_the_tree_exactly_as_found(self):
        session_id = "s-mutation-readonly-git"
        run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())  # baseline
        before = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.code_root,
            capture_output=True, text=True, check=True,
        ).stdout
        proc, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.code_root,
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertEqual(
            before, after,
            "the hook's own git status calls must never change what a "
            "subsequent git status in that root reports",
        )

    # -- 6: create, rename, delete --------------------------------------------

    def test_bash_create_rename_delete(self):
        session_id = "s-mutation-crud"
        run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())  # baseline

        new_file = self.code_root / "src" / "new_from_shell.py"
        _write(new_file, "# new\n")
        subprocess.run(["git", "add", "-A"], cwd=self.code_root, check=True)
        subprocess.run(
            ["git", "mv", "src/unmapped.py", "src/renamed.py"], cwd=self.code_root, check=True
        )
        deleted = self.code_root / "src" / "mapped.py"
        deleted.unlink()

        proc, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        rows = {e["path"]: e for e in state["ledger"] if e.get("source") == "shell-diff"}
        # Fix round 3: shell-diff rows are filed under the PHYSICAL root.
        deleted_r = str(deleted.resolve())
        self.assertIn(str(new_file.resolve()), rows, rows)
        self.assertIn(str((self.code_root / "src" / "renamed.py").resolve()), rows, rows)
        self.assertIn(str((self.code_root / "src" / "unmapped.py").resolve()), rows, rows)
        self.assertIn(deleted_r, rows, rows)
        self.assertEqual(rows[deleted_r]["content_sha256"], "")

    # -- 7: pre-existing dirt is not attributed -------------------------------

    def test_dirt_before_baseline_is_not_attributed(self):
        session_id = "s-mutation-predirt"
        target = self.code_root / "src" / "mapped.py"
        _write(target, "# dirty before the baseline call\n")
        run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        state = self.load_state(session_id)
        self.assertEqual(state.get("ledger", []), [])

        _write(target, "# dirty AFTER the baseline call too\n")
        proc, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state2 = self.load_state(session_id)
        # Fix round 3: shell-diff rows are filed under the PHYSICAL root.
        rows = [e for e in state2["ledger"] if e["path"] == str(target.resolve())]
        self.assertEqual(len(rows), 1, state2["ledger"])

    # -- 8: a shell edit under the store root is a kind: store row ------------

    def test_bash_edit_under_store_root_is_kind_store(self):
        session_id = "s-mutation-store"
        run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())  # baseline
        target = self.store_root / "topics" / "testing" / "mapped-topic.md"
        _write(target, TOPIC_MD + "\nedited from the shell\n")
        proc, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        # Fix round 3: shell-diff rows are filed under the PHYSICAL root
        # (the store root is realpath'd too, not just code roots).
        rows = [e for e in state["ledger"] if e["path"] == str(target.resolve())]
        self.assertEqual(len(rows), 1, state["ledger"])
        self.assertEqual(rows[0]["kind"], "store")
        self.assertEqual(rows[0]["source"], "shell-diff")

    # -- a code root that is a git WORKTREE (".git" is a file, not a
    # directory) must still be diffed, not silently counted non-git -------

    def test_bash_edit_under_a_worktree_code_root_is_still_diffed(self):
        session_id = "s-mutation-worktree"
        worktree = Path(self.td) / "code-worktree"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "wt-branch", str(worktree)],
            cwd=self.code_root, check=True,
        )
        env = self.base_env(
            MEMCONTINUUM_CODE_ROOT=str(worktree.resolve()),
            MEMCONTINUUM_CODE_ROOTS=json.dumps([str(worktree.resolve())]),
        )
        proc1, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), env)  # baseline
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        log1 = (self.home / "hook.log").read_text()
        self.assertIn("outcome=shell-diff appended=0 roots=2 timeouts=0 non-git=0", log1)

        target = worktree / "src" / "mapped.py"
        _write(target, "# mapped, changed inside the worktree\n")
        proc2, _ = run_script(LEDGER_HOOK, self.bash_payload(session_id), env)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        state2 = self.load_state(session_id)
        rows = [e for e in state2["ledger"] if e.get("source") == "shell-diff"]
        self.assertEqual(len(rows), 1, state2["ledger"])
        # Fix round 3: same physical-path comparison as the overwrite test
        # above -- `target` here is built from the unresolved `worktree`,
        # while the row's path comes back through the realpath'd root.
        self.assertEqual(rows[0]["path"], str(target.resolve()))

    # -- 9: a slow root times out and is counted; the other root still works -

    def test_slow_root_times_out_other_root_still_diffed(self):
        session_id = "s-mutation-slow"
        code_root_b = Path(self.td) / "code-b"
        code_root_b.mkdir()
        _write(code_root_b / "b.py", "# b\n")
        git_init(code_root_b)

        real_git = shutil.which("git")
        self.assertIsNotNone(real_git, "this test needs a real git on PATH")
        fake_git_dir = Path(self.td) / "fake-git-bin"
        fake_git_dir.mkdir()
        fake_git = fake_git_dir / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            "prev=\"\"\n"
            "slow=0\n"
            "for a in \"$@\"; do\n"
            "  if [ \"$prev\" = \"-C\" ] && [ \"$a\" = \"$SLOW_ROOT\" ]; then slow=1; fi\n"
            "  prev=\"$a\"\n"
            "done\n"
            "if [ \"$slow\" = 1 ]; then exec sleep 2; fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n"
        )
        fake_git.chmod(0o755)

        env = self.base_env(
            MEMCONTINUUM_CODE_ROOTS=json.dumps(
                [str(self.code_root.resolve()), str(code_root_b.resolve())]
            ),
            SLOW_ROOT=str(self.code_root.resolve()),
            REAL_GIT=real_git,
            PATH=f"{fake_git_dir}:{os.environ.get('PATH', '')}",
            MEMCONTINUUM_SHELL_DIFF_ROOT_BUDGET="0.3",
        )
        proc, elapsed = run_script(LEDGER_HOOK, self.bash_payload(session_id), env, timeout=5.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 2.0, elapsed)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("timeouts=1", log_text)
        self.assertNotIn("watchdog-killed", log_text)
        state = self.load_state(session_id)
        self.assertIn(str(code_root_b.resolve()), state.get("shell_baseline", {}))
        self.assertNotIn(str(self.code_root.resolve()), state.get("shell_baseline", {}))

        # The next call retries the timed-out root's baseline (still under
        # the fake/slow git) while the healthy root keeps working.
        _write(code_root_b / "b.py", "# b changed\n")
        run_script(LEDGER_HOOK, self.bash_payload(session_id), env, timeout=5.0)
        state2 = self.load_state(session_id)
        rows = [e for e in state2["ledger"] if e.get("root") == str(code_root_b.resolve())]
        self.assertEqual(len(rows), 1, state2["ledger"])

    # -- 10: an unknown tool is logged AND still diffed -----------------------

    def test_unknown_tool_logs_unsupported_and_still_diffs(self):
        session_id = "s-mutation-unknown"
        run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())  # baseline
        target = self.code_root / "src" / "mapped.py"
        _write(target, "# changed by an mcp tool\n")
        payload = self.unknown_tool_payload(session_id, "SomeMcpTool")
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=unsupported-mutation-surface tool=SomeMcpTool", log_text)
        state = self.load_state(session_id)
        # Fix round 3: shell-diff rows are filed under the PHYSICAL root.
        rows = [e for e in state["ledger"] if e["path"] == str(target.resolve())]
        self.assertEqual(len(rows), 1, state["ledger"])
        self.assertEqual(rows[0]["source"], "shell-diff")

    # -- 11: a missing tool_name is logged as tool=unknown --------------------

    def test_missing_tool_name_logs_unknown_and_still_diffs(self):
        session_id = "s-mutation-missing-tool"
        run_script(LEDGER_HOOK, self.bash_payload(session_id), self.base_env())  # baseline
        target = self.code_root / "src" / "mapped.py"
        _write(target, "# changed with no tool_name at all\n")
        payload = self.no_tool_name_payload(session_id)
        proc, _ = run_script(LEDGER_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=unsupported-mutation-surface tool=unknown", log_text)
        state = self.load_state(session_id)
        # Fix round 3: shell-diff rows are filed under the PHYSICAL root.
        rows = [e for e in state["ledger"] if e["path"] == str(target.resolve())]
        self.assertEqual(len(rows), 1, state["ledger"])

    # -- 12: the hook script never reads the command text or the tool's
    #        response (guard against re-reading the arbitrary shell command
    #        or its output -- ruling B) -----------------------------------

    def test_script_never_reads_command_or_tool_response(self):
        text = LEDGER_HOOK.read_text()
        self.assertNotIn("tool_input.command", text)
        self.assertNotIn("tool_response", text)


# ---------------------------------------------------------------------------
# 3. precompact-persist.sh
# ---------------------------------------------------------------------------


class TestPrecompactPersist(HookTestBase):
    def test_bash_syntax_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(PRECOMPACT_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    # F1 (ruling 68): same three coverage_status outcomes as
    # TestUserPromptRemind above, using this class's own pre_compact_payload/
    # PRECOMPACT_HOOK -- a real code-kind ledger entry is required for the
    # same reason (CODE_PATHS must be non-empty for `unmapped` to run at
    # all).

    def test_uninitialized_logs_index_uninitialized(self):
        db = self.home / f"{self.project}.sqlite"
        db.unlink()
        session_id = "s-precompact-uninit"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-uninitialized", (self.home / "hook.log").read_text())

    def test_upgrade_required_logs_index_upgrade_required(self):
        db = self.home / f"{self.project}.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', '1')")
        conn.commit(); conn.close()
        session_id = "s-precompact-upgrade"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-upgrade-required", (self.home / "hook.log").read_text())

    def test_index_error_logs_index_error(self):
        db = self.home / f"{self.project}.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("ALTER TABLE records RENAME COLUMN path TO path_broken")
        conn.commit(); conn.close()
        session_id = "s-precompact-index-error"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-error", (self.home / "hook.log").read_text())

    def test_degraded_logs_index_degraded(self):
        """LOW-1 (task-5-review.md): symmetric with
        TestUserPromptRemind.test_degraded_logs_index_degraded -- same
        corrupt-blob-db technique, this hook's own `DEGRADED_REASON=...`
        block (hooks/precompact-persist.sh) logs the same distinct
        outcome, additive to (never instead of) the existing `computed`
        line. Behaviorally already correct (confirmed independently by
        the Task 5 review's own probe against the real hook subprocess);
        this closes the coverage gap the review noted -- no hook script
        change needed."""
        db = self.home / f"{self.project}.sqlite"
        db.write_bytes(b"not a sqlite file at all")
        session_id = "s-precompact-degraded"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-degraded reason=internal-error", (self.home / "hook.log").read_text())

    def test_exits_zero_with_absolutely_empty_stdout(self):
        session_id = "s-precompact-empty"
        self.seed_ledger(
            session_id,
            [(str(self.code_root / "src" / "unmapped.py"), "code")],
        )
        payload = self.pre_compact_payload(session_id)
        proc, _ = run_script(PRECOMPACT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "PreCompact stdout becomes summarizer instructions -- must be empty")
        self.assertNotIn("hookSpecificOutput", proc.stdout)
        self.assertNotIn("decision", proc.stdout)

    def test_log_line_carries_project(self):
        """Liveness metric fix: every precompact-persist.sh outcome line
        must carry project=<MC_PROJECT> (via mc_log in memlib.sh)."""
        session_id = "s-precompact-project"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        payload = self.pre_compact_payload(session_id)
        run_script(PRECOMPACT_HOOK, payload, self.base_env())
        log_text = (self.home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "precompact" in l]
        self.assertTrue(matching)
        for line in matching:
            self.assertIn(f"project={self.project}", line, line)

    def test_computes_pending_evidence(self):
        session_id = "s-precompact-pending"
        self.seed_ledger(
            session_id,
            [
                (str(self.code_root / "src" / "unmapped.py"), "code"),
                (str(self.code_root / "src" / "mapped.py"), "code"),
            ],
        )
        # capture start SHAs the way sessionstart-remind.sh would
        state = self.load_state(session_id)
        state["start_code_sha"] = git_head(self.code_root)
        state["start_store_sha"] = git_head(self.store_root)
        self.state_file(session_id).write_text(json.dumps(state))

        payload = self.pre_compact_payload(session_id)
        proc, _ = run_script(PRECOMPACT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        state = self.load_state(session_id)
        pending = state.get("pending")
        self.assertIsNotNone(pending)
        self.assertIn("src/unmapped.py", pending.get("unmapped", []))
        self.assertNotIn("src/mapped.py", pending.get("unmapped", []))
        self.assertIn("code_head_changed", pending)
        self.assertIn("store_head_changed", pending)
        self.assertFalse(pending["code_head_changed"])
        self.assertFalse(pending["store_head_changed"])

    def test_no_state_no_crash(self):
        payload = self.pre_compact_payload("s-precompact-nostate")
        proc, _ = run_script(PRECOMPACT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_store_byte_identical(self):
        session_id = "s-precompact-bytesafe"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, _ = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.base_env())
        self.assertTrue(git_is_clean(self.store_root))

    def test_fail_open_corrupted_state_file(self):
        session_id = "s-precompact-corrupt"
        sf = self.state_file(session_id)
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text("{ this is not json ]]]")
        proc, _ = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_fail_open_missing_python(self):
        session_id = "s-precompact-nopython"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        env = self.base_env(MEMCONTINUUM_PYTHON="/no/such/python")
        proc, _ = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_fail_open_on_timeout(self):
        """macOS port mechanics note: MEMCONTINUUM_PYTHON also resolves the
        outer watchdog launcher's own interpreter (see hooks/*.sh's guard
        block), so a wrapper that sleeps unconditionally on every
        invocation would delay the launcher's own startup, not just the
        work call it means to simulate. The wrapper only sleeps for calls
        that are NOT the launcher itself (detected by the MC_WATCHDOG_LAUNCHER
        marker every guard's -c source carries) -- this keeps the test's
        original intent (a slow python for real work) while adjusting for
        the new mechanism, per the port's "adjust mechanics, keep bounds"
        instruction."""
        session_id = "s-precompact-timeout"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        slow_py = Path(self.td) / "slow-python"
        slow_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *MC_WATCHDOG_LAUNCHER*) exec \"" + VENV_PYTHON + "\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            "sleep 6\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        slow_py.chmod(0o755)
        env = self.base_env(MEMCONTINUUM_PYTHON=str(slow_py))
        start = time.monotonic()
        proc, elapsed = run_script(
            PRECOMPACT_HOOK, self.pre_compact_payload(session_id), env, timeout=10.0
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertLess(elapsed, 5.0, "internal timeout (2s) should have fired well before 5s")

    def test_completes_under_poisoned_pythonpath(self):
        session_id = "s-precompact-poison"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(
            PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.poisoned_base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_pending_snapshots_lookback_counters(self):
        """precompact-persist.sh must persist the counters sessionstart-
        remind.sh(compact) needs for the look-back twin -- since those
        values must be frozen at PreCompact time, not re-read live later."""
        session_id = "s-precompact-lookback-snapshot"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "mapped.py"), "code")])
        self.patch_state(session_id, user_turn_count=7, last_growth_turn=3)
        proc, _ = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pending = self.load_state(session_id).get("pending") or {}
        self.assertEqual(pending.get("user_turn_count"), 7)
        self.assertEqual(pending.get("last_growth_turn"), 3)

    def test_fail_open_when_watchdog_lib_missing(self):
        """R1 regression, round 4 gate: see TestLedgerPostEdit's twin."""
        session_id = "s-precompact-watchdog-lib-missing"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        env = self.base_env(MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh")
        proc, _ = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertEqual(proc.stdout, "")


# ---------------------------------------------------------------------------
# 4. sessionstart-remind.sh
# ---------------------------------------------------------------------------


class TestSessionStartRemind(HookTestBase):
    def test_bash_syntax_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(SESSIONSTART_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_startup_inits_state_with_start_shas_silently(self):
        session_id = "s-start-startup"
        payload = self.session_start_payload(session_id, source="startup")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        self.assertEqual(state.get("start_code_sha"), git_head(self.code_root))
        self.assertEqual(state.get("start_store_sha"), git_head(self.store_root))

    def test_relint_marker_surfaces_one_line_at_session_start(self):
        """0.4.0: a sticky <project>.relint-failed marker (written by the
        store post-commit re-lint) becomes one fact line in the startup
        context; without the marker the startup stays silent."""
        env = self.base_env()
        project = env["MEMCONTINUUM_PROJECT"]
        marker = self.home / f"{project}.relint-failed"
        marker.write_text(
            "root=/stores/the-store\n"
            "sha=0123456789abcdef0123456789abcdef01234567\n"
            "date=2026-10-19T10:00:00\n"
            "failed=schema\n"
            "summary=memlint: 1 error(s), 0 warning(s)\n"
        )
        payload = self.session_start_payload("s-start-relint", source="startup")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"MemContinuum: store {project} (/stores/the-store) commit 01234567 skipped the commit checks "
                      "and would have been refused (memlint: 1 error(s), 0 warning(s)) "
                      "-- fix with a new commit; see ", ctx)
        self.assertIn(f"{project}.relint-failed", ctx)
        self.assertIn("relint=shown", (self.home / "hook.log").read_text())

        marker.unlink()
        proc, _ = run_script(SESSIONSTART_HOOK, self.session_start_payload("s-start-relint2", source="startup"), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_clear_inits_state_with_start_shas_silently(self):
        """INC-0108: a session that begins with /clear (source=clear) must
        take the same init path as startup -- a cleared session is a fresh
        session. Mirrors test_startup_inits_state_with_start_shas_silently
        exactly, source swapped."""
        session_id = "s-start-clear"
        payload = self.session_start_payload(session_id, source="clear")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        self.assertEqual(state.get("start_code_sha"), git_head(self.code_root))
        self.assertEqual(state.get("start_store_sha"), git_head(self.store_root))
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=init", log_text)
        self.assertIn("source=clear", log_text)

    def test_clear_discards_state_left_over_from_before_the_clear(self):
        """INC-0108 design decision: /clear commonly fires on the SAME
        session_id an earlier startup already created state for (a /clear
        mid-process). Unlike resume (which must preserve the original
        startup's values via setdefault), clear must DISCARD that leftover
        state and start empty -- a stale turn-count/pending left over from
        before the clear would misfire the coverage/look-back nudges
        against turns the cleared context no longer has, and a stale
        sessionend `ended_at` stamp has no business surviving into a session
        that is still running. Ledger carryover is NOT exercised here (kept
        empty throughout) -- see test_clear_carries_over_ledger_so_
        pre_clear_coverage_still_fires for that, the one field that must
        survive the discard."""
        session_id = "s-start-clear-reset"
        stale_code_sha = "0" * 40
        self.seed_ledger(session_id, [])
        self.patch_state(
            session_id,
            start_code_sha=stale_code_sha,
            start_store_sha=stale_code_sha,
            user_turn_count=17,
            last_inject_turn=12,
            last_inject_time=123.0,
            last_inject_ts=123.0,
            last_injected_pairs=[["a", "b"]],
            last_growth_turn=9,
            last_growth_ts=100.0,
            lookback_count=3,
            pending={"unmapped": ["src/unmapped.py"], "coverage_status": "ok"},
            ended_at=999.0,
        )

        payload = self.session_start_payload(session_id, source="clear")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

        state = self.load_state(session_id)
        self.assertEqual(state.get("start_code_sha"), git_head(self.code_root))
        self.assertNotEqual(state.get("start_code_sha"), stale_code_sha)
        self.assertEqual(state.get("start_store_sha"), git_head(self.store_root))
        self.assertEqual(state.get("ledger"), [])
        self.assertEqual(state.get("user_turn_count"), 0)
        self.assertEqual(state.get("last_inject_turn"), -999)
        self.assertEqual(state.get("last_inject_time"), 0)
        self.assertEqual(state.get("last_inject_ts"), 0)
        self.assertEqual(state.get("last_injected_pairs"), [])
        self.assertEqual(state.get("last_growth_turn"), 0)
        self.assertEqual(state.get("lookback_count"), 0)
        self.assertNotIn("pending", state)
        self.assertNotIn("ended_at", state)

    def test_clear_carries_over_ledger_so_pre_clear_coverage_still_fires(self):
        """Review round 1, HIGH finding: userprompt-remind.sh's coverage
        check (`memidx.py unmapped`) classifies candidate paths ONLY from
        state["ledger"] -- it never walks the code tree. If clear wiped the
        ledger along with everything else, a file edited BEFORE the clear
        that is still genuinely unmapped would drop out of coverage
        candidacy until touched again post-clear -- silent evidence loss.
        This drives the real regression scenario end to end: a ledger entry
        seeded before the clear, no new edit after it, and the very next
        userprompt after the clear must still surface that file."""
        session_id = "s-start-clear-ledger-carryover"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        self.patch_state(
            session_id,
            user_turn_count=17,
            last_inject_turn=12,
            last_inject_time=123.0,
            last_inject_ts=123.0,
            last_injected_pairs=[["src/unmapped.py", "somehash"]],
            last_growth_turn=9,
            last_growth_ts=100.0,
            lookback_count=3,
        )

        payload = self.session_start_payload(session_id, source="clear")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        state = self.load_state(session_id)
        ledger_paths = [e.get("path") for e in state.get("ledger") or []]
        self.assertIn(str(self.code_root / "src" / "unmapped.py"), ledger_paths)
        self.assertEqual(state.get("user_turn_count"), 0)
        self.assertEqual(state.get("last_inject_turn"), -999)
        self.assertEqual(state.get("last_injected_pairs"), [])
        self.assertEqual(state.get("last_growth_turn"), 0)
        self.assertEqual(state.get("lookback_count"), 0)

        # No re-seeding, no new edit -- the very next real userprompt call
        # must still surface the pre-clear file.
        up_proc, _ = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env()
        )
        self.assertEqual(up_proc.returncode, 0, up_proc.stderr)
        self.assertIn("Coverage signal", up_proc.stdout)
        self.assertIn("src/unmapped.py", up_proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=injected", log_text)

    def test_clear_carries_over_shell_baseline_too(self):
        """Design R6 (audit MC-P1-04, TOP-0123 L6): `shell_baseline` is the
        shell-diff branch's own per-root baseline map -- INC-0108's clear
        discard must keep it alongside `ledger`, or a mid-session /clear
        would silently re-baseline every root (losing the distinction
        between pre- and post-clear shell dirt for the rest of the
        session, exactly the class of evidence loss `ledger` is already
        protected against)."""
        session_id = "s-start-clear-shell-baseline"
        self.patch_state(
            session_id,
            shell_baseline={str(self.code_root.resolve()): {"src/mapped.py": "deadbeef"}},
            user_turn_count=5,
        )
        payload = self.session_start_payload(session_id, source="clear")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        self.assertEqual(
            state.get("shell_baseline"),
            {str(self.code_root.resolve()): {"src/mapped.py": "deadbeef"}},
        )
        self.assertEqual(state.get("user_turn_count"), 0)

    def test_clear_then_userprompt_is_served_not_no_state(self):
        """INC-0108 end-to-end pin: a real SessionStart(source=clear) followed
        by a real UserPromptSubmit for the same session must be served
        (coverage evidence injected), not fall into userprompt-remind.sh's
        no-state branch the way the incident observed for 2.5 hours."""
        session_id = "s-start-clear-then-prompt"
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, source="clear"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self.state_file(session_id).exists())

        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        up_proc, _ = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env()
        )
        self.assertEqual(up_proc.returncode, 0, up_proc.stderr)
        self.assertIn("Coverage signal", up_proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=injected", log_text)
        self.assertNotIn("outcome=no-state", log_text)

    def test_log_line_carries_project(self):
        """Liveness metric fix: every sessionstart-remind.sh outcome line
        must carry project=<MC_PROJECT> (via mc_log in memlib.sh)."""
        session_id = "s-start-project"
        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "sessionstart" in l]
        self.assertTrue(matching)
        for line in matching:
            self.assertIn(f"project={self.project}", line, line)

    def test_log_line_carries_the_session_cwd(self):
        """INC-0130: the session's working directory is on every sessionstart
        outcome line; a payload without one prints cwd=-."""
        run_script(SESSIONSTART_HOOK, self.session_start_payload("s-start-cwd", "startup"), self.base_env())
        no_cwd = json.dumps({"session_id": "s-start-nocwd", "hook_event_name": "SessionStart",
                             "source": "startup"})
        run_script(SESSIONSTART_HOOK, no_cwd, self.base_env())
        lines = [l for l in (self.home / "hook.log").read_text().splitlines() if "sessionstart outcome=" in l]
        with_cwd = [l for l in lines if "session=s-start-cwd" in l]
        without = [l for l in lines if "session=s-start-nocwd" in l]
        self.assertTrue(with_cwd and without, lines)
        for line in with_cwd:
            self.assertIn(f" {memidx._log_path_token(self.code_root).join(['cwd=', ' '])}", line)
        for line in without:
            self.assertIn(" cwd=- ", line)

    def test_hostile_cwd_logs_as_one_token_in_both_languages(self):
        """INC-0130 gate round 1 (MAJOR): spaces and `=` in a cwd must not
        forge fields. The sessionstart line (bash encoder) carries ONE
        cwd= token that decodes back to the path, and project= stays last."""
        hostile = "/x project=other session=fake"
        payload = json.dumps({"session_id": "s-start-host", "hook_event_name": "SessionStart",
                              "source": "startup", "cwd": hostile})
        run_script(SESSIONSTART_HOOK, payload, self.base_env())
        lines = [l for l in (self.home / "hook.log").read_text().splitlines() if "session=s-start-host" in l]
        self.assertTrue(lines)
        for line in lines:
            toks = line.split(" ")
            cwd_toks = [t for t in toks if t.startswith("cwd=")]
            self.assertEqual(len(cwd_toks), 1, line)
            self.assertEqual(urllib.parse.unquote(cwd_toks[0][4:]), hostile)
            self.assertEqual([t for t in toks if t.startswith("project=")], [f"project={self.project}"], line)
            self.assertEqual([t for t in toks if t.startswith("session=")], ["session=s-start-host"], line)

    def test_resume_does_not_reset_existing_start_shas(self):
        session_id = "s-start-resume"
        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env())
        original = self.load_state(session_id)["start_code_sha"]
        # advance code HEAD
        _write(self.code_root / "src" / "new.py", "# new\n")
        subprocess.run(["git", "add", "-A"], cwd=self.code_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "advance"], cwd=self.code_root, check=True)

        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "resume"), self.base_env())
        after = self.load_state(session_id)["start_code_sha"]
        self.assertEqual(original, after)

    def test_prunes_state_older_than_24h(self):
        stale_session = "s-start-stale"
        fresh_session = "s-start-fresh"
        stale_file = self.state_file(stale_session)
        stale_file.parent.mkdir(parents=True, exist_ok=True)
        stale_file.write_text(json.dumps({"session_id": stale_session}))
        old = time.time() - (25 * 3600)
        os.utime(stale_file, (old, old))

        run_script(
            SESSIONSTART_HOOK, self.session_start_payload(fresh_session, "startup"), self.base_env()
        )
        self.assertFalse(stale_file.exists())
        self.assertTrue(self.state_file(fresh_session).exists())

    def test_compact_injects_queued_pending_evidence(self):
        session_id = "s-start-compact"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        state = self.load_state(session_id)
        state["pending"] = {
            "unmapped": ["src/unmapped.py"],
            "mapped_topic_count": 0,
            "mapped_concept_only_count": 0,
            "coverage_status": "ok",
            "code_head_changed": True,
            "store_head_changed": False,
            "computed_at": time.time(),
        }
        self.state_file(session_id).write_text(json.dumps(state))

        payload = self.session_start_payload(session_id, source="compact")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Coverage signal", ctx)
        self.assertIn("src/unmapped.py", ctx)
        self.assertIn("code HEAD changed: yes", ctx)
        self.assertIn("store HEAD changed: no", ctx)
        self.assertIn(
            "Any ruling, incident, or rejected alternative from this session that "
            "the MemContinuum store should hold? Store: " + str(self.store_root) + " — "
            "a ruling is a new link in topics/<area>/<topic>.md, an incident is a "
            "file in incidents/ (see docs/SCHEMA.md); NOT Claude Code auto-memory. "
            "If none, say so once.",
            ctx,
        )
        self.assertIn(str(self.store_root), ctx)
        self.assertIn("NOT Claude Code auto-memory", ctx)
        self.assertNotIn("that memory/ should hold", ctx)
        self.assertNotIn("unrecorded", ctx.lower())
        self.assertEqual(scan_forbidden_lines(ctx), [])

    def test_compact_injects_once_then_silent(self):
        session_id = "s-start-compact-once"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        state = self.load_state(session_id)
        state["pending"] = {
            "unmapped": ["src/unmapped.py"],
            "coverage_status": "ok",
            "code_head_changed": False,
            "store_head_changed": False,
            "computed_at": time.time(),
        }
        self.state_file(session_id).write_text(json.dumps(state))
        payload = self.session_start_payload(session_id, source="compact")

        proc1, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertTrue(proc1.stdout.strip())

        proc2, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc2.stdout.strip(), "", "second compact fire must be silent (consumed)")

    def test_compact_silent_on_empty_pending(self):
        session_id = "s-start-compact-empty"
        self.seed_ledger(session_id, [])
        payload = self.session_start_payload(session_id, source="compact")
        proc, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_never_claims_a_false_gap_when_pending_unknown(self):
        session_id = "s-start-compact-unknown"
        state = {
            "session_id": session_id,
            "pending": {
                "unmapped": [],
                "coverage_status": "unknown",
                "code_head_changed": True,
                "store_head_changed": False,
                "computed_at": time.time(),
            },
        }
        self.state_file(session_id).parent.mkdir(parents=True, exist_ok=True)
        self.state_file(session_id).write_text(json.dumps(state))
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("0 edited file", ctx)
        self.assertIn("Coverage signal", ctx)

    def test_other_sources_are_silent(self):
        """`clear` moved to the startup|resume|clear arm (INC-0108) and is
        covered by its own tests above; only genuinely-unhandled sources
        stay here."""
        for source in ("fork", "banana"):
            with self.subTest(source=source):
                proc, _ = run_script(
                    SESSIONSTART_HOOK,
                    self.session_start_payload(f"s-start-{source}", source),
                    self.base_env(),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "")

    def test_unknown_source_logs_source_not_handled(self):
        session_id = "s-start-unknown-fork"
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, source="fork"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=source-not-handled", log_text)
        self.assertIn("source=fork", log_text)
        self.assertFalse(self.state_file(session_id).exists())

    def test_store_byte_identical(self):
        session_id = "s-start-bytesafe"
        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env())
        self.assertTrue(git_is_clean(self.store_root))

    def test_fail_open_malformed_payload(self):
        proc, _ = run_script(SESSIONSTART_HOOK, "not json at all {{{", self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_completes_under_poisoned_pythonpath(self):
        session_id = "s-start-poison"
        proc, elapsed = run_script(
            SESSIONSTART_HOOK,
            self.session_start_payload(session_id, "startup"),
            self.poisoned_base_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.0)

    def test_fail_open_when_watchdog_lib_missing(self):
        """R1 regression, round 4 gate: see TestLedgerPostEdit's twin."""
        session_id = "s-start-watchdog-lib-missing"
        env = self.base_env(MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh")
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        self.assertEqual(state.get("start_code_sha"), git_head(self.code_root))


class TestSessionStartStandingDigest(HookTestBase):
    """TOP-0132 L1/L2: sessionstart-remind.sh's own delivery of the
    standing-decisions digest -- every source, dedupe, always-inject on
    clear/compact, skip on a refused index, and the no-write property.
    `HookTestBase`'s own fixture topics carry no `standing:` (see
    TestSessionStartRemind's silence assertions, which depend on that),
    so every test here adds its own eligible standing topic first."""

    STANDING_TOPIC = """---
type: topic
id: TOP-9500
title: A standing decision
area: testing
current: L1
standing: [L1]
links:
  - link: L1
    date: 2026-01-01
    status: active
    kind: adopted
    ruling:
      text: "this ruling stands for the whole project"
      authority: owner-verbatim
      source: "test"
    recorded_by: agent
    recorded_at: 2026-01-01
---

Body.
"""

    def _add_standing_topic(self):
        _write(self.store_root / "topics" / "testing" / "standing-topic.md", self.STANDING_TOPIC)
        subprocess.run(["git", "add", "-A"], cwd=self.store_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add standing topic"], cwd=self.store_root, check=True)
        db = self.home / f"{self.project}.sqlite"
        with contextlib.redirect_stdout(io.StringIO()):
            reindex(self.store_root, db, project=self.project)

    def _standing_line(self, log_text):
        matching = [l for l in log_text.splitlines() if "sessionstart outcome=" in l]
        self.assertTrue(matching, log_text)
        return matching[-1]

    def test_startup_injects_when_an_eligible_standing_link_exists(self):
        self._add_standing_topic()
        session_id = "s-standing-startup"
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("this ruling stands for the whole project", ctx)
        self.assertIn("TOP-9500 L1", ctx)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("standing=injected links=1", self._standing_line(log_text))

    def test_resume_dedupes_after_startup_already_injected(self):
        self._add_standing_topic()
        session_id = "s-standing-dedup"
        proc1, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )
        self.assertTrue(proc1.stdout.strip())

        proc2, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "resume"), self.base_env()
        )
        self.assertEqual(
            proc2.stdout.strip(), "", "resume with an unchanged digest must dedupe silently"
        )
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("standing=dedup", self._standing_line(log_text))

    def test_resume_dedup_backfills_missing_standing_ids(self):
        """Fix round 1 (MAJOR, Grok): a session whose state predates this
        key (standing_hash already stamped by an older engine build,
        standing_ids never written) must not leave the prompt-query
        exclusion set silently missing the standing digest's topics
        until clear/compact. Seed state with today's own standing_hash
        but no standing_ids (as if an older build wrote it), then
        resume: still dedup (nothing re-injected, an unchanged digest),
        but standing_ids is now backfilled and correct."""
        self._add_standing_topic()
        session_id = "s-standing-dedup-backfill"
        proc1, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )
        self.assertTrue(proc1.stdout.strip())
        state = self.load_state(session_id)
        self.assertEqual(state.get("standing_ids"), ["TOP-9500"])

        # Simulate a pre-existing state that predates this key: the hash
        # is already stamped (today's own digest), standing_ids never was.
        state.pop("standing_ids", None)
        self.state_file(session_id).write_text(json.dumps(state))

        proc2, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "resume"), self.base_env()
        )
        self.assertEqual(
            proc2.stdout.strip(), "", "an unchanged digest must still dedupe silently"
        )
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("standing=dedup", self._standing_line(log_text))

        state2 = self.load_state(session_id)
        self.assertEqual(
            state2.get("standing_ids"), ["TOP-9500"],
            "standing_ids must be backfilled on the dedup path",
        )

    def test_clear_always_injects_even_though_a_prior_hash_exists(self):
        self._add_standing_topic()
        session_id = "s-standing-clear"
        run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )

        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "clear"), self.base_env()
        )
        self.assertTrue(proc.stdout.strip(), "clear must always re-inject, never dedupe")
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("this ruling stands for the whole project", ctx)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("standing=injected", self._standing_line(log_text))

    def test_skipped_when_the_index_is_stale(self):
        self._add_standing_topic()
        # On-disk drift after the last reindex -- MEMCONTINUUM_ROOT is
        # always set by base_env(), so --root is always passed and
        # "stale" is observable here (unlike a rootless caller).
        (self.store_root / "topics" / "testing" / "new.md").write_text(
            "---\ntype: topic\nid: TOP-9501\ntitle: New\nlinks: []\n---\nBody.\n"
        )
        session_id = "s-standing-stale"
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("standing=skipped-stale", self._standing_line(log_text))

    def test_over_cap_logs_skipped_over_cap_not_skipped_unknown(self):
        """Fix-round MINOR (Grok): the over-cap JSON envelope carries
        `"reason": "over-cap"` and no `"state"` key at all -- the meta-
        parsing step used to fall into the generic `obj.get("state",
        "unknown")` branch and log `standing=skipped-unknown`, losing the
        one fact an operator most needs (the store is over its own cap)."""
        topic = (
            "---\ntype: topic\nid: TOP-9502\ntitle: Over cap\narea: testing\n"
            "current: L1\nstanding: [L1]\nlinks:\n"
            "  - link: L1\n    date: 2026-01-01\n    status: active\n    kind: adopted\n"
            f'    ruling: {{text: "{"x" * (memidx.STANDING_CAP_BYTES + 500)}", '
            "authority: owner-verbatim, source: s}\n"
            "    recorded_by: agent\n    recorded_at: 2026-01-01\n---\n\nBody.\n"
        )
        _write(self.store_root / "topics" / "testing" / "over-cap.md", topic)
        subprocess.run(["git", "add", "-A"], cwd=self.store_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "over cap"], cwd=self.store_root, check=True)
        db = self.home / f"{self.project}.sqlite"
        with contextlib.redirect_stdout(io.StringIO()):
            reindex(self.store_root, db, project=self.project)

        session_id = "s-standing-over-cap"
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (self.home / "hook.log").read_text()
        last = self._standing_line(log_text)
        self.assertIn("standing=skipped-over-cap", last)
        self.assertNotIn("skipped-unknown", last)

    def test_compact_appends_standing_after_coverage_evidence(self):
        self._add_standing_topic()
        session_id = "s-standing-compact-coverage"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        state = self.load_state(session_id)
        state["pending"] = {
            "unmapped": ["src/unmapped.py"],
            "coverage_status": "ok",
            "code_head_changed": True,
            "store_head_changed": False,
            "computed_at": time.time(),
        }
        self.state_file(session_id).write_text(json.dumps(state))

        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Coverage signal", ctx)
        self.assertIn("this ruling stands for the whole project", ctx)
        self.assertLess(
            ctx.index("Coverage signal"), ctx.index("this ruling stands"),
            "coverage's own text must stay first when it has evidence",
        )
        log_text = (self.home / "hook.log").read_text()
        last = self._standing_line(log_text)
        self.assertIn("outcome=compact-injected", last)
        self.assertIn("standing=injected", last)

    def test_compact_injects_standing_alone_when_no_state_file_exists_yet(self):
        self._add_standing_topic()
        session_id = "s-standing-compact-no-state"
        self.assertFalse(self.state_file(session_id).exists())
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("this ruling stands for the whole project", ctx)
        self.assertNotIn("Coverage signal", ctx)
        log_text = (self.home / "hook.log").read_text()
        last = self._standing_line(log_text)
        self.assertIn("outcome=compact-no-state", last)
        self.assertIn("standing=injected", last)

    def test_compact_never_dedupes_across_two_calls(self):
        self._add_standing_topic()
        session_id = "s-standing-compact-always"
        self.seed_ledger(session_id, [])

        proc1, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertTrue(proc1.stdout.strip())

        proc2, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertTrue(
            proc2.stdout.strip(),
            "compact must inject the standing digest every time, never dedupe",
        )

    def test_no_write_to_store_or_index_across_repeated_runs(self):
        self._add_standing_topic()
        self.assertTrue(git_is_clean(self.store_root))
        head_before = git_head(self.store_root)
        db = self.home / f"{self.project}.sqlite"
        sha_before = hashlib.sha256(db.read_bytes()).hexdigest()

        session_id = "s-standing-nowrite"
        for _ in range(20):
            proc, _ = run_script(
                SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue(git_is_clean(self.store_root))
        self.assertEqual(git_head(self.store_root), head_before)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), sha_before)

    def test_compact_no_state_then_resume_dedupes(self):
        """Fix-round MINOR (Codex): a `compact` with no prior state file
        used to inject the digest but never store its hash, so the very
        next `resume` in the same session had nothing to dedupe against
        and re-injected the identical digest a second time."""
        self._add_standing_topic()
        session_id = "s-standing-compact-then-resume"
        self.assertFalse(self.state_file(session_id).exists())

        proc1, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertTrue(proc1.stdout.strip())
        self.assertTrue(
            self.state_file(session_id).exists(),
            "compact must persist the standing hash even with no prior state file",
        )

        proc2, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "resume"), self.base_env()
        )
        self.assertEqual(
            proc2.stdout.strip(), "",
            "a following resume must dedupe against the hash compact just persisted",
        )

    def test_upgrade_required_shows_hint_once_per_session(self):
        """Fix-round MAJOR (both reviewers): a generation bump is not
        self-healing -- a project with no store commit after it would sit
        at `standing=skipped-upgrade-required` forever, silently, with
        nothing telling a human to reindex. The hint fires once per
        session, never on every SessionStart."""
        self._add_standing_topic()
        db = self.home / f"{self.project}.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', '5')"
        )
        conn.commit()
        conn.close()

        session_id = "s-standing-upgrade-hint"
        proc1, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        out1 = json.loads(proc1.stdout)
        ctx1 = out1["hookSpecificOutput"]["additionalContext"]
        # Fix-round MINOR (both reviewers): pins the exact FACT wording --
        # the previous version was an imperative ("run memcontinuum-
        # update.sh --apply") sitting inside additionalContext, against
        # this hook's own "never an imperative" rule; the sentence must
        # state what is true (naming the real generation numbers) and
        # never open with a bare verb addressed to the reader.
        self.assertEqual(
            f"Standing decisions unavailable this session: the decision index predates "
            f"this engine (generation 5 < {memidx.CURRENT_INDEX_GENERATION}); "
            f"memcontinuum-update.sh --apply repairs it.",
            ctx1,
        )
        self.assertFalse(
            ctx1.lstrip().split(":", 1)[0].split()[0].lower() in ("run", "reindex", "apply", "fix"),
            f"the fact must not open with a bare imperative verb: {ctx1!r}",
        )
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("standing=skipped-upgrade-required hint=shown", self._standing_line(log_text))

        proc2, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "resume"), self.base_env()
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(
            proc2.stdout.strip(), "",
            "the hint shows once per session, not on every SessionStart",
        )

    def test_generation_ahead_hint_names_the_direction_and_offers_no_apply_repair(self):
        """Fix-round MINOR (both reviewers): the OTHER direction of the
        ping-pong -- an index a NEWER engine already wrote. --apply is
        not a repair here (memidx.py reindex itself now refuses a
        generation ahead of its own), so the fact must say so, not repeat
        the behind-case remedy verbatim."""
        self._add_standing_topic()
        db = self.home / f"{self.project}.sqlite"
        future_gen = memidx.CURRENT_INDEX_GENERATION + 1
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', ?)",
            (str(future_gen),),
        )
        conn.commit()
        conn.close()

        session_id = "s-standing-ahead-hint"
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(
            f"Standing decisions unavailable this session: the decision index is newer "
            f"than this engine (generation {future_gen} > {memidx.CURRENT_INDEX_GENERATION}); "
            f"this checkout is older than whatever last reindexed it.",
            ctx,
        )
        self.assertNotIn("--apply repairs it", ctx)


class TestSessionStartHookLogRotation(HookTestBase):
    """eval-topic-logging section 5 (owner-approved add-on): rotation runs
    from sessionstart-remind.sh, once per session, at the startup/resume/
    clear session-init boundary only -- never on the write path itself.
    mc_rotate_hook_log's own mechanics are covered directly in
    TestMcRotateHookLog; this class proves sessionstart-remind.sh actually
    calls it there, and nowhere else."""

    def test_below_threshold_no_rotation(self):
        hook_log = self.home / "hook.log"
        hook_log.write_text("old content\n" * 2)
        env = self.base_env(MEMCONTINUUM_LOG_MAX_BYTES="1000000")
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload("s-rotate-below", "startup"), env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.home / "hook.log.1").exists())
        text = hook_log.read_text()
        self.assertIn("old content", text)
        self.assertIn("outcome=init", text)

    def test_startup_rotates_past_threshold(self):
        hook_log = self.home / "hook.log"
        old_content = "old content line\n" * 30
        hook_log.write_text(old_content)
        env = self.base_env(MEMCONTINUUM_LOG_MAX_BYTES=str(len(old_content) - 1))
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload("s-rotate-above", "startup"), env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rotated = self.home / "hook.log.1"
        self.assertTrue(rotated.exists(), "hook.log.1 must exist once the threshold is crossed")
        self.assertEqual(rotated.read_text(), old_content)
        new_text = hook_log.read_text()
        self.assertNotIn("old content", new_text)
        self.assertIn("outcome=init", new_text)

    def test_resume_and_clear_also_rotate(self):
        """Every session-init source (not just startup) sits above the
        rotation call in sessionstart-remind.sh's own branch."""
        for source in ("resume", "clear"):
            with self.subTest(source=source):
                hook_log = self.home / "hook.log"
                rotated = self.home / "hook.log.1"
                for p in (hook_log, rotated):
                    p.unlink(missing_ok=True)
                old_content = f"old {source} content\n" * 30
                hook_log.write_text(old_content)
                env = self.base_env(MEMCONTINUUM_LOG_MAX_BYTES=str(len(old_content) - 1))
                proc, _ = run_script(
                    SESSIONSTART_HOOK,
                    self.session_start_payload(f"s-rotate-{source}", source),
                    env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertTrue(rotated.exists())
                self.assertEqual(rotated.read_text(), old_content)

    def test_compact_source_never_rotates(self):
        """Rotation is tied to the session-INIT boundary only -- a mid-
        session compact must never trigger it, even past threshold."""
        hook_log = self.home / "hook.log"
        old_content = "old content line\n" * 30
        hook_log.write_text(old_content)
        env = self.base_env(MEMCONTINUUM_LOG_MAX_BYTES=str(len(old_content) - 1))
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload("s-rotate-compact", "compact"), env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.home / "hook.log.1").exists())
        self.assertIn("old content", hook_log.read_text())

    def test_unhandled_source_never_rotates(self):
        hook_log = self.home / "hook.log"
        old_content = "old content line\n" * 30
        hook_log.write_text(old_content)
        env = self.base_env(MEMCONTINUUM_LOG_MAX_BYTES=str(len(old_content) - 1))
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload("s-rotate-fork", "fork"), env
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.home / "hook.log.1").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permission bits")
    def test_unwritable_home_dir_does_not_break_sessionstart(self):
        """End-to-end fail-open proof (TestMcRotateHookLog's own
        test_unwritable_home_dir_fails_open_leaves_log_alone covers the
        rotation primitive in isolation; this proves the whole
        sessionstart-remind.sh run still completes normally around it)."""
        hook_log = self.home / "hook.log"
        old_content = "old content line\n" * 30
        hook_log.write_text(old_content)
        env = self.base_env(MEMCONTINUUM_LOG_MAX_BYTES=str(len(old_content) - 1))
        self.home.chmod(0o555)  # read+execute only -- mv/mkdir inside it must fail
        self.addCleanup(self.home.chmod, 0o755)
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload("s-rotate-unwritable", "startup"), env
        )
        self.home.chmod(0o755)  # restore before the assertion below reads the directory
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.home / "hook.log.1").exists())


# ---------------------------------------------------------------------------
# 4b. sessionstart-remind.sh (source=compact) -- the look-back twin
#     (docs/DESIGN.md 2026-08-30)
# ---------------------------------------------------------------------------


class TestSessionStartCompactLookback(HookTestBase):
    def _seed_pending(self, session_id, **pending_fields):
        base = {
            "unmapped": [],
            "coverage_status": "ok",
            "code_head_changed": False,
            "store_head_changed": False,
            "computed_at": time.time(),
        }
        base.update(pending_fields)
        self.seed_ledger(session_id, [])
        self.patch_state(session_id, pending=base)

    def test_no_coverage_thin_ge3_fires_lookback(self):
        session_id = "s-compact-lb-fires"
        self._seed_pending(session_id, user_turn_count=5, last_growth_turn=2)
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Look-back signal", ctx)
        self.assertIn("3 user turns", ctx)
        self.assertIn("context was just compacted", ctx)
        self.assertNotIn("Coverage signal", ctx)
        self.assertIn(
            "Did the conversation since then establish any ruling, incident, "
            "rejected alternative, priority, wording choice, money decision, or "
            "'not now' that the MemContinuum store should hold? Store: "
            + str(self.store_root) + " — a ruling is a new link in "
            "topics/<area>/<topic>.md, an incident is a file in incidents/ "
            "(see docs/SCHEMA.md); NOT Claude Code auto-memory. If none, say so once.",
            ctx,
        )
        self.assertIn(str(self.store_root), ctx)
        self.assertIn("NOT Claude Code auto-memory", ctx)
        self.assertNotIn("that memory/ should hold", ctx)
        self.assertEqual(scan_forbidden_lines(ctx), [])

    def test_thin_lt3_is_silent(self):
        session_id = "s-compact-lb-thin2"
        self._seed_pending(session_id, user_turn_count=5, last_growth_turn=3)
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        # a silent (non-firing) compact must not touch inject bookkeeping --
        # only an actual fire is allowed to stamp/increment those.
        state = self.load_state(session_id)
        self.assertEqual(state.get("lookback_count", 0), 0)
        self.assertNotEqual(state.get("last_inject_turn"), 5)
        self.assertEqual(state.get("pending"), {})
        # boundary pin (dual-gate finding 6): crossing to since=3 must now
        # fire -- proves the silence above was the thin-count boundary
        # itself, not just "compact never speaks" (also true, vacuously, on
        # a hook without the look-back twin at all).
        session_id2 = "s-compact-lb-thin3-boundary"
        self._seed_pending(session_id2, user_turn_count=6, last_growth_turn=3)
        proc2, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id2, "compact"), self.base_env()
        )
        self.assertIn("Look-back signal", proc2.stdout)

    def test_coverage_evidence_wins_over_lookback(self):
        session_id = "s-compact-lb-coveragewins"
        self._seed_pending(
            session_id,
            unmapped=["src/unmapped.py"],
            user_turn_count=10,
            last_growth_turn=0,
        )
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Coverage signal", ctx)
        self.assertNotIn("Look-back signal", ctx)

    def test_ignores_cooldown(self):
        """A recent last_inject_turn/last_inject_ts (as if a per-turn inject
        just fired) must NOT suppress the compact look-back -- it ignores
        cooldown entirely (one-shot loss point)."""
        session_id = "s-compact-lb-nocooldown"
        self._seed_pending(session_id, user_turn_count=6, last_growth_turn=2)
        self.patch_state(session_id, last_inject_turn=6, last_inject_ts=time.time())
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Look-back signal", ctx)

    def test_stamps_last_inject_after_firing(self):
        session_id = "s-compact-lb-stamps"
        before = time.time()
        self._seed_pending(session_id, user_turn_count=8, last_growth_turn=1)
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertIn("Look-back signal", proc.stdout)
        state = self.load_state(session_id)
        self.assertEqual(state.get("last_inject_turn"), 8)
        self.assertGreaterEqual(state.get("last_inject_ts", 0), before)

    def test_one_shot_then_silent(self):
        session_id = "s-compact-lb-oneshot"
        self._seed_pending(session_id, user_turn_count=5, last_growth_turn=0)
        payload = self.session_start_payload(session_id, "compact")
        proc1, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertIn("Look-back signal", proc1.stdout)
        proc2, _ = run_script(SESSIONSTART_HOOK, payload, self.base_env())
        self.assertEqual(proc2.stdout.strip(), "")

    def test_store_byte_identical(self):
        session_id = "s-compact-lb-bytesafe"
        self._seed_pending(session_id, user_turn_count=5, last_growth_turn=0)
        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env())
        self.assertTrue(git_is_clean(self.store_root))

    def test_completes_under_poisoned_pythonpath(self):
        session_id = "s-compact-lb-poison"
        self._seed_pending(session_id, user_turn_count=5, last_growth_turn=0)
        proc, elapsed = run_script(
            SESSIONSTART_HOOK,
            self.session_start_payload(session_id, "compact"),
            self.poisoned_base_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.0)
        # dual-gate finding 6: pin an actual fire under poison, not just
        # "didn't crash" (also true, vacuously, on a hook without the
        # look-back twin at all).
        self.assertIn("Look-back signal", proc.stdout)

    def test_stamps_last_inject_time(self):
        """Dual-gate finding 4 (MEDIUM): the compact twin must also stamp
        last_inject_time (not just last_inject_ts) -- coverage's own
        cooldown clock -- so a coverage candidate on the very next per-turn
        hook call doesn't read a stale/zero last_inject_time and fire right
        after this look-back."""
        session_id = "s-compact-lb-stampstime"
        before = time.time()
        self._seed_pending(session_id, user_turn_count=5, last_growth_turn=0)
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertIn("Look-back signal", proc.stdout)
        state = self.load_state(session_id)
        self.assertGreaterEqual(state.get("last_inject_time", 0), before)

    def test_log_line_includes_count(self):
        """Dual-gate finding 7 (LOW): lookback_count is tracked but was
        never logged for the compact twin either."""
        session_id = "s-compact-lb-logcount"
        self._seed_pending(session_id, user_turn_count=5, last_growth_turn=0)
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "compact"), self.base_env()
        )
        self.assertIn("Look-back signal", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        matches = [l for l in log_text.splitlines() if "compact-lookback" in l]
        self.assertEqual(len(matches), 1)
        self.assertIn("count=1", matches[0])


# ---------------------------------------------------------------------------
# 5. userprompt-remind.sh
# ---------------------------------------------------------------------------


class TestUserPromptRemind(HookTestBase):
    def test_bash_syntax_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(USERPROMPT_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    # F1 (ruling 68): a coverage_status the hook reads off `unmapped` that
    # isn't "ok" gets its own distinctly-loggable outcome, on top of (not
    # instead of) the existing degraded-input handling. Each test needs a
    # real code-kind ledger entry (deviation from the brief's literal test
    # text, which omits seed_ledger) -- otherwise CODE_PATHS is empty and
    # the hook never calls `unmapped` at all, so the assertion would pass
    # or fail for the wrong reason (see test_real_shaped_payload_no_source_
    # no_agent_proceeds above for the same seeding pattern).

    def test_uninitialized_logs_index_uninitialized(self):
        db = self.home / f"{self.project}.sqlite"
        db.unlink()
        session_id = "s-userprompt-uninit"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-uninitialized", (self.home / "hook.log").read_text())

    def test_upgrade_required_logs_index_upgrade_required(self):
        db = self.home / f"{self.project}.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', '1')")
        conn.commit(); conn.close()
        session_id = "s-userprompt-upgrade"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-upgrade-required", (self.home / "hook.log").read_text())

    def test_index_error_logs_index_error(self):
        db = self.home / f"{self.project}.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("ALTER TABLE records RENAME COLUMN path TO path_broken")
        conn.commit(); conn.close()
        session_id = "s-userprompt-index-error"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-error", (self.home / "hook.log").read_text())

    def test_degraded_logs_index_degraded(self):
        """Design R7 (audit MC-P2-03, TOP-0123 L7): a corrupt-blob sqlite
        file lands in `unmapped`'s broad except arm (a sqlite3.DatabaseError
        that is not an OperationalError), which now attaches a `degraded`
        object to the JSON -- the hook logs a distinct outcome token for it
        (on top of, never instead of, the existing coverage-unknown
        handling), so `stats` can count it."""
        db = self.home / f"{self.project}.sqlite"
        db.write_bytes(b"not a sqlite file at all")
        session_id = "s-userprompt-degraded"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-degraded reason=internal-error", (self.home / "hook.log").read_text())

    def test_quarantined_logs_index_quarantined_and_omits_the_unmapped_list(self):
        """audit MC-P1-03 / design R2 (TOP-0123 L2): a store holding one
        malformed record (already quarantined by a prior reindex) must
        make `unmapped` refuse the negative claim -- coverage_status
        "quarantined", `unmapped: []` -- and the hook logs a distinct
        outcome and prints the real status word instead of the hardcoded
        "stale", never falling back to listing (an empty) unmapped set."""
        db = self.home / f"{self.project}.sqlite"
        _write(self.store_root / "topics" / "bad.md",
               "---\ntype: topic\nid: TOP-9401\ntitle: Bad\nlinks: [\n---\nBody.\n")
        reindex(self.store_root, db, project=self.project)  # populates index_errors before the hook runs
        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM index_errors").fetchone(),
            "test setup bug: the bad record must already be quarantined",
        )
        conn.close()

        session_id = "s-userprompt-quarantined"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, elapsed = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-quarantined", (self.home / "hook.log").read_text())
        self.assertIn("Coverage signal", proc.stdout)
        self.assertIn("store index quarantined", proc.stdout)
        self.assertNotIn("unmapped.py", proc.stdout)

    def test_quarantined_logs_index_quarantined_after_self_heal_creates_it(self):
        """Fix wave 1, G2 (Grok MAJOR 2 / whole-branch-review BLOCKING-1):
        the PRE-heal path above starts from a store already quarantined by
        an explicit prior reindex. This covers the OTHER path: corrupting
        the covering topic ON DISK, without reindexing first, leaves the
        index STALE (real on-disk drift), not yet quarantined --
        userprompt-remind.sh's own `unmapped` call is what self-heals
        (reindex), which is what purges/quarantines the record. The hook
        must still log outcome=index-quarantined and never assert a false
        gap for the file that record used to cover (TOP-9001 covers
        src/mapped.py -- see TOPIC_MD/build_store_root above)."""
        topic_path = self.store_root / "topics" / "testing" / "mapped-topic.md"
        _write(topic_path, TOPIC_MD.replace("links:\n", "links: [\n", 1))
        db = self.home / f"{self.project}.sqlite"
        self.assertEqual(
            memidx.decision_index_state(db, self.project, root=self.store_root, verify_content=True),
            "stale",
            "test setup bug: corrupting the file without reindexing must read as on-disk drift, "
            "not yet quarantined",
        )

        session_id = "s-userprompt-quarantined-self-heal"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "mapped.py"), "code")])
        proc, elapsed = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("outcome=index-quarantined", (self.home / "hook.log").read_text())
        self.assertIn("Coverage signal", proc.stdout)
        self.assertIn("store index quarantined", proc.stdout)
        self.assertNotIn(
            "mapped.py", proc.stdout,
            "must never assert a gap for a file the just-quarantined record used to cover",
        )

        conn = sqlite3.connect(str(db)); conn.row_factory = sqlite3.Row
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM index_errors").fetchone(),
            "test setup bug: the hook's own self-heal must have quarantined the corrupted topic",
        )
        conn.close()

    def test_silent_on_empty_evidence(self):
        session_id = "s-prompt-empty"
        self.seed_ledger(session_id, [])
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_real_shaped_payload_no_source_no_agent_proceeds(self):
        """The contract fix's central claim: a real UserPromptSubmit payload
        (no `source` field at all, no agent_id) must fire the hook's normal
        coverage logic, not be silently rejected. `user_prompt_payload()`'s
        default is already real-shaped (no `source`), so this is the same
        payload shape every other test in this class uses -- named
        explicitly here as the regression pin for the dead-hook bug."""
        session_id = "s-prompt-realshape"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        payload = json.loads(self.user_prompt_payload(session_id))
        self.assertNotIn("source", payload)
        self.assertNotIn("agent_id", payload)
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Coverage signal", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=injected", log_text)

    def test_log_line_carries_project(self):
        """Liveness metric fix (INC-0103/INC-0105): every hook.log line must
        carry `project=<MC_PROJECT>` so memidx.py stats can group by
        project. userprompt-remind.sh sources memlib.sh, so this is really
        pinning mc_log's own project= append -- every outcome line from this
        hook (including agent-source and duplicate-delivery, not just
        injected) must carry it."""
        session_id = "s-prompt-project"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "userprompt" in l]
        self.assertTrue(matching)
        for line in matching:
            self.assertIn(f"project={self.project}", line, line)

    def test_arbitrary_source_field_does_not_block(self):
        """fix-round 2026-08-31: the source=="user" gate is gone -- a
        payload that happens to carry a `source` value (stale forwarding,
        or any value other than "user") must still fire normally, proving
        the gate was actually removed rather than merely never triggered by
        the default fixture shape."""
        session_id = "s-prompt-arbitrary-source"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, _ = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id, source="my-slash-cmd"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Coverage signal", proc.stdout)

    def test_agent_id_present_is_silent(self):
        session_id = "s-prompt-agent"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, agent_id="agent-123"),
            self.base_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=agent-source", log_text)

    def test_agent_type_alone_is_silent(self):
        """agent_type can be present without agent_id (a session run under
        `--agent`, not necessarily inside a subagent) -- the gate must catch
        that shape too, per the fix spec (agent_id OR agent_type)."""
        session_id = "s-prompt-agent-type-only"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        payload = json.loads(self.user_prompt_payload(session_id))
        payload["agent_type"] = "Explore"
        proc, _ = run_script(USERPROMPT_HOOK, json.dumps(payload), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=agent-source", log_text)

    def test_payload_keys_diagnostic_logged_even_when_agent_gate_skips(self):
        """fix-round 2026-08-31: the payload_keys= diagnostic was moved
        BEFORE the agent_id/agent_type gate specifically so a future
        contract mismatch stays visible even on a turn that gate goes on to
        skip -- this is what the old placement got wrong (it sat inside
        phase 1, reached only once the now-removed source=="user" gate had
        already let the turn through, so it never fired on a session where
        every real turn was being rejected)."""
        session_id = "s-prompt-agent-keys"
        self.seed_ledger(session_id, [])
        proc, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, agent_id="agent-999"),
            self.base_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        keys_lines = [l for l in log_text.splitlines() if "payload_keys=" in l]
        self.assertEqual(len(keys_lines), 1)
        self.assertIn("agent_id", keys_lines[0])
        self.assertIn("outcome=agent-source", log_text)

    def test_never_reads_user_input(self):
        """Two checks, both load-bearing (neither passes on a missing/no-op
        script -- the point of this test is a real, running hook that still
        never touches user_input, not an absent one that trivially can't):

        1. Static: the script source never DEREFERENCES the payload's
           `user_input` key -- `.get("user_input"` / `["user_input"]`
           shapes (either python quote style). TOP-0133 L2 amends ruling B
           (the prompt-query channel may derive query TERMS from the
           prompt, opt-in only -- see test_never_reads_prompt_key_either
           below), but that derivation happens ENTIRELY inside
           hooks/memlib.sh's mc_extract_fields, behind the "_prompt_terms"
           field NAME -- this script itself never touches the payload's
           raw `user_input`/`prompt` VALUE, on any turn, on or off. Plain
           substring checks would now false-positive on this script's own
           legitimate uses of the bare word (outcome names like
           `prompt-query`, the `prompt-query.projects` switch file, the
           literal FILE_PATH string "prompt" passed to
           mc_fallback_write_state) -- the dereference-shape regex is
           precise about what ruling B actually forbids.
        2. Dynamic: with a real payload whose user_input carries a poison
           string (channel OFF, the default -- prompt_key explicit here so
           this keeps testing that exact key), the hook still runs
           successfully (rc=0, actually injects, since evidence exists)
           and never leaks that string into its own log.
        """
        import re

        code_lines = [
            line for line in USERPROMPT_HOOK.read_text().splitlines()
            if not line.strip().startswith("#")
        ]
        deref_pattern = re.compile(r'''\.get\(\s*["']user_input["']|\[\s*["']user_input["']\s*\]''')
        offending = [line for line in code_lines if deref_pattern.search(line)]
        self.assertEqual(offending, [], f"script code (not comments) dereferences user_input: {offending}")

        session_id = "s-prompt-nouserinput"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        payload = self.user_prompt_payload(session_id, prompt_key="user_input")
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected a real injection (evidence exists)")

        log_text = (self.home / "hook.log").read_text() if (self.home / "hook.log").exists() else ""
        self.assertNotIn("must never be read", log_text)
        self.assertNotIn("must never be read", proc.stdout)

    def test_never_reads_prompt_key_either(self):
        """Payload contract addendum (Codex): accept BOTH `user_input` and
        `prompt` as the payload's prompt-text key -- the not-DEREFERENCE
        invariant must cover both names (same dereference-shape regex as
        test_never_reads_user_input above; a bare word-boundary check on
        `prompt` no longer works post-TOP-0133-L2 -- this script's own
        code legitimately says `prompt-query`/`prompt-query.projects`/
        `prompt-query-only` now). Channel OFF (the default, `base_env()`
        sets neither MEMCONTINUUM_PROMPT_QUERY nor the projects file), so
        this still proves the unconditional case: even a `prompt`-keyed
        payload is never dereferenced by this script's own code."""
        import re

        code_lines = [
            line for line in USERPROMPT_HOOK.read_text().splitlines()
            if not line.strip().startswith("#")
        ]
        deref_pattern = re.compile(r'''\.get\(\s*["'](?:user_input|prompt)["']|\[\s*["'](?:user_input|prompt)["']\s*\]''')
        offending = [line for line in code_lines if deref_pattern.search(line)]
        self.assertEqual(offending, [], f"script code (not comments) dereferences user_input/prompt: {offending}")

        session_id = "s-prompt-nopromptkey"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        payload = self.user_prompt_payload(session_id, prompt_key="prompt")
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected the same real injection as with user_input")
        self.assertIn("Coverage signal", proc.stdout)

        log_text = (self.home / "hook.log").read_text() if (self.home / "hook.log").exists() else ""
        state_text = json.dumps(self.load_state(session_id))
        self.assertNotIn("must never be read", log_text)
        self.assertNotIn("must never be read", proc.stdout)
        self.assertNotIn("must never be read", state_text)

    def test_never_exports_raw_payload(self):
        """Dual-gate finding 1 (BLOCKER): the raw payload (which may carry
        real prompt content under user_input/prompt) must never be placed
        in an EXPORTED environment variable -- an exported var is inherited
        by every subprocess this hook spawns (dirname/mkdir/flock/cat/
        timeout/env/python), not just whichever one call actually needs a
        piece of it. Static: no line anywhere in the script exports
        anything with PAYLOAD in its name."""
        code_lines = [
            line for line in USERPROMPT_HOOK.read_text().splitlines()
            if not line.strip().startswith("#")
        ]
        offending = [l for l in code_lines if "export" in l and "PAYLOAD" in l]
        self.assertEqual(offending, [], f"script exports a payload-bearing var: {offending}")

    def test_payload_content_never_reaches_any_child_environment(self):
        """Dynamic proof of the same rule: a sentinel standing in for real
        prompt content must never appear in the environment of ANY child
        process this hook spawns (not just never be logged). Wrapper
        scripts for flock/timeout/python dump their own environment before
        running the real command."""
        session_id = "s-prompt-envleak"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        sentinel = "SENTINEL-PAYLOAD-LEAK-CHECK-7f3a"
        dump_dir = Path(self.td) / "env-dumps"
        dump_dir.mkdir()
        wrapper_dir = Path(self.td) / "wrappers"
        wrapper_dir.mkdir()

        def make_wrapper(name, real_path):
            script = wrapper_dir / name
            script.write_text(
                "#!/usr/bin/env bash\n"
                f'env > "{dump_dir}/{name}.$$.$RANDOM.env"\n'
                f'exec "{real_path}" "$@"\n'
            )
            script.chmod(0o755)

        make_wrapper("flock", shutil.which("flock") or "/usr/bin/flock")
        make_wrapper("timeout", shutil.which("timeout") or "/usr/bin/timeout")

        python_wrapper = wrapper_dir / "python-wrapper"
        python_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f'env > "{dump_dir}/python.$$.$RANDOM.env"\n'
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        python_wrapper.chmod(0o755)

        env = self.base_env(MEMCONTINUUM_PYTHON=str(python_wrapper))
        env["PATH"] = f"{wrapper_dir}:{env['PATH']}"

        payload = json.dumps({
            "session_id": session_id,
            "hook_event_name": "UserPromptSubmit",
            "source": "user",
            "cwd": str(self.code_root),
            "prompt": sentinel,
        })
        proc, _ = run_script(USERPROMPT_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        dumps = list(dump_dir.glob("*.env"))
        self.assertTrue(dumps, "expected at least one wrapper env dump to have been produced")
        for dump_file in dumps:
            content = dump_file.read_text()
            self.assertNotIn(sentinel, content, f"{dump_file.name} leaked payload content:\n{content}")

    def test_prompt_id_dedupe_does_not_advance_turn_count(self):
        session_id = "s-prompt-dedupe"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_id="fixed-prompt-id-1")
        run_script(USERPROMPT_HOOK, payload, self.base_env())
        run_script(USERPROMPT_HOOK, payload, self.base_env())
        run_script(USERPROMPT_HOOK, payload, self.base_env())
        state = self.load_state(session_id)
        self.assertEqual(state.get("user_turn_count"), 1)

        log_text = (self.home / "hook.log").read_text() if (self.home / "hook.log").exists() else ""
        self.assertNotIn("fixed-prompt-id-1", log_text)
        # dual-gate finding 2: the raw id must not be readable in state either
        self.assertNotIn("fixed-prompt-id-1", json.dumps(state))

    def test_prompt_id_distinct_ids_each_advance_turn_count(self):
        session_id = "s-prompt-dedupe-distinct"
        self.seed_ledger(session_id, [])
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id, prompt_id="id-a"), self.base_env())
        state_a = self.load_state(session_id)
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id, prompt_id="id-b"), self.base_env())
        state = self.load_state(session_id)
        self.assertEqual(state.get("user_turn_count"), 2)
        # pin that per-prompt tracking actually happened -- not just "every
        # call advances the turn regardless of prompt_id", which would pass
        # identically on a hook with no dedupe mechanism at all.
        self.assertTrue(state.get("last_prompt_hash"))
        self.assertNotEqual(state_a.get("last_prompt_hash"), state.get("last_prompt_hash"))

    def test_prompt_hash_only_no_raw_id_in_state(self):
        """Dual-gate finding 2 (HIGH): state must hold only
        sha256(prompt_id)[:16] for dedupe, never the id itself."""
        session_id = "s-prompt-hashonly"
        self.seed_ledger(session_id, [])
        raw_id = "raw-id-must-not-appear-in-state-xyz"
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id, prompt_id=raw_id), self.base_env())
        state = self.load_state(session_id)
        self.assertNotIn(raw_id, json.dumps(state))
        self.assertNotIn("last_prompt_id", state)
        expected_hash = hashlib.sha256(raw_id.encode()).hexdigest()[:16]
        self.assertEqual(state.get("last_prompt_hash"), expected_hash)

    def test_legacy_last_prompt_id_purged_and_never_reaches_child_env(self):
        """Re-gate finding (HIGH, Codex): a pre-existing state file may
        still carry a legacy `last_prompt_id` key (a raw prompt_id, from
        before the sha256-fingerprint dedupe scheme existed). Two separate
        bugs let it leak: (a) mc_update_state_json used to pass the WHOLE
        existing state to its python transform via an exported env var
        (MC_EXISTING_STATE), which every subprocess that python spawns
        inherits, not just the one call that needs it; (b) the phase-1
        transform never dropped the legacy key, so it kept surviving from
        one state write to the next. Both must be fixed: the raw id must
        never appear in any child's environment, and it must be gone from
        the state file after this run."""
        session_id = "s-prompt-legacy-id-purge"
        self.seed_ledger(session_id, [])
        sentinel = "SENTINEL-RAW-ID"
        self.patch_state(session_id, last_prompt_id=sentinel)

        dump_dir = Path(self.td) / "env-dumps-legacy"
        dump_dir.mkdir()
        wrapper_dir = Path(self.td) / "wrappers-legacy"
        wrapper_dir.mkdir()

        def make_wrapper(name, real_path):
            script = wrapper_dir / name
            script.write_text(
                "#!/usr/bin/env bash\n"
                f'env > "{dump_dir}/{name}.$$.$RANDOM.env"\n'
                f'exec "{real_path}" "$@"\n'
            )
            script.chmod(0o755)

        make_wrapper("flock", shutil.which("flock") or "/usr/bin/flock")
        make_wrapper("timeout", shutil.which("timeout") or "/usr/bin/timeout")

        python_wrapper = wrapper_dir / "python-wrapper"
        python_wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f'env > "{dump_dir}/python.$$.$RANDOM.env"\n'
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        python_wrapper.chmod(0o755)

        env = self.base_env(MEMCONTINUUM_PYTHON=str(python_wrapper))
        env["PATH"] = f"{wrapper_dir}:{env['PATH']}"

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        dumps = list(dump_dir.glob("*.env"))
        self.assertTrue(dumps, "expected at least one wrapper env dump to have been produced")
        for dump_file in dumps:
            content = dump_file.read_text()
            self.assertNotIn(
                sentinel, content, f"{dump_file.name} leaked the legacy last_prompt_id:\n{content}"
            )

        state = self.load_state(session_id)
        self.assertNotIn("last_prompt_id", state, "legacy last_prompt_id must be purged from state")

    def test_duplicate_delivery_suppresses_whole_hook_even_when_thin(self):
        """Dual-gate finding 2 (HIGH): a duplicate prompt_id delivery must
        suppress the WHOLE hook -- no eligibility computed, no injection of
        any kind -- not just the turn-count bump. Constructed so the first
        delivery is genuinely ineligible (its own silence proves nothing on
        its own), then state is pushed into a thin-eligible position, then
        the SAME prompt_id is redelivered: only the dedupe short-circuit
        (not incidental cooldown from a real first fire) can explain
        continued silence."""
        session_id = "s-prompt-dup-thin"
        self.seed_ledger(session_id, [])
        proc1, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, prompt_id="dup-id-1"),
            self.base_env(),
        )
        self.assertEqual(proc1.stdout.strip(), "")
        self.patch_state(session_id, user_turn_count=5, last_growth_turn=0)
        proc2, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, prompt_id="dup-id-1"),
            self.base_env(),
        )
        self.assertEqual(
            proc2.stdout.strip(), "", "duplicate prompt_id must suppress even a thin-eligible turn"
        )
        state = self.load_state(session_id)
        self.assertEqual(state.get("user_turn_count"), 5, "duplicate must not advance the turn either")

    def test_swallowed_injection_retries_on_redelivery_without_double_advancing_turn(self):
        """Re-gate finding (HIGH, Codex+Grok): outer-timeout swallow. Phase 1
        commits the turn bump and prompt_hash stamp; the real injection text
        is only rendered later. A deadline landing between those two moments
        used to lose that prompt's inject forever -- the next identical
        redelivery read as an ordinary duplicate and was suppressed. Phase 1
        must instead leave a `delivery_open` marker so a redelivered,
        genuinely-swallowed prompt_id RETRIES the full decision path (without
        double-advancing user_turn_count or re-stamping the hash) instead of
        being dropped."""
        session_id = "s-prompt-swallow"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        prompt_id = "swallow-prompt-id-1"
        payload = self.user_prompt_payload(session_id, prompt_id=prompt_id)

        # A python wrapper that hangs forever ONLY for the call that renders
        # the actual hookSpecificOutput/additionalContext JSON (the "later"
        # half of the turn) -- every other call (phase 1's state commit
        # included) still runs for real, so phase 1's write genuinely lands
        # on disk before the outer 2s deadline kills the process group.
        hang_py = Path(self.td) / "hang-python-inject"
        hang_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *hookSpecificOutput*) sleep 10; exit 0 ;;\n"
            "  esac\n"
            "done\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        hang_py.chmod(0o755)
        env = self.base_env(MEMCONTINUUM_PYTHON=str(hang_py))

        proc1, elapsed1 = run_script(USERPROMPT_HOOK, payload, env, timeout=10.0)
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertEqual(proc1.stdout.strip(), "", "the swallowed run must not have produced stdout")
        self.assertLess(elapsed1, 3.0, "the outer 2s deadline must still have bounded the swallowed run")

        state_after_swallow = self.load_state(session_id)
        self.assertEqual(state_after_swallow.get("user_turn_count"), 1, "phase 1 must have committed the turn bump")
        self.assertTrue(state_after_swallow.get("last_prompt_hash"), "phase 1 must have stamped the hash")
        self.assertTrue(
            state_after_swallow.get("delivery_open"),
            "phase 1 must leave delivery_open true across an unresolved swallow",
        )

        # Re-deliver the SAME prompt_id with a healthy python -- this must be
        # recognized as a retry: it actually injects, and does not
        # double-advance the turn counter.
        proc2, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertTrue(proc2.stdout.strip(), "the retry must actually inject, not be suppressed as a duplicate")
        self.assertIn("Coverage signal", proc2.stdout)

        state_after_retry = self.load_state(session_id)
        self.assertEqual(
            state_after_retry.get("user_turn_count"), 1, "the retry must not double-advance the turn count"
        )
        self.assertFalse(
            state_after_retry.get("delivery_open"), "a completed delivery must close delivery_open"
        )

    def test_normal_duplicate_after_completed_run_stays_fully_suppressed(self):
        """Companion to the retry test above: a duplicate prompt_id that
        arrives after a run actually completed (delivery_open already
        closed, whether or not it injected) must still be treated as a
        plain duplicate-delivery -- fully silent, no turn advance -- never
        as a retry."""
        session_id = "s-prompt-normal-dup-after-completed"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        prompt_id = "completed-then-dup-1"
        payload = self.user_prompt_payload(session_id, prompt_id=prompt_id)

        proc1, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertTrue(proc1.stdout.strip(), "the first delivery should have injected (real evidence present)")
        state1 = self.load_state(session_id)
        self.assertFalse(state1.get("delivery_open"), "a completed injection must close delivery_open")

        proc2, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc2.stdout.strip(), "", "a duplicate after a completed run must stay fully suppressed")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=duplicate-delivery", log_text)

        state2 = self.load_state(session_id)
        self.assertEqual(state2.get("user_turn_count"), 1, "a real duplicate must not advance the turn")

    def test_overall_two_second_budget_bounds_sequential_subprocess_calls(self):
        """Dual-gate finding 3 (HIGH): this hook makes many SEQUENTIAL
        python calls that (macOS port, 2026-08-30) now carry no individual
        timeout of their own -- one outer watchdog bounds the whole run
        instead (see hooks/userprompt-remind.sh's guard block). A wrapper
        that merely hangs forever would get killed by that single watchdog
        and the hook would fail open almost immediately -- it does NOT
        exercise the "many successful-but-slow calls sum past budget"
        failure mode. Use a wrapper that adds real (but sub-2s) latency to
        every WORK call instead, so each individual call still succeeds;
        only their sum should blow the budget. It must NOT delay the
        watchdog launcher's own startup (MEMCONTINUUM_PYTHON also resolves
        that launcher's interpreter) -- detected via the MC_WATCHDOG_LAUNCHER
        marker every guard's -c source carries -- or the launcher's own
        2s deadline clock would not even start ticking until after the
        delay, breaking the bound this test means to prove (port's "adjust
        mechanics, keep bounds" instruction)."""
        session_id = "s-prompt-budget"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        slow_py = Path(self.td) / "slow-python-budget"
        slow_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *MC_WATCHDOG_LAUNCHER*) exec \"" + VENV_PYTHON + "\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            "sleep 1\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        slow_py.chmod(0o755)
        env = self.base_env(MEMCONTINUUM_PYTHON=str(slow_py))
        proc, elapsed = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env, timeout=20.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 2.5, f"expected an overall ~2s budget, took {elapsed:.3f}s")
        self.assertEqual(proc.stdout.strip(), "", "truncated by the budget before it could produce real output")

    def test_outer_deadline_covers_memlib_sourcing(self):
        """Re-gate finding (MED, Codex): the outer `timeout 2` re-exec used
        to happen AFTER `source memlib.sh` (which does its own mkdir -p
        work) -- so a slow/hung memlib.sh could blow the whole invocation's
        wall time with no bound at all, since it ran before the deadline
        even started ticking. The guard must be the literal first thing
        this script does (only `set -u` and resolving BASH_SOURCE may
        precede it), so sourcing memlib.sh happens INSIDE the re-exec'd
        child and is itself under the 2s deadline. Proven via a real seam
        (MC_MEMLIB_PATH, overridable but defaulting to the real sibling
        file) rather than pure timing: point it at a copy of memlib.sh with
        a 3s sleep prepended, and confirm the outer deadline still bounds
        the whole run."""
        session_id = "s-prompt-slow-memlib"
        self.seed_ledger(session_id, [])
        slow_memlib = Path(self.td) / "slow-memlib.sh"
        slow_memlib.write_text("sleep 3\n" + MEMLIB.read_text())
        env = self.base_env(MC_MEMLIB_PATH=str(slow_memlib))
        proc, elapsed = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id), env, timeout=8.0
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(
            elapsed,
            2.5,
            f"a slow memlib.sh source must still be bounded by the outer 2s deadline, took {elapsed:.3f}s",
        )

    def test_cat_on_path_does_not_delay_the_watchdog_deadline(self):
        """Finding 5 (MEDIUM): hooks/mc-watchdog.sh used to build its
        launcher source via `$(cat <<'EOF' ... EOF)` -- an external `cat`
        process run via command substitution, BEFORE the watchdog's own
        deadline clock starts ticking (that assignment happens at `source
        mc-watchdog.sh` time, strictly before the guard's Popen call). A
        slow/hung `cat` on PATH at that point would blow the whole
        invocation's wall time with no bound at all, exactly the same
        class of bug test_outer_deadline_covers_memlib_sourcing guards for
        memlib.sh. Proven via a real seam (a `cat` shim placed first on
        PATH that sleeps 5s before exec-ing the real `cat`) rather than
        pure code inspection: the outer ~2s budget must still bound the
        whole run."""
        session_id = "s-prompt-slow-cat-on-path"
        self.seed_ledger(session_id, [])
        real_cat = shutil.which("cat") or "/bin/cat"
        shim_dir = Path(self.td) / "slow-cat-bin"
        shim_dir.mkdir()
        (shim_dir / "cat").write_text(
            "#!/usr/bin/env bash\nsleep 5\nexec " + real_cat + ' "$@"\n'
        )
        (shim_dir / "cat").chmod(0o755)
        env = self.base_env(PATH=f"{shim_dir}:{os.environ.get('PATH', '')}")
        proc, elapsed = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id), env, timeout=10.0
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(
            elapsed,
            2.5,
            f"a slow `cat` on PATH must still be bounded by the outer watchdog deadline, "
            f"took {elapsed:.3f}s",
        )

    def test_payload_keys_logged_once_per_session(self):
        session_id = "s-prompt-payloadkeys"
        self.seed_ledger(session_id, [])
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        keys_lines = [l for l in log_text.splitlines() if "payload_keys=" in l]
        self.assertEqual(len(keys_lines), 1)
        # real-shaped default payload (fix-round 2026-08-31): no `source`
        # key at all -- the captured key list must reflect that, not the
        # pre-fix fixture shape.
        self.assertNotIn("source", keys_lines[0])
        self.assertIn("session_id", keys_lines[0])
        self.assertIn("cwd", keys_lines[0])

        # a second turn must not log payload_keys again
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        keys_lines = [l for l in log_text.splitlines() if "payload_keys=" in l]
        self.assertEqual(len(keys_lines), 1)

    def test_injects_on_first_growth_within_8_paths(self):
        session_id = "s-prompt-inject"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("Coverage signal", ctx)
        self.assertIn("src/unmapped.py", ctx)
        self.assertIn(
            "Any ruling, incident, or rejected alternative from this session that "
            "the MemContinuum store should hold? Store: " + str(self.store_root) + " — "
            "a ruling is a new link in topics/<area>/<topic>.md, an incident is a "
            "file in incidents/ (see docs/SCHEMA.md); NOT Claude Code auto-memory. "
            "If none, say so once.",
            ctx,
        )
        self.assertIn(str(self.store_root), ctx)
        self.assertIn("NOT Claude Code auto-memory", ctx)
        self.assertNotIn("that memory/ should hold", ctx)
        self.assertEqual(scan_forbidden_lines(ctx), [])

    def test_nudge_falls_back_when_root_unconfigured(self):
        """INC-0105: if MEMCONTINUUM_ROOT is unavailable, the nudge must
        render a placeholder rather than a bare folder name that could be
        misread as Claude Code auto-memory again."""
        session_id = "s-prompt-noroot"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        env = self.base_env(MEMCONTINUUM_ROOT="")
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertTrue(proc.stdout.strip())
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("<store root not configured>", ctx)
        self.assertNotIn("that memory/ should hold", ctx)

    def test_more_than_8_paths_truncated_with_plus_n(self):
        session_id = "s-prompt-many"
        entries = []
        for i in range(11):
            p = self.code_root / "src" / f"extra{i}.py"
            _write(p, f"# {i}\n")
            entries.append((str(p), "code"))
        self.seed_ledger(session_id, entries)
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertTrue(proc.stdout.strip())
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("+3", ctx)

    def test_cooldown_blocks_immediate_repeat_injection(self):
        session_id = "s-prompt-cooldown"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc1, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertTrue(proc1.stdout.strip())

        # more evidence grows, but cooldown (3 turns / 15 min) has not elapsed
        p2 = self.code_root / "src" / "unmapped2.py"
        _write(p2, "# more\n")
        state = self.load_state(session_id)
        import hashlib

        state["ledger"].append(
            {"path": str(p2), "kind": "code", "content_sha256": hashlib.sha256(b"more").hexdigest(), "seen_at": time.time()}
        )
        self.state_file(session_id).write_text(json.dumps(state))

        proc2, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc2.stdout.strip(), "", "cooldown should suppress the immediate next turn")

    def test_cooldown_falls_back_to_last_inject_ts_when_last_inject_time_absent(self):
        """Re-gate finding (LOW, Grok): coverage's own cooldown reads
        last_inject_time; if it is 0/absent it must fall back to
        last_inject_ts (the mirror image of the look-back's own
        last_inject_ts->last_inject_time fallback) so an upgraded/partial
        state does not read as "never injected" when a real inject
        timestamp exists under the other key. Constructed so the turn-based
        half of the cooldown OR-clause is deliberately false (only 1 turn
        elapsed since last_inject_turn), so only the time-based fallback
        can explain suppression."""
        session_id = "s-prompt-cooldown-ts-fallback"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        now = time.time()
        self.patch_state(session_id, user_turn_count=1, last_inject_turn=0, last_inject_ts=now - 60)
        state = self.load_state(session_id)
        del state["last_inject_time"]
        self.state_file(session_id).write_text(json.dumps(state))
        state_before = self.load_state(session_id)
        self.assertNotIn("last_inject_time", state_before)

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(
            proc.stdout.strip(),
            "",
            "a recent last_inject_ts must suppress the cooldown even with last_inject_time absent",
        )

    def test_no_growth_no_reinject(self):
        session_id = "s-prompt-nogrowth"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc1, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertTrue(proc1.stdout.strip())

        # simulate cooldown elapsed but ledger unchanged -- must stay silent
        state = self.load_state(session_id)
        state["last_inject_time"] = 0
        state["last_inject_turn"] = -999
        self.state_file(session_id).write_text(json.dumps(state))

        proc2, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc2.stdout.strip(), "")

    def test_store_byte_identical(self):
        session_id = "s-prompt-bytesafe"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertTrue(git_is_clean(self.store_root))

    def test_fail_open_missing_python(self):
        session_id = "s-prompt-nopython"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        env = self.base_env(MEMCONTINUUM_PYTHON="/no/such/python")
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_runs_cleanly_over_the_synthetic_payload_fixtures(self):
        """Dual-gate finding 7 (LOW): both documented payload-key variants
        (fixtures/payloads/) must run through the real hook without error --
        rc=0 either way, whether or not a state file happens to exist for
        that session (fail-open either way is fine; a traceback is not)."""
        for fixture in sorted(FIXTURES_DIR.glob("*.json")):
            with self.subTest(fixture=fixture.name):
                proc, _ = run_script(USERPROMPT_HOOK, fixture.read_text(), self.base_env())
                self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_latency_under_one_second_with_twenty_path_ledger_poisoned_pythonpath(self):
        session_id = "s-prompt-latency"
        entries = []
        for i in range(20):
            p = self.code_root / "src" / f"lat{i}.py"
            _write(p, f"# {i}\n")
            entries.append((str(p), "code"))
        self.seed_ledger(session_id, entries)
        proc, elapsed = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.poisoned_base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.0, f"userprompt hook took {elapsed:.3f}s with a 20-path ledger")

    def test_fail_open_when_watchdog_lib_missing(self):
        """R1 regression, round 4 gate: see TestLedgerPostEdit's twin."""
        session_id = "s-prompt-watchdog-lib-missing"
        env = self.base_env(MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh")
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")


# ---------------------------------------------------------------------------
# 5b. userprompt-remind.sh -- the T-thin look-back reminder
#     (docs/DESIGN.md 2026-08-30)
# ---------------------------------------------------------------------------

def lookback_question(store_root):
    return (
        "Did the conversation since then establish any ruling, incident, "
        "rejected alternative, priority, wording choice, money decision, or "
        "'not now' that the MemContinuum store should hold? Store: "
        + str(store_root) + " — a ruling is a new link in "
        "topics/<area>/<topic>.md, an incident is a file in incidents/ "
        "(see docs/SCHEMA.md); NOT Claude Code auto-memory. If none, say so once."
    )


class TestUserPromptLookback(HookTestBase):
    def _start(self, session_id):
        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env())
        self.seed_ledger(session_id, [])

    def _fire(self, session_id):
        return run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())

    def test_silent_turns_1_to_4_empty_ledger(self):
        session_id = "s-lb-silent-1-4"
        self._start(session_id)
        for turn in range(1, 5):
            with self.subTest(turn=turn):
                proc, _ = self._fire(session_id)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "", f"turn {turn} should be silent")
        # boundary pin (dual-gate finding 6): turn 5 must now fire -- proves
        # the silence above was the thin-count boundary actually being
        # evaluated, not just "this hook never speaks" (which would also
        # hold, vacuously, on a hook that lacks the look-back feature).
        proc5, _ = self._fire(session_id)
        self.assertIn("Look-back signal", proc5.stdout)

    def test_fires_at_thin_turn_5(self):
        session_id = "s-lb-fires-5"
        self._start(session_id)
        for _ in range(4):
            self._fire(session_id)
        proc, _ = self._fire(session_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip())
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.assertIn("Look-back signal", ctx)
        self.assertIn("5 user turns with no edited-file evidence.", ctx)
        self.assertNotIn("Coverage signal", ctx)
        self.assertIn(lookback_question(self.store_root), ctx)
        self.assertIn(str(self.store_root), ctx)
        self.assertIn("NOT Claude Code auto-memory", ctx)
        self.assertNotIn("that memory/ should hold", ctx)
        self.assertEqual(scan_forbidden_lines(ctx), [])
        # exactly one block -- one hookSpecificOutput, one question
        self.assertEqual(ctx.count("Did the conversation"), 1)

    def test_search_fallback_hits_are_listed_in_the_lookback(self):
        """search fallback (TOP-0133 L1): the look-back block reads the
        session's own `search_fallbacks` state (written by pre-edit-
        chain.sh/newfile-nudge.sh when their own fallback fires) and lists
        each hit as "search surfaced <title> (<id>) for <file>" -- never
        re-searching, never reading hook.log (ruling B: state and
        hook.log, never the prompt)."""
        session_id = "s-lb-fallback-listed"
        self._start(session_id)
        self.patch_state(
            session_id,
            search_fallbacks=[
                {
                    "file": "/repo/src/needle.py", "id": "TOP-7001",
                    "title": "Needle handling policy", "hook": "pre-edit-chain",
                },
            ],
        )
        for _ in range(4):
            self._fire(session_id)
        proc, _ = self._fire(session_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Look-back signal", ctx)
        self.assertIn(
            "search surfaced Needle handling policy (TOP-7001) for /repo/src/needle.py",
            ctx,
        )

    def test_search_fallback_listing_capped_at_eight_most_recent(self):
        session_id = "s-lb-fallback-capped"
        self._start(session_id)
        self.patch_state(
            session_id,
            search_fallbacks=[
                {"file": f"/repo/f{i}.py", "id": f"TOP-{i}", "title": f"Title {i}", "hook": "pre-edit-chain"}
                for i in range(12)
            ],
        )
        for _ in range(4):
            self._fire(session_id)
        proc, _ = self._fire(session_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx.count("search surfaced"), 8)
        self.assertIn("(TOP-11)", ctx)  # most recent, kept
        self.assertNotIn("(TOP-3)", ctx)  # oldest of the 12, dropped by the cap

    def test_no_search_fallback_state_means_no_extra_lines(self):
        session_id = "s-lb-fallback-none"
        self._start(session_id)
        for _ in range(4):
            self._fire(session_id)
        proc, _ = self._fire(session_id)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("search surfaced", ctx)

    def test_coverage_and_thin_same_turn_coverage_wins(self):
        session_id = "s-lb-coverage-wins"
        self._start(session_id)
        # advance 4 silent turns first (empty ledger -- last_growth_turn
        # stays at its default 0, so turn 5 would independently satisfy
        # the thin condition: 5 - 0 >= 5).
        for _ in range(4):
            self._fire(session_id)
        # seed (not via the real ledger hook, which would itself count as
        # growth and reset the thin baseline) a genuinely unmapped code
        # file so coverage ALSO has real evidence on this same turn 5.
        fpath = str(self.code_root / "src" / "unmapped.py")
        self.seed_ledger(session_id, [(fpath, "code")])
        proc, _ = self._fire(session_id)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Coverage signal", ctx)
        self.assertNotIn("Look-back signal", ctx)
        # never two blocks -- exactly one JSON object on stdout
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1)

    def test_ledger_growth_resets_thin_stretch(self):
        session_id = "s-lb-growth-resets"
        self._start(session_id)
        for _ in range(4):
            self._fire(session_id)  # turns 1-4, silent
        # growth at turn 4's boundary -- an already-mapped file, so coverage
        # keeps finding no evidence and always falls through to look-back.
        fpath = str(self.code_root / "src" / "mapped.py")
        run_script(LEDGER_HOOK, self.post_tool_use_payload(session_id, fpath), self.base_env())
        state = self.load_state(session_id)
        self.assertEqual(state.get("last_growth_turn"), 4)

        # turn 5 would have fired WITHOUT the reset (5 - 0 >= 5); with the
        # reset (baseline now turn 4) it must stay silent (5 - 4 = 1).
        proc, _ = self._fire(session_id)
        self.assertEqual(proc.stdout.strip(), "", "growth must reset the thin stretch")

    def test_full_new_thin_stretch_required_after_coverage_inject(self):
        session_id = "s-lb-after-coverage"
        self._start(session_id)
        # turn 1: a genuinely unmapped file -- coverage is a candidate,
        # has evidence, and injects (mirrors test_injects_on_first_growth).
        fpath = str(self.code_root / "src" / "unmapped.py")
        run_script(LEDGER_HOOK, self.post_tool_use_payload(session_id, fpath), self.base_env())
        proc1, _ = self._fire(session_id)
        self.assertIn("Coverage signal", proc1.stdout)
        state = self.load_state(session_id)
        self.assertEqual(state.get("last_inject_turn"), 1)

        # turns 2-5: no further ledger growth, coverage silent (cooldown/no
        # growth) -- since_turn = turn - 1, so turn 5 gives since_turn=4:
        # must stay silent (a NEW full 5-turn stretch is required after the
        # coverage inject, not just 5 turns since session start).
        for turn in range(2, 6):
            with self.subTest(turn=turn):
                proc, _ = self._fire(session_id)
                self.assertEqual(proc.stdout.strip(), "", f"turn {turn} should still be silent")

        # turn 6: since_turn = 6 - 1 = 5 -- fires.
        proc6, _ = self._fire(session_id)
        self.assertIn("Look-back signal", proc6.stdout)
        self.assertIn("5 user turns", proc6.stdout)

    def test_repeated_thin_stretches_fire_every_time_no_cap(self):
        """Owner's 2026-08-30 14:02 spec change: the per-session cap is
        removed. A look-back may fire any number of times, each time a full
        new thin stretch elapses."""
        session_id = "s-lb-no-cap"
        self._start(session_id)
        fire_turns = []
        for cycle in range(4):
            for _ in range(4):
                proc, _ = self._fire(session_id)
                self.assertEqual(proc.stdout.strip(), "")
            proc, _ = self._fire(session_id)
            self.assertIn("Look-back signal", proc.stdout, f"cycle {cycle} (5th turn) should fire")
            fire_turns.append(json.loads(proc.stdout))
        self.assertEqual(len(fire_turns), 4, "no cap -- all 4 cycles must fire")
        state = self.load_state(session_id)
        self.assertEqual(state.get("lookback_count"), 4, "count is tracked for logging, but never gates")

    def test_time_or_twenty_minutes_fires(self):
        session_id = "s-lb-time-fires"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=2, last_growth_ts=time.time() - 1205)
        proc, _ = self._fire(session_id)
        self.assertIn("Look-back signal", proc.stdout)

    def test_time_under_twenty_minutes_is_silent(self):
        session_id = "s-lb-time-silent"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=2, last_growth_ts=time.time() - 1140)
        proc, _ = self._fire(session_id)
        self.assertEqual(proc.stdout.strip(), "")
        # boundary pin (dual-gate finding 6): crossing the 1200s threshold
        # must now fire -- proves this test exercises the time boundary
        # itself, not just "this hook never speaks".
        session_id2 = "s-lb-time-silent-boundary"
        self._start(session_id2)
        self.patch_state(session_id2, user_turn_count=2, last_growth_ts=time.time() - 1205)
        proc2, _ = self._fire(session_id2)
        self.assertIn("Look-back signal", proc2.stdout)

    def test_upgrade_missing_last_inject_ts_falls_back_to_last_inject_time(self):
        """Dual-gate finding 5 (MEDIUM): a pre-existing/upgraded session's
        state may carry last_inject_time (from a real prior coverage
        inject) but lack the newer last_inject_ts key entirely. Defaulting
        that missing key to 0 makes the 20-minute look-back branch fire
        immediately even though a real inject just happened -- it must
        fall back to last_inject_time instead."""
        session_id = "s-lb-upgrade-fallback"
        # seed_ledger's own defaults deliberately omit last_inject_ts,
        # simulating state written before this key existed.
        self.seed_ledger(session_id, [])
        self.patch_state(
            session_id,
            user_turn_count=2,
            last_growth_ts=time.time() - 1300,  # session "20+ min stale" by growth alone
            last_inject_time=time.time() - 60,   # but a coverage inject fired 60s ago
        )
        state_before = self.load_state(session_id)
        self.assertNotIn("last_inject_ts", state_before)
        proc, _ = self._fire(session_id)
        self.assertEqual(
            proc.stdout.strip(),
            "",
            "a recent last_inject_time must suppress the look-back even without last_inject_ts",
        )
        # boundary pin: the SAME missing-last_inject_ts shape, but with a
        # genuinely stale last_inject_time too, must still fire -- proves
        # this test is exercising the fallback logic itself, not just
        # "this hook never speaks" (also true, vacuously, on a hook
        # without the look-back feature at all).
        session_id2 = "s-lb-upgrade-fallback-fires"
        self.seed_ledger(session_id2, [])
        self.patch_state(
            session_id2,
            user_turn_count=2,
            last_growth_ts=time.time() - 1300,
            last_inject_time=time.time() - 1300,
        )
        self.assertNotIn("last_inject_ts", self.load_state(session_id2))
        proc2, _ = self._fire(session_id2)
        self.assertIn("Look-back signal", proc2.stdout)

    def test_lookback_stamps_coverage_clock_suppressing_next_turn(self):
        """Dual-gate finding 4 (MEDIUM): a look-back write must also stamp
        last_inject_time (coverage's own cooldown clock) -- otherwise a
        coverage candidate on the very next turn reads last_inject_time as
        its stale pre-feature value (0) and its time-based cooldown
        OR-clause trivially passes, letting coverage fire right after a
        look-back instead of waiting out the shared cooldown."""
        session_id = "s-lb-stamps-coverage-clock"
        self._start(session_id)
        for _ in range(4):
            self._fire(session_id)
        proc5, _ = self._fire(session_id)
        self.assertIn("Look-back signal", proc5.stdout)

        # An edit lands right after the look-back, creating a genuinely
        # unmapped file -- coverage now has real evidence.
        fpath = str(self.code_root / "src" / "unmapped.py")
        run_script(LEDGER_HOOK, self.post_tool_use_payload(session_id, fpath), self.base_env())

        # Turn 6: only 1 turn since the look-back's last_inject_turn=5 --
        # cooldown (>=3 turns OR >=15min) must still be closed.
        proc6, _ = self._fire(session_id)
        self.assertEqual(
            proc6.stdout.strip(), "", "coverage must stay silent one turn after a look-back (shared cooldown)"
        )

        # By turn 8, 3 turns have elapsed since turn 5 -- cooldown re-opens
        # and coverage (which has real evidence) fires.
        proc7, _ = self._fire(session_id)
        proc8, _ = self._fire(session_id)
        fired = proc7.stdout.strip() or proc8.stdout.strip()
        self.assertIn("Coverage signal", fired, "coverage must fire once its shared cooldown re-opens")

    def test_arbitrary_source_field_still_fires_when_thin(self):
        """fix-round 2026-08-31: the source=="user" gate is gone -- an
        arbitrary `source` value must no longer silence the look-back
        reminder either (it used to; this is the look-back twin of
        TestUserPromptRemind.test_arbitrary_source_field_does_not_block)."""
        session_id = "s-lb-nonuser"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=20, last_growth_turn=0)
        proc, _ = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id, source="my-slash-cmd"), self.base_env()
        )
        self.assertIn("Look-back signal", proc.stdout)

    def test_agent_id_silent_even_when_thin(self):
        session_id = "s-lb-agent"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=20, last_growth_turn=0)
        proc, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, agent_id="agent-1"),
            self.base_env(),
        )
        self.assertEqual(proc.stdout.strip(), "")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=agent-source", log_text)

    def test_store_byte_identical_on_lookback_fire(self):
        session_id = "s-lb-bytesafe"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=5, last_growth_turn=0)
        proc, _ = self._fire(session_id)
        self.assertIn("Look-back signal", proc.stdout)
        self.assertTrue(git_is_clean(self.store_root))

    def test_fail_open_missing_python_when_thin(self):
        session_id = "s-lb-nopython"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=5, last_growth_turn=0)
        env = self.base_env(MEMCONTINUUM_PYTHON="/no/such/python")
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_latency_under_one_second_on_lookback_fire_poisoned_pythonpath(self):
        session_id = "s-lb-latency"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=5, last_growth_turn=0)
        proc, elapsed = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.poisoned_base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.0)
        self.assertIn("Look-back signal", proc.stdout)

    def test_log_line_format(self):
        session_id = "s-lb-logformat"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=4, last_growth_turn=0)
        proc, _ = self._fire(session_id)
        self.assertIn("Look-back signal", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        matches = [l for l in log_text.splitlines() if "lookback-injected" in l]
        self.assertEqual(len(matches), 1)
        self.assertIn("outcome=lookback-injected", matches[0])
        self.assertIn("turn=5", matches[0])
        self.assertIn("since=5", matches[0])
        self.assertIn(
            "count=1", matches[0], "lookback_count is tracked but was never logged -- must appear in the line"
        )
        self.assertNotIn("must never be read", log_text)

    def test_log_line_count_falls_back_to_placeholder_when_bookkeeping_write_fails(self):
        """Re-gate finding (NIT, Grok): the look-back count side-channel
        (DECIDE_TMP) is reused as a scratch file -- it holds the phase-1
        decision JSON until the bookkeeping transform overwrites it with a
        clean digit string. If that transform never runs at all (its state
        write failed, so it never reaches the file-write line), DECIDE_TMP
        still holds the STALE phase-1 JSON, and that must never be spliced
        verbatim into the log line as `count=...` -- it must fall back to a
        `?` placeholder instead."""
        session_id = "s-lb-count-write-fails"
        self._start(session_id)

        # A python wrapper that fails ONLY the one call whose transform
        # source contains "lookback_count" (unique to the look-back
        # bookkeeping write) -- every other call, including the phase-1
        # write that first populates DECIDE_TMP with the decision JSON,
        # still runs for real.
        fail_py = Path(self.td) / "fail-lookback-count-python"
        fail_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *lookback_count*) exit 1 ;;\n"
            "  esac\n"
            "done\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        fail_py.chmod(0o755)
        env = self.base_env(MEMCONTINUUM_PYTHON=str(fail_py))

        for _ in range(4):
            proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
            self.assertEqual(proc.stdout.strip(), "")
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Look-back signal", proc.stdout)

        log_text = (self.home / "hook.log").read_text()
        matches = [l for l in log_text.splitlines() if "lookback-injected" in l]
        self.assertEqual(len(matches), 1)
        self.assertIn("count=?", matches[0])
        self.assertNotIn("{", matches[0], "the raw decision JSON must never leak into the log line")
        self.assertNotIn("hookSpecificOutput", matches[0])


# ---------------------------------------------------------------------------
# 5d. userprompt-remind.sh -- prompt-derived search queries (TOP-0133 L2)
# ---------------------------------------------------------------------------


class TestUserPromptPromptQuery(HookTestBase):
    """The second, opt-in search-fallback channel: the prompt hook derives
    FTS-only query terms from the user's own prompt text, in process, and
    discards it. Fixture: TWO topics that both match the same query words
    (`MATCH_PROMPT` below), ranked deterministically FTS-first TOP-8001,
    second TOP-8002 (pinned by test_setup_ranking_is_as_expected below) --
    this is what lets a single fixture exercise both "a hit" and "the
    first hit is already surfaced, the second is picked instead" without a
    second store. TOP-8001 also carries a `standing:` pointer, reused by
    the Addendum B tests. Neither topic's `code_refs` cover any file this
    class's ledger ever touches, so pre-edit/for-path/coverage never see
    them by accident -- only the prompt-query channel's own search call
    can surface either one."""

    MATCH_PROMPT = "please investigate the widget caching regression carefully today"
    NO_MATCH_PROMPT = "banana pineapple coconut lighthouse trombone yesterday"

    WIDGET_STANDING_TOPIC = """---
type: topic
id: TOP-8001
title: Widget caching policy
area: testing
current: L1
standing: [L1]
code_refs:
  - somewhere/else/unrelated1.py
links:
  - link: L1
    date: 2026-01-01
    status: active
    kind: adopted
    ruling:
      text: "cache the widget carefully during regression"
      authority: owner-verbatim
      source: "test"
    recorded_by: agent
    recorded_at: 2026-01-01
---

Body prose about widget caching regression handling in detail.
"""

    WIDGET_SECOND_TOPIC = """---
type: topic
id: TOP-8002
title: Widget request throttling
area: testing
current: L1
code_refs:
  - somewhere/else/unrelated2.py
links:
  - link: L1
    date: 2026-01-01
    status: active
    kind: adopted
    ruling:
      text: "throttle widget requests carefully during regression"
      authority: owner-verbatim
      source: "test"
    recorded_by: agent
    recorded_at: 2026-01-01
---

Body prose about widget regression throttling handling in detail.
"""

    def setUp(self):
        super().setUp()
        _write(self.store_root / "topics" / "testing" / "widget-standing.md", self.WIDGET_STANDING_TOPIC)
        _write(self.store_root / "topics" / "testing" / "widget-second.md", self.WIDGET_SECOND_TOPIC)
        subprocess.run(["git", "add", "-A"], cwd=self.store_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add widget topics"], cwd=self.store_root, check=True)
        db = self.home / f"{self.project}.sqlite"
        with contextlib.redirect_stdout(io.StringIO()):
            reindex(self.store_root, db, project=self.project)

    def pq_env(self, **overrides):
        overrides.setdefault("MEMCONTINUUM_PROMPT_QUERY", "1")
        overrides.setdefault("MEMCONTINUUM_FALLBACK_MODE", "fts")
        return self.base_env(**overrides)

    def _last_pq_line(self, log_text, needle="prompt-query"):
        # Exact outcome-token match ("outcome=prompt-query " must never
        # also match "outcome=prompt-query-only " or "-empty ") --
        # finish()'s own outcome token can legitimately be
        # "prompt-query-only" on the SAME line set this helper searches,
        # so a plain substring test picks up the wrong line.
        matching = [
            l for l in log_text.splitlines()
            if " outcome=" in l and l.split(" outcome=", 1)[1].split(" ", 1)[0] == needle
        ]
        self.assertTrue(matching, log_text)
        return matching[-1]

    def test_prompt_query_never_returns_readme_or_sources_records(self):
        """Decision records only: a store README and a sources/ record that
        match the prompt's words better than either topic are never
        returned."""
        _write(self.store_root / "README.md", "# Store\n" + "widget caching regression carefully " * 10 + "\n")
        _write(self.store_root / "sources" / "sitting.md",
               "---\ntype: source\nid: SITTING-1\ntitle: widget caching regression\n---\n"
               + "widget caching regression carefully " * 10 + "\n")
        subprocess.run(["git", "add", "-A"], cwd=self.store_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add readme and source"], cwd=self.store_root, check=True)
        with contextlib.redirect_stdout(io.StringIO()):
            reindex(self.store_root, self.home / f"{self.project}.sqlite", project=self.project)
        session_id = "s-pq-decisions-only"
        self.seed_ledger(session_id, [])
        proc, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT),
            self.pq_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        line = self._last_pq_line((self.home / "hook.log").read_text())
        self.assertIn("ids=TOP-8001", line)
        self.assertNotIn("README", line)
        self.assertNotIn("SITTING", line)

    def test_setup_ranking_is_as_expected(self):
        """Not a spec requirement -- pins this fixture's own FTS ranking
        (TOP-8001 first, TOP-8002 second on MATCH_PROMPT) so a future
        content edit that silently flips it fails loudly here instead of
        making test_already_surfaced_falls_through_to_second_topic below
        fail for the wrong reason."""
        session_id = "s-pq-ranking-pin"
        self.seed_ledger(session_id, [])
        proc, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT),
            self.pq_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text)
        self.assertIn("outcome=prompt-query ", line)
        self.assertIn("ids=TOP-8001", line)

    # -- off by default ----------------------------------------------------

    def test_off_by_default_no_line_no_search_no_leak(self):
        session_id = "s-pq-off-default"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT + " with ten real content words extra")
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("prompt-query", log_text)
        self.assertIn("outcome=no-evidence", log_text)
        state_text = json.dumps(self.load_state(session_id))
        for word in ("widget", "caching", "regression", "carefully"):
            self.assertNotIn(word, log_text)
            self.assertNotIn(word, state_text)
            self.assertNotIn(word, proc.stdout)

    # -- the opt-in switch ---------------------------------------------------

    def test_on_via_env(self):
        session_id = "s-pq-on-env"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Nearest recorded decision by search on this prompt's words -- may be unrelated:", ctx)
        self.assertIn("TOP-8001", ctx)
        self.assertIn("cache the widget carefully during regression", ctx)

        state = self.load_state(session_id)
        fallbacks = state.get("search_fallbacks")
        self.assertTrue(fallbacks)
        self.assertEqual(fallbacks[0]["file"], "prompt")
        self.assertEqual(fallbacks[0]["id"], "TOP-8001")
        self.assertEqual(fallbacks[0]["title"], "Widget caching policy")

        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text)
        self.assertIn("outcome=prompt-query ", line)
        self.assertIn("hits=1", line)
        self.assertIn("ids=TOP-8001", line)
        self.assertIn("q=", line)

        # No-write property: index sha and store git status unchanged.
        db = self.home / f"{self.project}.sqlite"
        sha_before = hashlib.sha256(db.read_bytes()).hexdigest()
        proc2, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id + "-2", prompt_text=self.MATCH_PROMPT),
            self.pq_env(),
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        sha_after = hashlib.sha256(db.read_bytes()).hexdigest()
        self.assertEqual(sha_before, sha_after, "the search call must never write to the index")
        self.assertTrue(git_is_clean(self.store_root), "the search call must never write to the store")

    def test_on_via_projects_file(self):
        (self.home / "prompt-query.projects").write_text(f"{self.project}\n")
        session_id = "s-pq-on-file"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=prompt-query ", self._last_pq_line(log_text))

    def test_projects_file_listing_a_different_project_stays_off(self):
        (self.home / "prompt-query.projects").write_text("some-other-project\n")
        session_id = "s-pq-off-file-other-project"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("prompt-query", log_text)

    def test_env_zero_wins_over_the_projects_file(self):
        (self.home / "prompt-query.projects").write_text(f"{self.project}\n")
        session_id = "s-pq-off-env-zero"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.base_env(MEMCONTINUUM_PROMPT_QUERY="0"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("prompt-query", log_text)

    # -- the four-content-word gate ------------------------------------------

    def test_three_content_words_too_few_terms(self):
        session_id = "s-pq-three-words"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text="fix urgent bug")
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text, "prompt-query-empty")
        self.assertIn("outcome=prompt-query-empty reason=too-few-terms q=", line)

    def test_stopwords_and_non_ascii_are_too_few_terms(self):
        session_id_a = "s-pq-stopwords"
        self.seed_ledger(session_id_a, [])
        payload_a = self.user_prompt_payload(session_id_a, prompt_text="the a an is are was")
        proc_a, _ = run_script(USERPROMPT_HOOK, payload_a, self.pq_env())
        self.assertEqual(proc_a.returncode, 0, proc_a.stderr)
        log_a = (self.home / "hook.log").read_text()
        self.assertIn("outcome=prompt-query-empty reason=too-few-terms", self._last_pq_line(log_a, "prompt-query-empty"))

        session_id_b = "s-pq-nonascii"
        self.seed_ledger(session_id_b, [])
        payload_b = self.user_prompt_payload(session_id_b, prompt_text="проверка виджета кэширования сегодня")
        proc_b, _ = run_script(USERPROMPT_HOOK, payload_b, self.pq_env())
        self.assertEqual(proc_b.returncode, 0, proc_b.stderr)
        log_b = (self.home / "hook.log").read_text()
        self.assertIn("outcome=prompt-query-empty reason=too-few-terms", self._last_pq_line(log_b, "prompt-query-empty"))

    def test_look_back_reminder_survives_filler_with_look_removed(self):
        """Fix round 2 (TOP-0133 L2, Grok NIT): `_PROMPT_FILLER` used to
        include "look" -- but this store's own "look-back" (the feature
        name) tokenizes to `look` + `back`, and `look` is the distinctive
        half of that compound; filtering it out defeated a search for the
        store's own feature name. "please explain the look-back reminder
        design" tokenizes to please/explain/the/look/back/reminder/design;
        "the" is a stopword, "please"/"explain" stay filler -- with `look`
        no longer filler, `look back reminder design` survives: four
        terms, clears the too-few-terms gate, and `look` itself is among
        them (q= in the log line, +-joined)."""
        session_id = "s-pq-lookback-term"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(
            session_id, prompt_text="please explain the look-back reminder design",
        )
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text, "prompt-query-empty")
        self.assertNotIn("too-few-terms", line, line)
        self.assertIn("reason=no-hits", line, line)
        q_field = next(p for p in line.split() if p.startswith("q="))
        terms = q_field[len("q="):].split("+")
        self.assertIn("look", terms, terms)

    # -- no hits --------------------------------------------------------------

    def test_no_fts_match_reason_no_hits(self):
        session_id = "s-pq-no-hits"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.NO_MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("reason=no-hits", self._last_pq_line(log_text, "prompt-query-empty"))
        self.assertEqual(proc.stdout.strip(), "", "no other block fired either -- nothing to inject")

    # -- per-session dedup (Addendum B: search_fallbacks union standing_ids) -

    def test_already_surfaced_when_both_matching_topics_excluded(self):
        session_id = "s-pq-both-excluded"
        self.seed_ledger(session_id, [])
        self.patch_state(
            session_id,
            search_fallbacks=[
                {"file": "prompt", "id": "TOP-8001", "title": "Widget caching policy", "hook": "userprompt"},
                {"file": "prompt", "id": "TOP-8002", "title": "Widget request throttling", "hook": "userprompt"},
            ],
        )
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text, "prompt-query-empty")
        self.assertIn("reason=already-surfaced", line)
        self.assertNotIn("TOP-8001", proc.stdout)
        self.assertNotIn("TOP-8002", proc.stdout)

    def test_already_surfaced_falls_through_to_second_topic(self):
        """search_fallbacks already carries the first-ranked topic (as if
        pre-edit-chain.sh surfaced it earlier this session) -- with
        --limit 3 the second, different matching topic is picked instead."""
        session_id = "s-pq-first-excluded"
        self.seed_ledger(session_id, [])
        self.patch_state(
            session_id,
            search_fallbacks=[
                {"file": "/some/file.py", "id": "TOP-8001", "title": "Widget caching policy", "hook": "pre-edit-chain"},
            ],
        )
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text)
        self.assertIn("outcome=prompt-query ", line)
        self.assertIn("ids=TOP-8002", line)
        self.assertIn("TOP-8002", proc.stdout)
        self.assertNotIn("TOP-8001", proc.stdout)

    def test_standing_topic_already_surfaced_falls_through(self):
        """Addendum B: a topic the session-start standing digest already
        delivered (state["standing_ids"], written by
        sessionstart-remind.sh) is excluded the same way a search-fallback
        hit is -- the next matching, non-standing topic is picked."""
        session_id = "s-pq-standing-excluded"
        self.seed_ledger(session_id, [])
        self.patch_state(session_id, standing_ids=["TOP-8001"])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text)
        self.assertIn("outcome=prompt-query ", line)
        self.assertIn("ids=TOP-8002", line)
        self.assertIn("TOP-8002", proc.stdout)
        self.assertNotIn("TOP-8001", proc.stdout)

    # -- merge into the three existing emission points -----------------------

    def test_merge_into_coverage_envelope(self):
        session_id = "s-pq-merge-coverage"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # ONE stdout JSON document.
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Coverage signal", ctx)
        self.assertIn("Nearest recorded decision by search on this prompt's words -- may be unrelated:", ctx)
        # Order: existing block first, then the guess.
        self.assertLess(ctx.index("Coverage signal"), ctx.index("Nearest recorded decision"))
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=injected", log_text)
        self.assertIn("outcome=prompt-query ", self._last_pq_line(log_text))
        self.assertNotIn("outcome=prompt-query-only", log_text)

    def test_merge_into_lookback_envelope(self):
        session_id = "s-pq-merge-lookback"
        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env())
        self.seed_ledger(session_id, [])
        # Four silent warm-up turns (channel OFF, so these never surface
        # TOP-8001/TOP-8002 themselves and never consume the exclusion
        # budget) to reach the look-back's own turn-5 threshold, exactly
        # TestUserPromptLookback's own pattern.
        for _ in range(4):
            run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Look-back signal", ctx)
        self.assertIn("Nearest recorded decision by search on this prompt's words -- may be unrelated:", ctx)
        self.assertLess(ctx.index("Look-back signal"), ctx.index("Nearest recorded decision"))
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=lookback-injected", log_text)
        self.assertNotIn("outcome=prompt-query-only", log_text)

    def test_exit3_coverage_candidate_falls_through_to_lookback_no_double_mention(self):
        """Fix round 2 (TOP-0133 L2, Grok NIT): a turn that IS a coverage
        candidate (ledger grew, cooldown open) but whose classification
        finds no evidence (`raise SystemExit(3)` in the coverage python
        block, hooks/userprompt-remind.sh's own OUTPUT_JSON transform)
        prints no coverage envelope -- OUT_RC=3, OUTPUT_JSON empty -- and
        falls straight through to the SAME turn's own look-back check.
        Before fix round 1's append-after-delivery reordering, that
        look-back block's "search surfaced ... for prompt" listing (read
        from search_fallbacks BEFORE this turn's own append) and the
        guess itself (merged in fresh off PQ_TEXT) could land in the SAME
        envelope once an exit-3 candidate coincided with a look-back-
        eligible turn -- the double-mention Grok's own fixture measured.
        Ledger seeded with the ALREADY-mapped file (src/mapped.py,
        covered by TOP-9001) makes candidate=1 (pairs non-empty, grew,
        cooldown open) but has_evidence=False (coverage_status ok,
        unmapped=[], neither HEAD moved) -- the exit-3 shape -- while
        patch_state jumps straight to turn 5 so the SAME turn is also
        look-back eligible."""
        session_id = "s-pq-exit3-lookback"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "mapped.py"), "code")])
        # Baseline start_code_sha/start_store_sha directly to the CURRENT
        # code/store HEADs -- with no baseline at all, both read as
        # "changed" (code_changed/store_changed true), which alone would
        # satisfy has_evidence and this turn would never reach the exit-3
        # shape this test needs. A real SessionStart call would set this
        # same baseline, but it would ALSO run the standing digest --
        # WIDGET_STANDING_TOPIC carries `standing: [L1]`, so that digest
        # would deliver TOP-8001 itself and exclude it from this turn's
        # own prompt-query search (Addendum B), picking TOP-8002 instead
        # and defeating this test's fixed expectations below.
        self.patch_state(
            session_id,
            start_code_sha=git_head(self.code_root),
            start_store_sha=git_head(self.store_root),
            user_turn_count=4,
        )
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # exactly one JSON document on stdout
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        out = json.loads(lines[0])
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Look-back signal", ctx)
        self.assertNotIn("Coverage signal", ctx, "an exit-3 candidate must never also print a coverage envelope")
        self.assertIn("Nearest recorded decision by search on this prompt's words -- may be unrelated:", ctx)
        # the guess text present exactly once ...
        self.assertEqual(ctx.count("TOP-8001"), 1, ctx)
        # ... and no "search surfaced ... for prompt" line for THIS turn's
        # own fresh hit in the SAME document (the append that would add
        # it to search_fallbacks runs strictly after this printf).
        self.assertNotIn("search surfaced", ctx, ctx)

        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=lookback-injected", log_text)

        state = self.load_state(session_id)
        fallbacks = state.get("search_fallbacks") or []
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0].get("id"), "TOP-8001")

        # the NEXT look-back turn (channel off, no fresh search needed)
        # DOES list it -- the deferred append already landed by now, and
        # this later read sees it.
        self.patch_state(session_id, user_turn_count=9)
        proc2, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertIn("Look-back signal", proc2.stdout)
        self.assertIn(
            "search surfaced Widget caching policy (TOP-8001) for prompt",
            proc2.stdout,
        )

    def test_silent_turn_emits_standalone_and_delivery_open_ends_false(self):
        """A turn with neither coverage nor look-back evidence would
        otherwise print nothing at all -- the guess goes out standalone,
        under its own outcome token (Addendum C), and delivery_open still
        ends false (Addendum C: closes it, stamps nothing else)."""
        session_id = "s-pq-standalone"
        self.seed_ledger(session_id, [])
        state_before = self.load_state(session_id)
        # A real prompt_id opens delivery_open this turn (mirrors the
        # existing dual-gate machinery) so the "ends false" half of this
        # assertion is actually exercising something.
        payload = self.user_prompt_payload(
            session_id, prompt_text=self.MATCH_PROMPT, prompt_id="pq-standalone-1",
        )
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Nearest recorded decision by search on this prompt's words -- may be unrelated:", ctx)
        self.assertNotIn("Coverage signal", ctx)
        self.assertNotIn("Look-back signal", ctx)

        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=prompt-query-only", log_text)
        self.assertNotIn("outcome=no-evidence", log_text)

        state = self.load_state(session_id)
        self.assertFalse(state.get("delivery_open"), "a standalone guess must still close delivery_open")
        # Fix round 1b: the append now runs AFTER the close write above (in
        # finish() itself, guarded by PQ_EMITTED) rather than before it --
        # the observable end state is unchanged: the entry is still there.
        fallbacks = state.get("search_fallbacks") or []
        self.assertEqual(len(fallbacks), 1, "the delivered guess must still be appended to search_fallbacks")
        self.assertEqual(fallbacks[0].get("id"), "TOP-8001")
        # Addendum C: stamps NOTHING else -- every one of these fields
        # stays exactly what it was BEFORE this turn ran (seed_ledger's
        # own defaults, e.g. last_inject_time=0), never advanced by the
        # standalone emission.
        for key in ("last_inject_turn", "last_inject_time", "last_inject_ts", "lookback_count"):
            self.assertEqual(state.get(key), state_before.get(key), f"{key} must be untouched by a standalone guess")

    # -- subagent / duplicate-delivery gates run first ------------------------

    def test_subagent_payload_stays_off_no_line(self):
        session_id = "s-pq-subagent"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, agent_id="agent-1", prompt_text=self.MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("prompt-query", log_text)
        self.assertIn("outcome=agent-source", log_text)

    def test_duplicate_delivery_never_searches_twice(self):
        session_id = "s-pq-dup"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT, prompt_id="pq-dup-1")
        proc1, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        proc2, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc2.stdout.strip(), "")
        log_text = (self.home / "hook.log").read_text()
        pq_lines = [l for l in log_text.splitlines() if " outcome=prompt-query " in l or l.rstrip().endswith("outcome=prompt-query")]
        self.assertEqual(len(pq_lines), 1, log_text)
        self.assertIn("outcome=duplicate-delivery", log_text)

    def test_resubmit_after_completed_run_is_plain_duplicate_one_entry(self):
        """Fix round 1b (TOP-0133 L2): a kill landing in the window between
        the write that closes delivery_open and the search_fallbacks
        append (the residual defect this round fixes) can no longer be
        observed from outside the process -- the append now runs strictly
        after that close, inside finish(), so the moment a run's own
        process has exited at all, delivery_open is already false and any
        redelivery of the SAME prompt_id reads as a plain duplicate, not a
        retry. Exercised end-to-end: run once for real (a genuine
        completed turn, no simulated rewind), resubmit the identical
        prompt_id, and confirm the second run neither re-searches (no new
        `outcome=prompt-query` line, no stdout) nor double-appends (still
        exactly one search_fallbacks entry)."""
        session_id = "s-pq-dup-completed"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT, prompt_id="pq-dup-completed-1")

        proc1, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        state1 = self.load_state(session_id)
        self.assertFalse(state1.get("delivery_open"), "a completed run must leave delivery_open false")
        self.assertEqual(len(state1.get("search_fallbacks") or []), 1)

        proc2, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc2.stdout.strip(), "", "a duplicate delivery must print nothing")

        log_text = (self.home / "hook.log").read_text()
        pq_lines = [
            l for l in log_text.splitlines()
            if " outcome=prompt-query " in l or l.rstrip().endswith("outcome=prompt-query")
        ]
        self.assertEqual(len(pq_lines), 1, "a duplicate must never re-search: still exactly one prompt-query line")
        self.assertIn("outcome=duplicate-delivery", log_text)

        state2 = self.load_state(session_id)
        self.assertFalse(state2.get("delivery_open"))
        self.assertEqual(len(state2.get("search_fallbacks") or []), 1, "one entry, not two")

    def test_append_transform_itself_closes_delivery_open_fix_round_2(self):
        """Fix round 2 (TOP-0133 L2, Grok NIT): pq_mark_delivered's own
        search_fallbacks append (mc_fallback_write_state's CLOSE_DELIVERY
        arg) now sets delivery_open=False itself, inside the SAME locked
        write, rather than relying solely on a separate close write that
        ran a moment earlier -- a lock-wait timeout or a skipped close on
        some future outcome branch could otherwise leave that separate
        write undone while the append still ran, leaving delivery_open
        true with the hit already excluded (a redelivery would then
        misread as a retry and take a SECOND topic). Forcing that
        specific separate-close-failed shape is not exercisable from
        outside the process; this instead pins the postcondition the fix
        guarantees end-to-end. NOTE (fix round 3, Codex final26 review):
        a standalone guess is NOT the one outcome with no separate close
        write of its own -- finish() DOES perform one, for any outcome
        other than "injected"/"lookback-injected" (a standalone guess's
        own outcome, "prompt-query-only", is neither), strictly before
        calling pq_mark_delivered. The CLOSE_DELIVERY arg is a SECOND,
        REDUNDANT close covering that first one failing (a lock timeout,
        a skipped branch), not the only close on this path -- this test
        still ends up asserting the right thing (delivery_open false
        after a standalone guess) because BOTH the ordinary close and the
        redundant one succeed in the common case this test exercises; it
        is not, by itself, proof the redundant close is what did the
        work."""
        session_id = "s-pq-append-closes"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(
            session_id, prompt_text=self.MATCH_PROMPT, prompt_id="pq-append-closes-1",
        )
        proc1, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertIn("outcome=prompt-query-only", (self.home / "hook.log").read_text())
        state1 = self.load_state(session_id)
        self.assertFalse(state1.get("delivery_open"), "the append's own transform must leave delivery_open false")
        self.assertEqual(len(state1.get("search_fallbacks") or []), 1)

        proc2, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc2.stdout.strip(), "", "a duplicate delivery must print nothing")
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=duplicate-delivery", log_text)
        state2 = self.load_state(session_id)
        self.assertFalse(state2.get("delivery_open"))
        self.assertEqual(len(state2.get("search_fallbacks") or []), 1, "one entry, not two")

    # -- the prompt-text invariant --------------------------------------------

    def test_prompt_text_never_leaks_only_individual_terms_do(self):
        """A distinctive 6-word sentence that matches nothing in the store
        (NO_MATCH_PROMPT): no 3-word-or-longer substring of it appears
        anywhere in hook.log, the state file, or stdout -- terms show up
        only individually, space-replaced-by-'+' in `q=`. The terms are
        also never written to state (only ids/titles are, and only on a
        hit -- this is a no-hit turn, so search_fallbacks stays empty)."""
        session_id = "s-pq-invariant"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.NO_MATCH_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        log_text = (self.home / "hook.log").read_text()
        state_text = json.dumps(self.load_state(session_id))
        words = self.NO_MATCH_PROMPT.split()
        for i in range(len(words) - 2):
            substring = " ".join(words[i:i + 3])
            self.assertNotIn(substring, log_text)
            self.assertNotIn(substring, state_text)
            self.assertNotIn(substring, proc.stdout)
        # Individual terms DO appear, +-joined, in q=.
        self.assertIn("banana+pineapple+coconut", log_text)
        state = self.load_state(session_id)
        self.assertNotIn("search_fallbacks", state)

    # -- latency ---------------------------------------------------------------

    def test_latency_under_one_second_on_hit_turn(self):
        """Same generous bound test_latency_under_one_second_with_twenty_
        path_ledger_poisoned_pythonpath uses -- see the final report
        message for the actual measured number on this machine."""
        session_id = "s-pq-latency"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT)
        proc, elapsed = run_script(USERPROMPT_HOOK, payload, self.pq_env(), timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Nearest recorded decision", proc.stdout)
        self.assertLess(elapsed, 1.0, f"userprompt hook (prompt-query ON, hit) took {elapsed:.3f}s")

    # -- fix round 1 (Grok) --------------------------------------------------

    def test_two_char_tokens_are_dropped_too_few_terms(self):
        """Fix round 1 (MINOR, Grok): a wrong implementation that keeps
        2-character tokens would find 4 terms here ("ok", "go", "fix",
        "bug") and search; the real gate drops tokens under 3 characters
        (and, since fix round 1, filler/stopwords too) -- only "fix" and
        "bug" survive ("ok"/"go" are 2 characters, "the"/"now" are
        stopwords, "please" is filler) -- too-few-terms."""
        session_id = "s-pq-two-char-tokens"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text="ok go fix the bug now please")
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text, "prompt-query-empty")
        self.assertIn("outcome=prompt-query-empty reason=too-few-terms", line)

    def test_prompt_terms_field_requested_only_when_on(self):
        """Fix round 1 (MINOR, Grok): a wrong implementation that always
        requests "_prompt_terms" from mc_extract_fields (rather than only
        when PQ_ON=1) would defeat the OFF-project "the prompt is never
        read" proof even though no test asserting on OUTPUT would catch
        it -- test_never_reads_prompt_key_either only flags a `.get(...)`
        dereference inside userprompt-remind.sh's OWN code, not the field
        list it hands to mc_extract_fields. Proven via a real seam
        (MC_MEMLIB_PATH, the same seam test_outer_deadline_covers_
        memlib_sourcing above uses): a shim sources the real memlib.sh,
        then wraps mc_extract_fields to log its FIELD NAMES ONLY (argv
        from position 2 on -- never argv[1], the raw payload) to a file
        before delegating to the real implementation."""
        argv_log = Path(self.td) / "extract-argv.log"
        shim = Path(self.td) / "shim-memlib.sh"
        shim.write_text(
            "source " + shlex.quote(str(MEMLIB)) + "\n"
            "eval \"$(declare -f mc_extract_fields | "
            "sed '1s/mc_extract_fields/_orig_mc_extract_fields/')\"\n"
            "mc_extract_fields() {\n"
            "    local _payload=\"$1\"\n"
            "    shift\n"
            "    printf '%s\\n' \"$*\" >> " + shlex.quote(str(argv_log)) + "\n"
            "    _orig_mc_extract_fields \"$_payload\" \"$@\"\n"
            "}\n"
        )

        session_id_off = "s-pq-fieldlist-off"
        self.seed_ledger(session_id_off, [])
        proc_off, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id_off, prompt_text=self.MATCH_PROMPT),
            self.base_env(MC_MEMLIB_PATH=str(shim)),
        )
        self.assertEqual(proc_off.returncode, 0, proc_off.stderr)
        off_log = argv_log.read_text() if argv_log.exists() else ""
        self.assertEqual(off_log.count("_prompt_terms"), 0, off_log)

        if argv_log.exists():
            argv_log.unlink()

        session_id_on = "s-pq-fieldlist-on"
        self.seed_ledger(session_id_on, [])
        proc_on, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id_on, prompt_text=self.MATCH_PROMPT),
            self.pq_env(MC_MEMLIB_PATH=str(shim)),
        )
        self.assertEqual(proc_on.returncode, 0, proc_on.stderr)
        on_log = argv_log.read_text() if argv_log.exists() else ""
        self.assertEqual(on_log.count("_prompt_terms"), 1, on_log)

    def test_retry_after_kill_before_stdout_delivers_once_not_twice(self):
        """Fix round 3 (MAJOR, Codex final26): a RETRY turn (delivery_open
        still true because a kill landed before either close write could
        run) now skips the prompt-query search ENTIRELY instead of re-
        running it -- RETRY is exported from the one-shot decision reader
        and checked before anything else inside `if [ "$PQ_ON" = "1" ]`
        (userprompt-remind.sh). This is a deliberate contract change from
        fix round 1: a retry used to re-search and redeliver the SAME
        guess (safe only because nothing had been excluded yet); Codex
        showed that guarantee breaks the moment an intervening write
        excludes the original hit before the redelivery, since re-
        searching then surfaces a DIFFERENT topic for the same prompt.
        Skipping the search on every retry makes "at most one topic per
        prompt" hold by construction instead: the first delivery either
        reached stdout (one topic) or it did not (zero) -- a retry never
        selects, so it can never select a second, different one either.
        Simulated by running once for real, then rewinding state to
        exactly the kill-before-close shape (undo this run's own append,
        reopen delivery_open) before resubmitting the identical
        prompt_id."""
        session_id = "s-pq-retry"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT, prompt_id="pq-retry-1")
        proc1, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertIn("TOP-8001", proc1.stdout)
        state1 = self.load_state(session_id)
        self.assertFalse(state1.get("delivery_open"))
        self.assertEqual(len(state1.get("search_fallbacks") or []), 1)
        turn_after_first = state1.get("user_turn_count")

        state_file = self.state_file(session_id)
        state = json.loads(state_file.read_text())
        state.pop("search_fallbacks", None)
        state["delivery_open"] = True
        state_file.write_text(json.dumps(state))

        proc2, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertNotIn("TOP-8001", proc2.stdout, "a retry must never re-select any topic, not even the original")
        self.assertNotIn("TOP-8002", proc2.stdout, "a retry must never select a second, different topic")

        state2 = self.load_state(session_id)
        self.assertFalse(state2.get("delivery_open"))
        self.assertEqual(
            len(state2.get("search_fallbacks") or []), 0,
            "a retry runs no search at all -- nothing new is ever appended",
        )
        self.assertEqual(
            state2.get("user_turn_count"), turn_after_first,
            "a retry must not double-advance the turn counter",
        )

        log_text = (self.home / "hook.log").read_text()
        pq_lines = [
            l for l in log_text.splitlines()
            if " outcome=prompt-query " in l or l.rstrip().endswith("outcome=prompt-query")
        ]
        self.assertEqual(len(pq_lines), 1, "a retry must never search: still exactly one real hit line, from run 1")
        self.assertIn("outcome=prompt-query-empty reason=retry", log_text)

    def test_codex_finding_retry_never_yields_second_topic_even_with_intervening_fallback(self):
        """Codex's own reproduction (final26 review, MAJOR): a kill after
        the guess already reached stdout but before EITHER close write
        (finish()'s own, or pq_mark_delivered's) lands leaves
        delivery_open true with NOTHING written to search_fallbacks for
        that hit. If, in that same window, an UNRELATED L1 fallback hook
        (pre-edit-chain.sh/newfile-nudge.sh's own shape) independently
        appends the SAME id to search_fallbacks for a different file --
        a real, legitimate event this fix must not depend on the absence
        of -- the redelivered prompt_id must still read as a RETRY and
        select nothing new: never TOP-8002, regardless of what
        search_fallbacks already holds by the time it runs."""
        session_id = "s-pq-retry-codex"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.MATCH_PROMPT, prompt_id="pq-retry-codex-1")
        proc1, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertIn("TOP-8001", proc1.stdout)

        # Simulate: both the normal close and pq_mark_delivered's own
        # append were lost to a kill -- delivery_open reopened, this
        # run's own search_fallbacks entry undone.
        state_file = self.state_file(session_id)
        state = json.loads(state_file.read_text())
        state["delivery_open"] = True
        # An unrelated L1 hook independently surfaces the SAME id for a
        # DIFFERENT file, in the SAME session, moments later -- a real
        # event, not this run's own (undone) append.
        state["search_fallbacks"] = [{
            "file": "somewhere/else/unrelated1.py",
            "id": "TOP-8001",
            "title": "Widget caching policy",
            "hook": "pre-edit",
        }]
        state_file.write_text(json.dumps(state))

        proc2, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertNotIn("TOP-8001", proc2.stdout)
        self.assertNotIn("TOP-8002", proc2.stdout, "must never surface a second topic for the same prompt")

        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=prompt-query-empty reason=retry", log_text)

        state2 = self.load_state(session_id)
        self.assertFalse(state2.get("delivery_open"))
        self.assertEqual(
            len(state2.get("search_fallbacks") or []), 1,
            "the intervening L1 entry is left exactly as it was -- a retry adds nothing",
        )

    def test_grep_qxf_project_name_with_dot_is_literal_not_regex(self):
        """Fix round 1 (MINOR, Grok): grep -qx treats $MC_PROJECT as a
        REGEX matched against each line of the projects file -- a project
        name containing a metacharacter (a bare ".") then matches a
        DIFFERENT line ("." matches any single character). project =
        "hook.test", file line "hook-test": under plain -qx this reads as
        a match (WRONG -- a dash-substituted line must never enable a
        different project); -qxF requires a literal match, so this must
        stay OFF. The positive control (the file's own literal name)
        proves ON still works with -F."""
        original_project = self.project
        self.addCleanup(setattr, self, "project", original_project)
        self.project = "hook.test"

        (self.home / "prompt-query.projects").write_text("hook-test\n")
        session_id_off = "s-pq-dot-project-off"
        self.seed_ledger(session_id_off, [])
        payload_off = self.user_prompt_payload(session_id_off, prompt_text=self.MATCH_PROMPT)
        proc_off, _ = run_script(USERPROMPT_HOOK, payload_off, self.base_env())
        self.assertEqual(proc_off.returncode, 0, proc_off.stderr)
        log_off = (self.home / "hook.log").read_text() if (self.home / "hook.log").exists() else ""
        self.assertNotIn("prompt-query", log_off)

        (self.home / "prompt-query.projects").write_text("hook.test\n")
        session_id_on = "s-pq-dot-project-on"
        self.seed_ledger(session_id_on, [])
        payload_on = self.user_prompt_payload(session_id_on, prompt_text=self.MATCH_PROMPT)
        proc_on, _ = run_script(USERPROMPT_HOOK, payload_on, self.base_env())
        self.assertEqual(proc_on.returncode, 0, proc_on.stderr)
        log_on = (self.home / "hook.log").read_text()
        self.assertIn("prompt-query", log_on)

    # -- machine-delivered messages never search (TOP-0133 L3) ---------------
    #
    # Claude Code delivers subagent hand-back reports, background-task
    # notifications, and cross-session messages through this SAME
    # UserPromptSubmit event a typed prompt arrives on. hooks/memlib.sh
    # classifies the prompt text BEFORE tokenizing it (mc_text.py's
    # `_MACHINE_PROMPT_PREFIXES`) and userprompt-remind.sh's prompt-query
    # block skips the search entirely on a machine-framed turn -- no search
    # subprocess (no `pq_ms=`), no guess on stdout, no `search_fallbacks`
    # entry, logged as `reason=non-user q=`. Every fixture below embeds
    # MATCH_PROMPT (which WOULD hit TOP-8001 if tokenized) so a wrongly-
    # searching implementation would fail loudly, not silently.

    TASK_NOTIFICATION_PROMPT = (
        "<task-notification>\n<task-id>abc</task-id>\n" + MATCH_PROMPT
    )
    AGENT_MESSAGE_PROMPT = (
        "Another Claude session sent a message:\n"
        '<agent-message from="a1">\n[Subagent hand-back] ' + MATCH_PROMPT
    )
    CROSS_SESSION_PROMPT = (
        '<cross-session-message from="x">\n' + MATCH_PROMPT
    )

    def _assert_non_user_no_search(self, session_id, prompt_text):
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=prompt_text)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text, "prompt-query-empty")
        self.assertIn("outcome=prompt-query-empty reason=non-user q=", line)
        self.assertNotIn("pq_ms=", line, "a non-user turn must never run the search subprocess")
        self.assertNotIn("TOP-8001", log_text)
        self.assertNotIn("TOP-8001", proc.stdout)
        state = self.load_state(session_id)
        self.assertNotIn("search_fallbacks", state)

    def test_task_notification_prefix_never_searches(self):
        self._assert_non_user_no_search("s-pq-nonuser-tasknotif", self.TASK_NOTIFICATION_PROMPT)

    def test_agent_message_hand_back_prefix_never_searches(self):
        self._assert_non_user_no_search("s-pq-nonuser-agentmsg", self.AGENT_MESSAGE_PROMPT)

    def test_cross_session_message_prefix_never_searches(self):
        self._assert_non_user_no_search("s-pq-nonuser-crosssess", self.CROSS_SESSION_PROMPT)

    AGENT_MESSAGE_PROMPT_NO_PREAMBLE = (
        '<agent-message from="a1">\n' + MATCH_PROMPT
    )

    def test_agent_message_prefix_without_preamble_never_searches(self):
        """`<agent-message` is matched independently of any English
        preamble -- it is its own entry in the tuple, not just a substring
        the "Another Claude session sent a message" sentence happens to
        contain -- so a prompt that opens directly with the tag (no
        preamble at all) must still be recognised. Deleting
        `<agent-message` from the tuple fails this test."""
        self._assert_non_user_no_search(
            "s-pq-nonuser-agentmsg-bare", self.AGENT_MESSAGE_PROMPT_NO_PREAMBLE
        )

    # -- new framing markers (TOP-0133 L3, widened) --------------------------

    NEW_MACHINE_PREFIX_PROMPTS = {
        "peer-working-colon": (
            "Another Claude session sent a message while you were working:\n"
            + MATCH_PROMPT
        ),
        "peer-session-working-colon": (
            "A peer session sent a message while you were working:\n"
            + MATCH_PROMPT
        ),
        "system-reminder": "<system-reminder>\n" + MATCH_PROMPT,
        "system-notification": "[SYSTEM NOTIFICATION - NOT USER INPUT]\n" + MATCH_PROMPT,
    }

    def test_widened_machine_framing_prefixes_never_search(self):
        for label, prompt_text in self.NEW_MACHINE_PREFIX_PROMPTS.items():
            with self.subTest(marker=label):
                self._assert_non_user_no_search(f"s-pq-nonuser-{label}", prompt_text)

    def test_leading_whitespace_before_marker_still_recognised(self):
        """The classification checks `text.lstrip().startswith(...)` --
        leading whitespace/newlines (as a JSON string can legitimately
        carry) before the marker must not defeat the match."""
        self._assert_non_user_no_search(
            "s-pq-nonuser-leading-ws",
            "\n\n   " + self.TASK_NOTIFICATION_PROMPT,
        )

    def test_mid_sentence_mention_of_marker_text_still_searches(self):
        """A human prompt that merely MENTIONS the literal marker tag
        `<task-notification>` mid-sentence is a prefix miss (the marker
        text is not at the very start) -- it must still search normally,
        same as any other human prompt, and hit TOP-8001 on MATCH_PROMPT's
        own words. The fixture embeds the marker WITH its angle brackets so
        a substring-based implementation (`"<task-notification>" in text`
        instead of a prefix check) fails this test loudly."""
        session_id = "s-pq-nonuser-midsentence"
        self.seed_ledger(session_id, [])
        prompt_text = (
            self.MATCH_PROMPT
            + " -- similar to that <task-notification> and <agent-message> issue from before"
        )
        payload = self.user_prompt_payload(session_id, prompt_text=prompt_text)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        line = self._last_pq_line(log_text)
        self.assertIn("outcome=prompt-query ", line)
        self.assertIn("ids=TOP-8001", line)
        self.assertNotIn("reason=non-user", log_text)

    def test_channel_off_machine_framed_prompt_produces_no_line_at_all(self):
        """Channel OFF: a machine-framed prompt must behave exactly like
        the plain "off by default" case -- no prompt-query line of any
        kind, since the whole `_prompt_terms`/PROMPT_SOURCE derivation is
        never requested at all when PQ_ON != 1."""
        session_id = "s-pq-nonuser-off"
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=self.TASK_NOTIFICATION_PROMPT)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("prompt-query", log_text)

    def test_prompt_text_invariant_holds_for_machine_framed_prompt(self):
        """Same invariant as test_prompt_text_never_leaks_only_individual_
        terms_do above, for a machine-framed prompt: since it is never
        tokenized at all, no 3-word-or-longer substring of it may appear
        anywhere in hook.log, state, or stdout."""
        session_id = "s-pq-nonuser-invariant"
        prompt_text = (
            "<task-notification>\n<task-id>xyz789</task-id>\n"
            + self.NO_MATCH_PROMPT
        )
        self.seed_ledger(session_id, [])
        payload = self.user_prompt_payload(session_id, prompt_text=prompt_text)
        proc, _ = run_script(USERPROMPT_HOOK, payload, self.pq_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

        log_text = (self.home / "hook.log").read_text()
        state_text = json.dumps(self.load_state(session_id))
        words = prompt_text.split()
        for i in range(len(words) - 2):
            substring = " ".join(words[i:i + 3])
            self.assertNotIn(substring, log_text)
            self.assertNotIn(substring, state_text)
            self.assertNotIn(substring, proc.stdout)
        line = self._last_pq_line(log_text, "prompt-query-empty")
        self.assertIn("outcome=prompt-query-empty reason=non-user q=", line)


class TestMcFallbackParseExcludeIdsMaxHits(unittest.TestCase):
    """hooks/mc-fallback-lib.sh's mc_fallback_parse (TOP-0133 L2): the two
    new optional args (EXCLUDE_IDS, MAX_HITS) the prompt-query channel
    needs, exercised by sourcing the library directly and calling the
    function -- same in-process-shell approach TestQueryLib
    (tests/test_hooks.py) already uses for mc-query-lib.sh."""

    FB_LIB = HOOKS_DIR / "mc-fallback-lib.sh"
    TWO_HIT_JSON = json.dumps({
        "results": [
            {"id": "TOP-1", "title": "one", "chain_text": "chain one"},
            {"id": "TOP-2", "title": "two", "chain_text": "chain two"},
        ]
    })

    def _parse(self, raw_json, exclude_ids="", max_hits="", second_floor=None):
        script = (
            f'source {shlex.quote(str(self.FB_LIB))}; '
            f'mc_fallback_parse {shlex.quote(VENV_PYTHON)} 0 LABEL 1 '
            f'{shlex.quote(raw_json)} {shlex.quote(exclude_ids)} {shlex.quote(max_hits)}'
            + (f' {shlex.quote(second_floor)}' if second_floor is not None else '')
            + '; echo "HITS=$FB_HITS"; echo "IDS=$FB_IDS"; echo "REASON=$FB_REASON"; '
            'echo "DROPPED=$FB_FLOOR_DROPPED"; echo "STATE=$FB_HITS_FOR_STATE"'
        )
        proc = subprocess.run([MC_BASH, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = {}
        for line in proc.stdout.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                out[k] = v
        return out

    def test_five_arg_form_unchanged(self):
        """No EXCLUDE_IDS/MAX_HITS args at all -- byte-identical to the
        pre-L2 behavior: both hits survive, in order."""
        script = (
            f'source {shlex.quote(str(self.FB_LIB))}; '
            f'mc_fallback_parse {shlex.quote(VENV_PYTHON)} 0 LABEL 1 {shlex.quote(self.TWO_HIT_JSON)}; '
            'echo "HITS=$FB_HITS"; echo "IDS=$FB_IDS"; echo "REASON=$FB_REASON"'
        )
        proc = subprocess.run([MC_BASH, "-c", script], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("HITS=2", proc.stdout)
        self.assertIn("IDS=TOP-1,TOP-2", proc.stdout)
        self.assertIn("REASON=", proc.stdout)

    def test_exclude_one_id_leaves_the_other(self):
        out = self._parse(self.TWO_HIT_JSON, exclude_ids="TOP-1")
        self.assertEqual(out["HITS"], "1")
        self.assertEqual(out["IDS"], "TOP-2")
        self.assertEqual(out["REASON"], "")

    def test_exclude_both_ids_yields_already_surfaced(self):
        out = self._parse(self.TWO_HIT_JSON, exclude_ids="TOP-1,TOP-2")
        self.assertEqual(out["HITS"], "0")
        self.assertEqual(out["REASON"], "already-surfaced")

    def test_max_hits_one_stops_after_first_surviving_hit(self):
        out = self._parse(self.TWO_HIT_JSON, max_hits="1")
        self.assertEqual(out["HITS"], "1")
        self.assertEqual(out["IDS"], "TOP-1")

    # --- TOP-0133 L5: second-hit RRF score floor -------------------------
    FLOOR = "0.0314"

    @staticmethod
    def _env_json(scores, **envelope):
        """A search --json envelope: one hit per score (None = no score key)."""
        results = []
        for i, sc in enumerate(scores, 1):
            h = {"id": f"TOP-{i}", "title": f"t{i}", "chain_text": f"chain {i}"}
            if sc is not None:
                h["score"] = sc
            results.append(h)
        return json.dumps({"results": results, **envelope})

    def test_floor_drops_a_below_floor_second_hit_and_names_it(self):
        out = self._parse(self._env_json([0.0328, 0.0200], fusion="rrf"), second_floor=self.FLOOR)
        self.assertEqual(out["HITS"], "1")
        self.assertEqual(out["IDS"], "TOP-1")
        self.assertEqual(out["DROPPED"], "TOP-2")
        self.assertNotIn("TOP-2", out["STATE"])

    def test_floor_keeps_a_second_hit_at_or_above_the_floor(self):
        for sc in (0.0314, 0.0328):
            out = self._parse(self._env_json([0.0328, sc], fusion="rrf"), second_floor=self.FLOOR)
            self.assertEqual(out["HITS"], "2", sc)
            self.assertEqual(out["IDS"], "TOP-1,TOP-2", sc)
            self.assertEqual(out["DROPPED"], "", sc)

    def test_floor_never_drops_the_first_hit(self):
        out = self._parse(self._env_json([0.001, 0.0005], fusion="rrf"), second_floor=self.FLOOR)
        self.assertEqual(out["IDS"], "TOP-1")
        self.assertEqual(out["DROPPED"], "TOP-2")

    def test_floor_missing_or_non_numeric_second_score_counts_as_below(self):
        for sc in (None, "0.9", True):
            out = self._parse(self._env_json([0.0328, sc], fusion="rrf"), second_floor=self.FLOOR)
            self.assertEqual(out["IDS"], "TOP-1", sc)
            self.assertEqual(out["DROPPED"], "TOP-2", sc)

    def test_floor_applies_to_the_second_surviving_hit_after_exclusion(self):
        """TOP-1 excluded: TOP-2 is the FIRST survivor (always shown),
        TOP-3 the second (judged against the floor)."""
        raw = self._env_json([0.0328, 0.0100, 0.0100], fusion="rrf")
        out = self._parse(raw, exclude_ids="TOP-1", second_floor=self.FLOOR)
        self.assertEqual(out["IDS"], "TOP-2")
        self.assertEqual(out["DROPPED"], "TOP-3")

    def test_floor_ignored_unless_the_envelope_proves_rrf(self):
        low = [0.2, 0.1]
        cases = {
            "vector-only": self._env_json(low, fusion="vector-only"),
            "no fusion key": self._env_json(low),
            "bare list (fts/vector mode)": json.dumps(json.loads(self._env_json(low))["results"]),
            "rrf but embeddings unavailable": self._env_json(low, fusion="rrf", embedding="unavailable"),
            "rrf with dimension mismatch": self._env_json(low, fusion="rrf", dimension_mismatch_rows=3),
        }
        for name, raw in cases.items():
            out = self._parse(raw, second_floor=self.FLOOR)
            self.assertEqual(out["HITS"], "2", name)
            self.assertEqual(out["DROPPED"], "", name)

    def test_no_floor_arg_empty_or_zero_is_unchanged(self):
        raw = self._env_json([0.0328, 0.0001], fusion="rrf")
        for floor in (None, "", "0"):
            out = self._parse(raw, second_floor=floor)
            self.assertEqual(out["IDS"], "TOP-1,TOP-2", floor)
            self.assertEqual(out["DROPPED"], "", floor)

    def test_floor_with_max_hits_one_never_considers_a_second_hit(self):
        """The prompt channel passes MAX_HITS=1 and no floor; even with one
        the second hit is never reached."""
        raw = self._env_json([0.0328, 0.0001], fusion="rrf")
        out = self._parse(raw, max_hits="1", second_floor=self.FLOOR)
        self.assertEqual(out["IDS"], "TOP-1")
        self.assertEqual(out["DROPPED"], "")

    def _floor_value(self, env_value):
        env = dict(os.environ)
        env.pop("MEMCONTINUUM_FALLBACK_SECOND_FLOOR", None)
        if env_value is not None:
            env["MEMCONTINUUM_FALLBACK_SECOND_FLOOR"] = env_value
        proc = subprocess.run(
            [MC_BASH, "-c", f'source {shlex.quote(str(self.FB_LIB))}; mc_fallback_second_floor'],
            capture_output=True, text=True, timeout=10, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_second_floor_env_default_override_disable_and_garbage(self):
        self.assertEqual(self._floor_value(None), "0.0314")
        self.assertEqual(self._floor_value(""), "0.0314")
        self.assertEqual(self._floor_value("0.02"), "0.02")
        self.assertEqual(self._floor_value(".5"), ".5")
        self.assertEqual(self._floor_value("0"), "0")
        for bad in ("abc", "-0.01", "0.03x", "1e-2", "0.0.3", " 0.03"):
            self.assertEqual(self._floor_value(bad), "0.0314", bad)


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestMcFallbackWriteStateCloseDelivery(unittest.TestCase):
    """Direct library test (fix round 3, TOP-0133 L2, Codex final26 MINOR):
    mc_fallback_write_state's 6th arg (CLOSE_DELIVERY) proven in isolation
    against a pre-seeded state file, rather than only end to end through a
    full hook run. Seeds delivery_open=true, calls the function with the
    6th arg "1" and confirms it flips to false in the SAME locked write; a
    control call omitting the 6th arg (the 5-arg form) proves delivery_open
    is left untouched otherwise -- the two together are what the library
    comment above the function now claims (a REDUNDANT close, not the only
    one), not a no-op."""

    FB_LIB = HOOKS_DIR / "mc-fallback-lib.sh"

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-fbwrite-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

    def _seed_state(self, name: str) -> Path:
        state_file = Path(self.td) / f"{name}.json"
        state_file.write_text(json.dumps({"delivery_open": True, "user_turn_count": 3}))
        return state_file

    def _call(self, state_file: Path, close_delivery_arg: str) -> subprocess.CompletedProcess:
        script = (
            f'source {shlex.quote(str(MEMLIB))}\n'
            f'source {shlex.quote(str(self.FB_LIB))}\n'
            f'mc_fallback_write_state {shlex.quote(str(state_file))} "prompt" "[]" "userprompt" 2.0'
            + (f" {close_delivery_arg}" if close_delivery_arg is not None else "")
            + "\n"
            'echo "RC=$?"\n'
        )
        env = clean_env(MEMCONTINUUM_HOME=self.td, MEMCONTINUUM_PYTHON=VENV_PYTHON)
        return subprocess.run([MC_BASH, "-c", script], capture_output=True, text=True, env=env, timeout=10)

    def test_sixth_arg_one_closes_delivery_open(self):
        state_file = self._seed_state("close")
        proc = self._call(state_file, '"1"')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)
        state = json.loads(state_file.read_text())
        self.assertFalse(state.get("delivery_open"), "CLOSE_DELIVERY=1 must set delivery_open false")

    def test_five_arg_form_leaves_delivery_open_untouched(self):
        state_file = self._seed_state("control")
        proc = self._call(state_file, None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout)
        state = json.loads(state_file.read_text())
        self.assertTrue(state.get("delivery_open"), "omitting the 6th arg must never touch delivery_open")


# ---------------------------------------------------------------------------
# 5c. userprompt-remind.sh -- the commit nudge (TOP-0122 L1 rule 2a)
# ---------------------------------------------------------------------------


class TestUserPromptCommitNudge(HookTestBase):
    """Commit messages name the decision (TOP-0122 L1 rule 2a; rulings 140,
    144, DESIGN-a2a3.md A2-3): when a code root's HEAD moves since the last
    prompt (state `last_seen_heads`, distinct from the per-SESSION
    `start_code_shas`) and the new commit names no TOP-xxxx id, and the
    session's own `unmapped` call already finds at least one edited file
    under that root with no topic, userprompt-remind.sh adds one fact line
    and logs `outcome=commit-nudge`, once per commit (`nudged_commits`,
    bounded to the last 20 in state). `last_seen_heads` always advances to
    the new HEAD once a root is examined, whether or not it nudges."""

    def _commit(self, repo, message, filename="newcommit.py", content="# x\n"):
        _write(repo / "src" / filename, content)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
             "commit", "-q", "-m", message],
            cwd=repo, check=True,
        )
        return git_head(repo)

    def _seed_last_seen(self, session_id, heads: dict):
        """Directly seeds state["last_seen_heads"] -- the multi-root suite's
        own established pattern for start_code_shas (see
        test_userprompt_remind_multiroot_coverage_and_head_changed)."""
        state_file = self.state_file(session_id)
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {}
        if state_file.exists():
            state = json.loads(state_file.read_text())
        state["last_seen_heads"] = heads
        state_file.write_text(json.dumps(state))

    def test_commit_without_top_id_and_unmapped_edit_gets_nudged(self):
        session_id = "s-nudge-a"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        new_sha = self._commit(self.code_root, "a plain commit, no decision id")

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        short = new_sha[:7]
        root_name = self.code_root.name
        self.assertIn(
            f"Commit {short} under {root_name} names no decision; "
            "1 file(s) edited under this root in the session have no topic",
            ctx,
        )
        self.assertIn(
            "name the decision (TOP-xxxx Ln) in the message, or write the record.", ctx
        )
        self.assertEqual(scan_forbidden_lines(ctx), [])

        log_text = (self.home / "hook.log").read_text()
        nudge_lines = [l for l in log_text.splitlines() if "outcome=commit-nudge" in l]
        self.assertEqual(len(nudge_lines), 1, log_text)
        self.assertIn(f"sha={short}", nudge_lines[0])
        self.assertIn(f"root={self.code_root}", nudge_lines[0])
        self.assertTrue(
            nudge_lines[0].rstrip().endswith(f"project={self.project}"), nudge_lines[0]
        )
        self.assertNotIn("Traceback", log_text)

        state = self.load_state(session_id)
        self.assertEqual(state["last_seen_heads"][str(self.code_root)], new_sha)
        self.assertIn(new_sha, state["nudged_commits"])

    def test_same_commit_next_prompt_does_not_repeat(self):
        session_id = "s-nudge-b"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        self._commit(self.code_root, "no decision here either")

        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        log_before = (self.home / "hook.log").read_text()
        self.assertEqual(log_before.count("outcome=commit-nudge"), 1, log_before)

        # Same commit still HEAD -- a second prompt must not repeat the nudge.
        proc2, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        log_after = (self.home / "hook.log").read_text()
        self.assertEqual(log_after.count("outcome=commit-nudge"), 1, log_after)

    def test_revisited_sha_already_nudged_is_not_renudged(self):
        """Beyond the brief's own list: exercises nudged_commits itself (not
        just the trivial "HEAD did not move" case above) -- HEAD moves away
        from an already-nudged commit and back to it; the revisit must not
        re-nudge even though last_seen_heads reads it as "moved" again."""
        session_id = "s-nudge-b2"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        nudged_sha = self._commit(self.code_root, "no decision on this one")
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        log1 = (self.home / "hook.log").read_text()
        self.assertEqual(log1.count("outcome=commit-nudge"), 1, log1)

        self._commit(
            self.code_root, "TOP-0042 L3 a second, decided commit",
            filename="secondcommit.py", content="# second\n",
        )
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())

        subprocess.run(["git", "reset", "-q", "--hard", nudged_sha], cwd=self.code_root, check=True)
        proc3, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc3.returncode, 0, proc3.stderr)
        log3 = (self.home / "hook.log").read_text()
        self.assertEqual(log3.count("outcome=commit-nudge"), 1, log3)

    def test_commit_naming_a_decision_is_not_nudged(self):
        session_id = "s-nudge-c"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        new_sha = self._commit(self.code_root, "TOP-0042 L3 names its own decision")

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("outcome=commit-nudge", log_text)
        if proc.stdout.strip():
            ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("names no decision", ctx)

        state = self.load_state(session_id)
        self.assertEqual(state["last_seen_heads"][str(self.code_root)], new_sha)

    def test_commit_naming_a_decision_with_a_short_id_is_not_nudged(self):
        """G9 (Codex 16's class, carried into this hook): SCHEMA.md's own
        running example topic is `id: TOP-42`, two digits -- no fixed digit
        count is enforced on `id:` anywhere else in this store, matching
        memlint.py's own decision-marker regex (`TOP-\\d+`, fixed in fix wave
        1 G2). This hook's own TOP_RE used to require exactly four digits,
        so a commit naming a genuinely shorter (or longer) id read as naming
        NO decision at all and got wrongly nudged."""
        session_id = "s-nudge-c2"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        new_sha = self._commit(self.code_root, "TOP-42 L1 names its own decision")

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("outcome=commit-nudge", log_text)
        if proc.stdout.strip():
            ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("names no decision", ctx)

        state = self.load_state(session_id)
        self.assertEqual(state["last_seen_heads"][str(self.code_root)], new_sha)

    def test_commit_with_no_unmapped_edits_is_not_nudged_but_last_seen_advances(self):
        session_id = "s-nudge-d"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        # Only a MAPPED file in the ledger -- unmapped's own list for this
        # root is empty, so the nudge's own count-under-root is 0.
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "mapped.py"), "code")])

        new_sha = self._commit(self.code_root, "no decision, but nothing unmapped either")

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("outcome=commit-nudge", log_text)
        if proc.stdout.strip():
            ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertNotIn("names no decision", ctx)

        # Rule 3: last_seen_heads still advances even when nothing is nudged.
        state = self.load_state(session_id)
        self.assertEqual(state["last_seen_heads"][str(self.code_root)], new_sha)

    def test_two_roots_only_one_moved_nudges_only_that_root(self):
        session_id = "s-nudge-e"
        code_root_b = Path(self.td) / "code-b"
        code_root_b.mkdir()
        _write(code_root_b / "src" / "b_unmapped.py", "# b unmapped\n")
        git_init(code_root_b)

        roots = [str(self.code_root.resolve()), str(code_root_b.resolve())]
        env = self.base_env(
            MEMCONTINUUM_CODE_ROOT=roots[0], MEMCONTINUUM_CODE_ROOTS=json.dumps(roots),
        )
        self._seed_last_seen(session_id, {
            str(self.code_root.resolve()): git_head(self.code_root),
            str(code_root_b.resolve()): git_head(code_root_b),
        })
        self.seed_ledger(session_id, [
            (str(self.code_root / "src" / "unmapped.py"), "code"),
            (str(code_root_b / "src" / "b_unmapped.py"), "code"),
        ])

        # Advance root B only, with no decision id.
        new_sha_b = self._commit(code_root_b, "root b change, no decision")

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(f"Commit {new_sha_b[:7]} under {code_root_b.name} names no decision", ctx)
        self.assertEqual(ctx.count("names no decision"), 1, ctx)

        log_text = (self.home / "hook.log").read_text()
        self.assertEqual(log_text.count("outcome=commit-nudge"), 1, log_text)
        self.assertIn(f"root={code_root_b.resolve()}", log_text)

        state = self.load_state(session_id)
        self.assertEqual(
            state["last_seen_heads"][str(self.code_root.resolve())], git_head(self.code_root)
        )
        self.assertEqual(state["last_seen_heads"][str(code_root_b.resolve())], new_sha_b)

    def test_colliding_relative_names_across_sibling_roots_count_independently(self):
        """Codex 11 / Grok M6: two sibling (non-nested) code roots each have
        their OWN unrelated file at the identical relative path
        (src/collide.py), neither topic-covered. Both edited this session;
        only root B is committed, with no decision id. The nudge count for
        root B's own commit must be exactly 1 (root B's own file) -- never
        2 (double-counting root A's unrelated file merely because its
        relative-path STRING happens to match), and never 0 (a
        `by_path`/set-membership regression that fails to recognize root
        B's own file at all)."""
        session_id = "s-nudge-collide"
        code_root_b = Path(self.td) / "code-b"
        code_root_b.mkdir()
        _write(code_root_b / "src" / "collide.py", "# root b, unmapped\n")
        git_init(code_root_b)
        # self.code_root already has src/mapped.py (topic-covered) and
        # src/unmapped.py from HookTestBase.setUp; add the colliding name.
        _write(self.code_root / "src" / "collide.py", "# root a, unmapped\n")
        subprocess.run(["git", "add", "-A"], cwd=self.code_root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
             "commit", "-q", "-m", "seed root a collide.py"],
            cwd=self.code_root, check=True,
        )

        roots = [str(self.code_root.resolve()), str(code_root_b.resolve())]
        env = self.base_env(
            MEMCONTINUUM_CODE_ROOT=roots[0], MEMCONTINUUM_CODE_ROOTS=json.dumps(roots),
        )
        self._seed_last_seen(session_id, {
            str(self.code_root.resolve()): git_head(self.code_root),
            str(code_root_b.resolve()): git_head(code_root_b),
        })
        self.seed_ledger(session_id, [
            (str(self.code_root / "src" / "collide.py"), "code"),
            (str(code_root_b / "src" / "collide.py"), "code"),
        ])

        new_sha_b = self._commit(
            code_root_b, "root b collide, no decision", filename="collide.py",
            content="# root b, unmapped, edited again\n",
        )

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn(
            f"Commit {new_sha_b[:7]} under {code_root_b.name} names no decision; "
            "1 file(s) edited under this root in the session have no topic",
            ctx,
        )
        log_text = (self.home / "hook.log").read_text()
        self.assertEqual(log_text.count("outcome=commit-nudge"), 1, log_text)

    def test_deleted_root_fails_open_no_traceback(self):
        session_id = "s-nudge-f"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        shutil.rmtree(self.code_root)

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("outcome=commit-nudge", log_text)
        self.assertNotIn("Traceback", log_text)
        self.assertNotIn("Traceback", proc.stderr)

    def test_git_log_failure_after_real_head_move_fails_open(self):
        """A corrupted commit object: `git rev-parse HEAD` (mc_git_head,
        used for CODE_HEADS/the gate) still succeeds, but `git log -1
        --format=%B` (this feature's own call) fails -- the failure this
        rule's own subprocess call must itself absorb, distinct from a
        wholly deleted root (the gate never even sees a moved head there)."""
        session_id = "s-nudge-f2"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        new_sha = self._commit(self.code_root, "a commit whose object goes missing")
        obj_file = self.code_root / ".git" / "objects" / new_sha[:2] / new_sha[2:]
        self.assertTrue(obj_file.is_file(), obj_file)
        obj_file.rename(obj_file.with_suffix(".bak"))

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertNotIn("outcome=commit-nudge", log_text)
        self.assertNotIn("Traceback", log_text)
        self.assertNotIn("Traceback", proc.stderr)

    def test_clear_reseeds_last_seen_heads_from_current_head(self):
        session_id = "s-nudge-g"
        run_script(SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.base_env())
        start_state = self.load_state(session_id)
        self.assertEqual(
            start_state["last_seen_heads"][str(self.code_root)], git_head(self.code_root)
        )

        new_sha = self._commit(self.code_root, "advance past startup, no decision")
        self.assertNotEqual(new_sha, start_state["last_seen_heads"][str(self.code_root)])

        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "clear"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cleared_state = self.load_state(session_id)
        self.assertEqual(cleared_state["last_seen_heads"][str(self.code_root)], new_sha)
        self.assertEqual(cleared_state["nudged_commits"], [])

    def test_moved_head_is_checked_regardless_of_coverage_candidacy(self):
        """Codex 9 (BLOCKING): the brief's own shape -- a coverage reminder
        fires first (a genuine candidate turn, consuming `grew` against
        last_injected_pairs), THEN a commit with no decision id, THEN five
        more prompts with no NEW ledger growth (so coverage candidacy is
        False on every one of them). The moved-HEAD check -- and the
        commit nudge it gates -- must still fire exactly once across those
        five prompts; last_seen_heads must still advance. On the old code,
        the whole moved-HEAD computation lived inside `if CANDIDATE=1`, so
        none of those five prompts ever even looked at HEAD: zero nudges,
        last_seen_heads frozen at whatever it was after prompt 1."""
        session_id = "s-nudge-candidacy"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        # Prompt 1: a genuine coverage candidate turn (first ever ledger
        # pairs, nothing injected yet) -- fires "injected", consuming grew.
        proc1, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertTrue(proc1.stdout.strip(), "prompt 1 must be a genuine coverage candidate")
        state_after_1 = self.load_state(session_id)
        self.assertTrue(state_after_1.get("last_injected_pairs"), state_after_1)

        new_sha = self._commit(self.code_root, "a commit with no decision id, after the reminder")

        # Five more prompts, same session, same (unchanged) ledger -- grew
        # is False every time, so coverage is never a candidate again.
        for _ in range(5):
            proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
            self.assertEqual(proc.returncode, 0, proc.stderr)

        log_text = (self.home / "hook.log").read_text()
        self.assertEqual(
            log_text.count("outcome=commit-nudge"), 1,
            "the commit nudge must fire exactly once across the five non-candidate "
            "prompts following the commit, not zero",
        )
        self.assertIn("outcome=nudge-only", log_text)
        self.assertNotIn("Traceback", log_text)

        state = self.load_state(session_id)
        self.assertEqual(state["last_seen_heads"][str(self.code_root)], new_sha)
        self.assertIn(new_sha, state["nudged_commits"])

    def test_nudge_only_turn_never_touches_coverage_cooldown_bookkeeping(self):
        """Codex 9, the risk the advisor named alongside the fix: a
        nudge-only turn (CANDIDATE=0, a root moved) must never run
        coverage's OWN Phase-3 bookkeeping (last_injected_pairs,
        last_inject_turn/time/ts) -- doing so would let a commit nudge
        quietly re-arm coverage's cooldown clock without coverage ever
        actually having fired again. Kept to a single nudge-only prompt
        (unlike the five-prompt brief scenario above) so the unrelated
        T-thin look-back reminder -- which legitimately touches this same
        bookkeeping once it becomes eligible after enough turns -- never
        confounds this specific assertion."""
        session_id = "s-nudge-cooldown"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])

        proc1, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        state_after_1 = self.load_state(session_id)
        self.assertTrue(state_after_1.get("last_injected_pairs"), state_after_1)

        self._commit(self.code_root, "no decision id, one nudge-only prompt follows")

        proc2, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc2.returncode, 0, proc2.stderr)

        log_text = (self.home / "hook.log").read_text()
        self.assertEqual(log_text.count("outcome=commit-nudge"), 1, log_text)
        self.assertIn("outcome=nudge-only", log_text)

        state = self.load_state(session_id)
        self.assertEqual(state["last_injected_pairs"], state_after_1["last_injected_pairs"])
        self.assertEqual(state["last_inject_turn"], state_after_1["last_inject_turn"])
        self.assertEqual(state["last_inject_time"], state_after_1["last_inject_time"])

    def test_stats_reports_commit_nudges(self):
        session_id = "s-nudge-h"
        start_sha = git_head(self.code_root)
        self._seed_last_seen(session_id, {str(self.code_root): start_sha})
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        self._commit(self.code_root, "counted by stats, no decision")

        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertEqual(log_text.count("outcome=commit-nudge"), 1, log_text)

        stats_args = SimpleNamespace(
            project=self.project, days=7, home=str(self.home), store=None, json=True, now=None,
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = memidx.cmd_stats(stats_args)
        self.assertEqual(rc, 0)
        result = json.loads(buf.getvalue())
        self.assertEqual(result["nudges"]["commit_nudges"], 1, result["nudges"])


# ---------------------------------------------------------------------------
# 6. sessionend-stamp.sh
# ---------------------------------------------------------------------------


class TestSessionEndStamp(HookTestBase):
    def test_bash_syntax_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(SESSIONEND_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stamps_ended_at_only_silent(self):
        session_id = "s-end-stamp"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        before = self.load_state(session_id)
        self.assertNotIn("ended_at", before)

        proc, elapsed = run_script(SESSIONEND_HOOK, self.session_end_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertLess(elapsed, 1.5)

        after = self.load_state(session_id)
        self.assertIn("ended_at", after)
        self.assertEqual(after["ledger"], before["ledger"])

    def test_log_line_carries_project(self):
        """Liveness metric fix: every sessionend-stamp.sh outcome line must
        carry project=<MC_PROJECT> (via mc_log in memlib.sh)."""
        session_id = "s-end-project"
        run_script(SESSIONEND_HOOK, self.session_end_payload(session_id), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "sessionend" in l]
        self.assertTrue(matching)
        for line in matching:
            self.assertIn(f"project={self.project}", line, line)

    def test_store_byte_identical(self):
        session_id = "s-end-bytesafe"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        run_script(SESSIONEND_HOOK, self.session_end_payload(session_id), self.base_env())
        self.assertTrue(git_is_clean(self.store_root))

    def test_fail_open_no_state(self):
        proc, _ = run_script(SESSIONEND_HOOK, self.session_end_payload("s-end-nostate"), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_fail_open_malformed_payload(self):
        proc, _ = run_script(SESSIONEND_HOOK, "{{{ garbage", self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_completes_under_poisoned_pythonpath(self):
        session_id = "s-end-poison"
        self.seed_ledger(session_id, [])
        proc, elapsed = run_script(
            SESSIONEND_HOOK, self.session_end_payload(session_id), self.poisoned_base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 1.5)

    def test_watchdog_budget_reduced_to_1_2s(self):
        """Finding 1: SessionEnd's own harness budget is 1.5s (this
        hook's own header), tighter than the shared 2s default every
        other hook's guard uses (hooks/mc-watchdog.sh) -- its own
        watchdog budget must leave real headroom under that ceiling, not
        consume it. A python wrapper that hangs on every real work call
        (same pattern as e.g. TestPrecompactPersist.test_fail_open_on_timeout)
        proves the bound empirically: the OLD shared 2s default would
        leave ~2.0-2.3s elapsed here (launcher startup + post-kill reap
        add a little past the bare 2.0s); the reduced 1.2s budget must
        land well under that, with real margin to spare before the 1.5s
        harness ceiling."""
        session_id = "s-end-budget"
        self.seed_ledger(session_id, [])
        hang_py = Path(self.td) / "hang-python-sessionend"
        hang_py.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *MC_WATCHDOG_LAUNCHER*) exec \"" + VENV_PYTHON + "\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            "sleep 6\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        hang_py.chmod(0o755)
        env = self.base_env(MEMCONTINUUM_PYTHON=str(hang_py))
        proc, elapsed = run_script(
            SESSIONEND_HOOK, self.session_end_payload(session_id), env, timeout=10.0
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(
            elapsed, 1.8, f"SessionEnd's watchdog budget must be ~1.2s (not the shared 2s), took {elapsed:.3f}s"
        )

    def test_fail_open_when_watchdog_lib_missing(self):
        """R1 regression, round 4 gate: see TestLedgerPostEdit's twin."""
        session_id = "s-end-watchdog-lib-missing"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        env = self.base_env(MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh")
        proc, _ = run_script(SESSIONEND_HOOK, self.session_end_payload(session_id), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")
        after = self.load_state(session_id)
        self.assertIn("ended_at", after)


# ---------------------------------------------------------------------------
# 6c. code-refresh.sh (TOP-0137 L2 step 4): Stop + SessionStart code-index freshness
# ---------------------------------------------------------------------------


class TestCodeRefreshHook(HookTestBase):
    """Every test runs in the HookTestBase temp HOME; nothing touches the
    real ~/.memcontinuum. Each run is checked for the four invariants the
    hook promises on every path: exit 0, empty stdout, empty stderr, and
    exactly one `code-refresh outcome=` line in hook.log."""

    def setUp(self):
        super().setUp()
        self.db = self.home / f"{self.project}-code.sqlite"
        self.marker = self.home / f"{self.project}.code-stale"
        self.lock = self.home / f"{self.project}.code-refresh.lock"

    def build_code_index(self, root=None):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rc = memidx.cmd_code_reindex(ns(
                code_root=str(root or self.code_root), db=str(self.db), project=self.project,
                no_embed=True, full=False, lang="python", drop_root=None,
            ))
        self.assertEqual(rc, 0)

    def refresh_env(self, **overrides):
        # TOP-0137 L2 step 5: the hook spawns a detached code-embed-worker
        # whenever chunks await a vector; off by default here so no test
        # loads a real model by accident (the embed tests override it).
        merged = {"MEMCONTINUUM_CODE_ROOTS": json.dumps([str(self.code_root)]),
                  "MEMCONTINUUM_EMBED_WORKER": "0"}
        merged.update(overrides)
        return self.base_env(**merged)

    def payload(self, event, session_id="s-refresh"):
        return json.dumps({"session_id": session_id, "hook_event_name": event, "cwd": str(self.code_root)})

    def log_lines(self):
        log = self.home / "hook.log"
        if not log.exists():
            return []
        return [l for l in log.read_text().splitlines() if " code-refresh outcome=" in l]

    def run_hook(self, event="Stop", env=None, timeout=20.0, payload=None):
        before = len(self.log_lines())
        proc, elapsed = run_script(
            CODE_REFRESH_HOOK, payload if payload is not None else self.payload(event),
            env or self.refresh_env(), timeout=timeout,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "a Stop hook that prints is fed back to the model")
        self.assertEqual(proc.stderr, "")
        lines = self.log_lines()
        self.assertEqual(len(lines) - before, 1, lines)
        return lines[-1], elapsed

    def chunk_symbols(self):
        conn = memidx.open_code_db(self.db)
        try:
            return {r["symbol"] for r in conn.execute("SELECT symbol FROM chunks")}
        finally:
            conn.close()

    def head_sha(self):
        conn = memidx.open_code_db(self.db)
        try:
            return conn.execute("SELECT head_sha FROM code_meta WHERE project=?", (self.project,)).fetchone()[0]
        finally:
            conn.close()

    def commit_all(self, msg="change"):
        subprocess.run(["git", "add", "-A"], cwd=self.code_root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=self.code_root, check=True)

    def patched_python(self, patch_src):
        """A MEMCONTINUUM_PYTHON wrapper that runs memidx.py code-refresh
        with `patch_src` applied first (the watchdog launcher and every
        other call go straight to the real interpreter)."""
        runner = Path(self.td) / "patched-code-refresh.py"
        runner.write_text(
            "import sys, time\n"
            f"sys.path.insert(0, {str(TOOLS_DIR)!r})\n"
            "import memidx\n" + patch_src + "\n"
            "sys.exit(memidx.main(sys.argv[2:]))\n"
        )
        wrapper = Path(self.td) / "patched-python"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            'for a in "$@"; do\n'
            '  if [ "$a" = "code-refresh" ]; then exec "' + VENV_PYTHON + '" "' + str(runner) + '" "$@"; fi\n'
            "done\n"
            'exec "' + VENV_PYTHON + '" "$@"\n'
        )
        wrapper.chmod(0o755)
        return str(wrapper)

    def test_bash_syntax_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(CODE_REFRESH_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uninitialized_skips_and_creates_nothing(self):
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=skip reason=uninitialized", line)
        self.assertFalse(self.db.exists(), "the hook must never create a code db")
        self.assertFalse(self.marker.exists())

    # -- TOP-0137 L2 step 5: the embed worker spawn -------------------------

    def record_popen_python(self):
        """A MEMCONTINUUM_PYTHON wrapper whose code-refresh run records the
        argv of every subprocess.Popen instead of starting it (so no worker,
        and no model, ever runs); returns (python, record_file)."""
        rec = Path(self.td) / "popen-record.jsonl"
        py = self.patched_python(
            "import json\n"
            "_real_popen = memidx.subprocess.Popen\n"
            "class _P:\n"
            "    def __new__(cls, argv, **kw):\n"
            "        if 'code-embed-worker' not in list(argv):\n"
            "            return _real_popen(argv, **kw)\n"
            f"        open({str(rec)!r}, 'a').write(json.dumps({{'argv': argv, 'new_session': kw.get('start_new_session')}}) + '\\n')\n"
            "        return object.__new__(cls)\n"
            "memidx.subprocess.Popen = _P\n"
        )
        return py, rec

    def embed_marker(self):
        return self.home / f"{self.project}.code-embed-pending"

    def build_chunky_index(self):
        """The fixture repo's files are comment-only (zero chunks, nothing to
        embed): give one real functions, then index."""
        _write(self.code_root / "src" / "mapped.py", "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
        self.build_code_index()

    def test_stop_spawns_the_embed_worker_for_a_current_index_with_no_vectors(self):
        self.build_chunky_index()
        py, rec = self.record_popen_python()
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_EMBED_WORKER="1"))
        self.assertIn("outcome=skip reason=current", line)
        self.assertTrue(line.endswith("embed=spawned"), line)
        calls = [json.loads(l) for l in rec.read_text().splitlines()]
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["argv"][2:], ["code-embed-worker", "--project", self.project, "--db", str(self.db),
                                               "--root", str(self.code_root.resolve())])
        self.assertTrue(calls[0]["new_session"])
        self.assertTrue(self.embed_marker().exists())

    def test_session_start_spawns_it_too(self):
        self.build_chunky_index()
        py, rec = self.record_popen_python()
        line, _ = self.run_hook("SessionStart", env=self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_EMBED_WORKER="1"))
        self.assertIn("event=SessionStart", line)
        self.assertTrue(line.endswith("embed=spawned"), line)
        self.assertEqual(len(rec.read_text().splitlines()), 1)

    def test_embed_worker_off_raises_the_marker_but_spawns_nothing(self):
        self.build_chunky_index()
        py, rec = self.record_popen_python()
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_EMBED_WORKER="0"))
        self.assertTrue(line.endswith("embed=off"), line)
        self.assertFalse(rec.exists(), "MEMCONTINUUM_EMBED_WORKER=0 must not spawn")
        self.assertTrue(self.embed_marker().exists())

    def test_spawn_happens_even_while_the_worker_lock_is_held(self):
        self.build_chunky_index()
        py, rec = self.record_popen_python()
        fd = os.open(str(self.home / f"{self.project}.code-embed.lock"), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_EMBED_WORKER="1"))
        finally:
            os.close(fd)
        # the lock-probe `pending` answer is gone: a worker finding the lock
        # held exits 0 itself, so a worker finishing right now cannot swallow
        # the request
        self.assertTrue(line.endswith("embed=spawned"), line)
        self.assertEqual(len(rec.read_text().splitlines()), 1)

    def test_nothing_awaiting_is_clean_and_spawns_nothing(self):
        self.build_chunky_index()
        fp = memidx.embedding_fingerprint(model=None).replace("revision=unknown", "revision=r1")
        conn = memidx.open_code_db(self.db)
        for (cid,) in conn.execute("SELECT id FROM chunks").fetchall():
            conn.execute("INSERT INTO embeddings (chunk_id, project, dim, vector, embed_fp) VALUES (?,?,?,?,?)",
                         (cid, self.project, 4, memidx.pack_vector([0.1, 0.2, 0.3, 0.4]), fp))
        conn.commit(); conn.close()
        py, rec = self.record_popen_python()
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_EMBED_WORKER="1"))
        self.assertTrue(line.endswith("embed=clean"), line)
        self.assertFalse(rec.exists())
        self.assertFalse(self.embed_marker().exists())

    def test_a_heal_that_adds_chunks_spawns_after_the_heal_on_stop(self):
        self.build_chunky_index()
        fp = memidx.embedding_fingerprint(model=None).replace("revision=unknown", "revision=r1")
        conn = memidx.open_code_db(self.db)
        for (cid,) in conn.execute("SELECT id FROM chunks").fetchall():
            conn.execute("INSERT INTO embeddings (chunk_id, project, dim, vector, embed_fp) VALUES (?,?,?,?,?)",
                         (cid, self.project, 4, memidx.pack_vector([0.1, 0.2, 0.3, 0.4]), fp))
        conn.commit(); conn.close()
        (self.code_root / "src" / "mapped.py").write_text("def brand_new_symbol():\n    return 1\n")
        py, rec = self.record_popen_python()
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_EMBED_WORKER="1"))
        self.assertIn("outcome=refreshed reason=healed", line)
        self.assertIn(" embed=spawned", line)
        self.assertEqual(len(rec.read_text().splitlines()), 1)

    @unittest.skipUnless(os.environ.get("MEMCONTINUUM_TEST_EMBED_CACHE"),
                         "set $MEMCONTINUUM_TEST_EMBED_CACHE to a fastembed cache dir to run the real worker")
    def test_the_spawned_worker_really_fills_the_index(self):
        self.build_chunky_index()
        cache = os.environ["MEMCONTINUUM_TEST_EMBED_CACHE"]
        line, _ = self.run_hook("Stop", env=self.refresh_env(
            MEMCONTINUUM_EMBED_WORKER="1", FASTEMBED_CACHE_PATH=cache))
        self.assertTrue(line.endswith("embed=spawned"), line)
        deadline = time.monotonic() + 120
        # claim-first protocol: the marker is renamed to <marker>.claim.<pid> when the
        # pass STARTS, so "finished" means neither the marker nor a claim remains
        def in_flight():
            return self.embed_marker().exists() or list(self.home.glob(f"{self.embed_marker().name}.claim.*"))
        while time.monotonic() < deadline and in_flight():
            time.sleep(0.5)
        self.assertFalse(in_flight(), "the worker never finished")
        conn = memidx.open_code_db(self.db)
        try:
            report = memidx.code_index_report(conn, self.project)
        finally:
            conn.close()
        self.assertEqual((report["embedding_mode"], report["awaiting_embedding"]), ("full", 0))

    def test_current_index_skips(self):
        self.build_code_index()
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=skip reason=current", line)
        self.assertIn(f"project={self.project}", line)
        self.assertIn("roots=1 unrecorded=0 changed=0 removed=0 healed=0", line)
        self.assertIn("event=Stop session=s-refresh", line)
        self.assertFalse(self.marker.exists())

    def test_stop_refreshes_an_edited_file_without_a_commit(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def brand_new_symbol():\n    return 1\n")
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=refreshed reason=healed", line)
        self.assertIn("changed=1 removed=0 healed=1", line)
        self.assertIn("brand_new_symbol", self.chunk_symbols())
        self.assertFalse(self.marker.exists())
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=skip reason=current", line)

    def test_stop_refreshes_after_a_commit_and_advances_head_sha(self):
        self.build_code_index()
        before = self.head_sha()
        self.assertEqual(before, git_head(self.code_root))
        (self.code_root / "src" / "mapped.py").write_text("def committed_symbol():\n    return 2\n")
        self.commit_all()
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=refreshed", line)
        self.assertIn("changed=1", line)
        self.assertNotEqual(self.head_sha(), before)
        self.assertEqual(self.head_sha(), git_head(self.code_root))
        self.assertIn("committed_symbol", self.chunk_symbols())

    def test_a_commit_that_changes_nothing_indexed_only_advances_head_sha(self):
        """An empty commit moves HEAD with no content change: the preflight's
        git trigger verifies and refreshes head_sha itself (skip=current)."""
        self.build_code_index()
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "empty"], cwd=self.code_root, check=True)
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=skip reason=current", line)
        self.assertEqual(self.head_sha(), git_head(self.code_root))

    def test_deleted_file_is_purged_removed_only(self):
        self.build_code_index()
        (self.code_root / "src" / "unmapped.py").write_text("def doomed_symbol():\n    return 3\n")
        self.run_hook("Stop")
        self.assertIn("doomed_symbol", self.chunk_symbols())
        (self.code_root / "src" / "unmapped.py").unlink()
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=refreshed", line)
        self.assertIn("removed=1", line)
        self.assertNotIn("doomed_symbol", self.chunk_symbols())

    def test_sessionstart_preflights_only_and_writes_the_marker(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def pending_symbol():\n    return 4\n")
        line, _ = self.run_hook("SessionStart")
        self.assertIn("outcome=skip reason=stale-marked", line)
        self.assertIn("changed=1", line)
        self.assertNotIn("pending_symbol", self.chunk_symbols(), "SessionStart must never reindex")
        text = self.marker.read_text()
        self.assertRegex(text, r"^reason=stale changed=1 since=\S+ roots=1\n$")
        # the first Stop refreshes and clears it
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=refreshed", line)
        self.assertIn("pending_symbol", self.chunk_symbols())
        self.assertFalse(self.marker.exists())

    def test_current_preflight_clears_a_leftover_marker(self):
        self.build_code_index()
        self.marker.write_text("reason=refreshing since=2026-01-01T00:00:00+00:00 roots=1\n")
        self.run_hook("SessionStart")
        self.assertFalse(self.marker.exists())

    def test_unrecorded_root_is_counted_and_left_alone(self):
        self.build_code_index()
        other = Path(self.td) / "other-root"
        other.mkdir()
        (other / "o.py").write_text("def o():\n    return 0\n")
        env = self.refresh_env(MEMCONTINUUM_CODE_ROOTS=json.dumps([str(self.code_root), str(other)]))
        line, _ = self.run_hook("Stop", env=env)
        self.assertIn("unrecorded=1", line)
        self.assertIn("roots=1", line)
        self.assertNotIn("o", self.chunk_symbols())

    # -- INC-0129: a recorded root the wiring does not name is stale --------

    def stale_state(self):
        """A recorded A (wired) plus a recorded, still-on-disk B (not wired);
        B's chunks carry a fake vector and the project claims mode `full`."""
        self.build_chunky_index()
        old = Path(self.td) / "old-root"
        old.mkdir()
        (old / "o.py").write_text("def old_symbol():\n    return 0\n")
        self.build_code_index(old)
        old_s = str(old.resolve())
        conn = memidx.open_code_db(self.db)
        try:
            cid = conn.execute("SELECT id FROM chunks WHERE code_root=?", (old_s,)).fetchone()[0]
            conn.execute(
                "INSERT INTO embeddings (chunk_id, project, dim, vector, embed_fp) VALUES (?,?,?,?,?)",
                (cid, self.project, 2, b"\x00" * 8, "fp"))
            conn.execute("UPDATE code_project SET embedding_mode='full' WHERE project=?", (self.project,))
            conn.commit()
        finally:
            conn.close()
        return old_s

    def chunk_ids(self, root_s):
        conn = memidx.open_code_db(self.db)
        try:
            return [r[0] for r in conn.execute("SELECT id FROM chunks WHERE code_root=?", (root_s,))]
        finally:
            conn.close()

    def rows_of(self, root_s, ids=None):
        """Row counts for one root. `ids` are chunk ids captured BEFORE a
        deletion: fts/embeddings are counted by those ids, so an orphan left
        behind by a deletion that missed its shadows is still seen."""
        conn = memidx.open_code_db(self.db)
        try:
            if ids is None:
                ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE code_root=?", (root_s,))]
            return {
                "chunks": conn.execute("SELECT COUNT(*) FROM chunks WHERE code_root=?", (root_s,)).fetchone()[0],
                "fts": sum(conn.execute("SELECT COUNT(*) FROM fts WHERE rowid=?", (i,)).fetchone()[0] for i in ids),
                "emb": sum(conn.execute("SELECT COUNT(*) FROM embeddings WHERE chunk_id=?", (i,)).fetchone()[0] for i in ids),
                "file_sha": conn.execute("SELECT COUNT(*) FROM file_sha WHERE code_root=?", (root_s,)).fetchone()[0],
                "meta": conn.execute("SELECT COUNT(*) FROM code_meta WHERE code_root=?", (root_s,)).fetchone()[0],
                "mode": conn.execute("SELECT embedding_mode FROM code_project").fetchone()[0],
            }
        finally:
            conn.close()

    def test_stop_drops_a_recorded_root_the_wiring_does_not_name(self):
        old_s = self.stale_state()
        a_s = str(self.code_root.resolve())
        a_before = self.rows_of(a_s)
        old_ids = self.chunk_ids(old_s)
        self.assertTrue(old_ids)
        self.assertEqual({k: v > 0 for k, v in self.rows_of(old_s).items() if k != "mode"},
                         {"chunks": True, "fts": True, "emb": True, "file_sha": True, "meta": True})
        line, _ = self.run_hook("Stop")
        self.assertIn("dropped=1", line)
        self.assertIn(f"detail=dropped-roots:{old_s}", line)
        self.assertIn("roots=1", line)
        gone = self.rows_of(old_s, ids=old_ids)
        self.assertEqual({k: v for k, v in gone.items() if k != "mode"},
                         {"chunks": 0, "fts": 0, "emb": 0, "file_sha": 0, "meta": 0})
        a_after = self.rows_of(a_s)
        self.assertEqual(a_after["mode"], "none", "embedding_mode recomputed from real coverage")
        self.assertEqual({k: v for k, v in a_after.items() if k != "mode"},
                         {k: v for k, v in a_before.items() if k != "mode"}, "the wired root is untouched")
        line, _ = self.run_hook("Stop")
        self.assertIn("dropped=0", line)
        self.assertNotIn("dropped-roots", line)

    def test_session_start_never_drops_and_marks_stale_root(self):
        old_s = self.stale_state()
        before = self.rows_of(old_s)
        line, _ = self.run_hook("SessionStart")
        self.assertIn("dropped=0", line)
        self.assertEqual(self.rows_of(old_s), before, "SessionStart is preflight only")
        self.assertRegex(self.marker.read_text(), r"^reason=stale-root=1 since=\S+ roots=2\n$")
        line, _ = self.run_hook("Stop")
        self.assertIn("dropped=1", line)
        self.assertFalse(self.marker.exists(), "the Stop run clears the marker by dropping")

    def test_a_hook_that_names_no_roots_drops_nothing(self):
        old_s = self.stale_state()
        before = self.rows_of(old_s)
        env = self.refresh_env(MEMCONTINUUM_CODE_ROOTS="[]")
        line, _ = self.run_hook("Stop", env=env)
        self.assertIn("dropped=0", line)
        self.assertEqual(self.rows_of(old_s), before)

    def test_a_relative_wired_root_disables_the_drop(self):
        old_s = self.stale_state()
        ids = self.chunk_ids(old_s)
        before = self.rows_of(old_s, ids=ids)
        a_before = self.rows_of(str(self.code_root.resolve()))
        env = self.refresh_env(MEMCONTINUUM_CODE_ROOTS=json.dumps(["Sources"]))
        line, _ = self.run_hook("Stop", env=env)
        self.assertIn("dropped=0", line)
        self.assertIn("detail=stale-check-skipped:relative-wired-root", line)
        self.assertEqual(self.rows_of(old_s, ids=ids), before)
        self.assertEqual(self.rows_of(str(self.code_root.resolve())), a_before)

    def assert_drop_withheld(self, wired_root, detail):
        old_s = self.stale_state()
        a_s = str(self.code_root.resolve())
        ids = self.chunk_ids(old_s)
        before = self.rows_of(old_s, ids=ids)
        a_ids = self.chunk_ids(a_s)
        a_before = self.rows_of(a_s, ids=a_ids)
        self.assertTrue(a_ids)
        wired = wired_root if isinstance(wired_root, list) else [wired_root]
        env = self.refresh_env(MEMCONTINUUM_CODE_ROOTS=json.dumps([str(w) for w in wired]))
        line, _ = self.run_hook("Stop", env=env)
        self.assertNotIn("crash", line)
        self.assertIn("dropped=0", line)
        self.assertIn(f"detail=stale-check-skipped:{detail}", line)
        self.assertEqual(self.rows_of(old_s, ids=ids), before)
        self.assertEqual(self.rows_of(a_s, ids=a_ids), a_before)
        return line

    def test_an_unresolvable_wired_root_disables_the_drop_without_crashing(self):
        loop = Path(self.td) / "loop"
        os.symlink(loop, loop)  # a symlink loop: 3.12 raises, 3.13 resolves lax
        self.assert_drop_withheld(loop, "unresolvable-wired-root")

    def test_a_missing_wired_root_disables_the_drop(self):
        self.assert_drop_withheld(Path(self.td) / "does-not-exist", "unresolvable-wired-root")

    def test_a_wired_root_that_is_a_file_disables_the_drop(self):
        f = Path(self.td) / "plain-file"
        f.write_text("x")
        self.assert_drop_withheld(f, "wired-root-not-a-directory")

    def test_a_missing_wired_root_beside_a_good_one_withholds_the_drop(self):
        # wired {A, B}, B's directory gone, recorded {A, B, C}: C is stale but
        # the wiring is untrustworthy, so nothing is dropped.
        b = Path(self.td) / "wired-b"
        (b / "src").mkdir(parents=True)
        (b / "src" / "b.py").write_text("def b_fn():\n    return 0\n")
        git_init(b)
        self.build_code_index(root=b)
        b.rename(Path(self.td) / "wired-b-gone")
        line = self.assert_drop_withheld([self.code_root.resolve(), b], "unresolvable-wired-root")
        self.assertRegex(self.marker.read_text(), r"^reason=degraded missing-root=1 not-indexed=\d+ since=\S+ roots=3\n$")

    def test_a_wired_root_recorded_nowhere_disables_the_drop(self):
        other = Path(self.td) / "unrecorded-root"
        other.mkdir()
        line = self.assert_drop_withheld(other, "no-wired-root-recorded")
        self.assertIn("unrecorded=1", line)

    def test_lock_held_skips_and_leaves_the_marker_untouched(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def locked_out():\n    return 5\n")
        sentinel = "reason=refreshing since=2026-01-01T00:00:00+00:00 roots=9\n"
        self.marker.write_text(sentinel)
        fd = os.open(str(self.lock), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            line, _ = self.run_hook("Stop")
        finally:
            os.close(fd)
        self.assertIn("outcome=skip reason=locked", line)
        self.assertEqual(self.marker.read_text(), sentinel)
        self.assertNotIn("locked_out", self.chunk_symbols())

    def test_reindex_failure_rewrites_the_marker_with_the_reason(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def x():\n    return 6\n")
        py = self.patched_python("memidx.cmd_code_reindex = lambda a: 3")
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py))
        self.assertIn("outcome=failed reason=reindex-failed", line)
        self.assertRegex(self.marker.read_text(), r"^reason=reindex-failed rc=3 since=\S+ roots=1\n$")
        self.assertIn("detail=code-refresh: heal did not complete (code-reindex exit 3)", line)

    def test_heal_exception_rewrites_the_marker_with_the_type(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def y():\n    return 7\n")
        py = self.patched_python("def _boom(a):\n    raise RuntimeError('injected')\nmemidx.cmd_code_reindex = _boom")
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py))
        self.assertIn("outcome=failed reason=heal-error", line)
        self.assertRegex(self.marker.read_text(), r"^reason=heal-error RuntimeError since=\S+ roots=1\n$")

    def test_over_limit_refusal_is_written_into_the_marker(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def p():\n    return 8\n")
        (self.code_root / "src" / "unmapped.py").write_text("def q():\n    return 9\n")
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_CODE_REFRESH_LIMIT="1"))
        self.assertIn("outcome=failed reason=over-limit", line)
        self.assertRegex(self.marker.read_text(), r"^reason=over-limit changed=2 since=\S+ roots=1\n$")
        self.assertNotIn("p", self.chunk_symbols())
        self.assertNotIn("q", self.chunk_symbols())

    def test_limit_with_leading_zero_is_decimal_not_octal(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def zz():\n    return 1\n")
        line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_CODE_REFRESH_LIMIT="08"))
        self.assertIn("outcome=refreshed", line)

    def test_watchdog_kill_leaves_the_refreshing_marker_and_one_kill_line(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def slow():\n    return 1\n")
        py = self.patched_python("memidx.cmd_code_reindex = lambda a: time.sleep(60)")
        before = len(self.log_lines())
        proc, elapsed = run_script(
            CODE_REFRESH_HOOK, self.payload("Stop"),
            self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_CODE_REFRESH_BUDGET="1"), timeout=15.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertLess(elapsed, 4.0, f"the watchdog must bound the run, took {elapsed:.2f}s")
        log = (self.home / "hook.log").read_text()
        self.assertEqual(log.count("outcome=watchdog-killed hook=code-refresh.sh"), 1, log)
        self.assertEqual(len(self.log_lines()), before, "a killed run writes no code-refresh line of its own")
        self.assertRegex(self.marker.read_text(), r"^reason=refreshing since=\S+ roots=1\n$")

    def test_sessionstart_budget_is_two_seconds_not_the_refresh_budget(self):
        self.build_code_index()
        py = self.patched_python("memidx.cmd_code_reindex = lambda a: time.sleep(60)\n"
                                 "memidx.code_index_report = lambda *a, **k: time.sleep(60)")
        proc, elapsed = run_script(
            CODE_REFRESH_HOOK, self.payload("SessionStart"),
            self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_CODE_REFRESH_BUDGET="30"), timeout=15.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 4.5, f"SessionStart is a 2 s class hook, took {elapsed:.2f}s")
        self.assertIn("outcome=watchdog-killed hook=code-refresh.sh", (self.home / "hook.log").read_text())

    def test_invalid_budget_falls_back_to_the_default(self):
        self.build_code_index()
        for bad in ("abc", "0", "0.0", "-3", "1.2.3", ""):
            line, _ = self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_CODE_REFRESH_BUDGET=bad))
            self.assertIn("outcome=skip reason=current", line, bad)

    def test_log_line_carries_the_session_cwd(self):
        """INC-0130: which directory did this session run from. The worker's
        line (memidx.py) and the shell-composed skip line both carry cwd=."""
        self.build_code_index()
        line, _ = self.run_hook("Stop")
        self.assertIn(f" cwd={memidx._log_path_token(self.code_root)} ", line + " ")
        env = self.refresh_env(MEMCONTINUUM_PROJECT="../victim")
        line, _ = self.run_hook("Stop", env=env)
        self.assertIn("outcome=skip reason=bad-project", line)
        # Only memidx.py decodes the payload as JSON; the shell never parses
        # a JSON string, so its own lines print cwd=-.
        self.assertTrue(line.endswith(" cwd=-"), line)

    def test_log_line_without_a_cwd_prints_a_dash(self):
        self.build_code_index()
        no_cwd = json.dumps({"session_id": "s-refresh", "hook_event_name": "Stop"})
        line, _ = self.run_hook("Stop", payload=no_cwd)
        self.assertIn(" cwd=- ", line + " ")
        line, _ = self.run_hook("Stop", payload=no_cwd, env=self.refresh_env(MEMCONTINUUM_PROJECT="../victim"))
        self.assertTrue(line.endswith(" cwd=-"), line)

    def test_cwd_with_a_lone_surrogate_logs_a_token_not_a_crash(self):
        """INC-0130 re-gate polish: JSON "\\ud800" is a lone surrogate that
        surrogateescape cannot encode; the refresh must not log
        crash-UnicodeEncodeError."""
        self.build_code_index()
        payload = json.dumps({"session_id": "s-refresh", "hook_event_name": "Stop", "cwd": "/x/\ud800/y"})
        line, _ = self.run_hook("Stop", payload=payload)
        self.assertNotIn("crash", line)
        cwd_toks = [t for t in line.split(" ") if t.startswith("cwd=")]
        self.assertEqual(cwd_toks, ["cwd=/x/%ED%A0%80/y"], line)
        self.assertEqual(memidx._log_path_token("/a\udc80\ud800"), "/a%80%ED%A0%80")

    def test_cwd_control_characters_and_fields_cannot_split_or_forge_the_log_line(self):
        self.build_code_index()
        raw = "/x/y\nforged outcome=refreshed\tz project=other session=fake"
        hostile = json.dumps({"session_id": "s-refresh", "hook_event_name": "Stop", "cwd": raw})
        line, _ = self.run_hook("Stop", payload=hostile)
        toks = line.split(" ")
        cwd_toks = [t for t in toks if t.startswith("cwd=")]
        self.assertEqual(len(cwd_toks), 1, line)
        self.assertEqual(urllib.parse.unquote(cwd_toks[0][4:]), raw)
        self.assertEqual([t for t in toks if t.startswith("project=")], [f"project={self.project}"], line)
        self.assertEqual([t for t in toks if t.startswith("session=")], ["session=s-refresh"], line)
        line, _ = self.run_hook("Stop", payload=hostile, env=self.refresh_env(MEMCONTINUUM_PROJECT="../victim"))
        self.assertTrue(line.endswith(" cwd=-"), line)

    def test_a_cwd_with_an_escaped_quote_is_not_mangled_by_the_shell(self):
        self.build_code_index()
        raw = '/x/a"b'
        payload = json.dumps({"session_id": "s-refresh", "hook_event_name": "Stop", "cwd": raw})
        line, _ = self.run_hook("Stop", payload=payload)
        self.assertIn(f" cwd={memidx._log_path_token(raw)} ", line + " ")

    def test_unsafe_project_name_builds_no_path(self):
        env = self.refresh_env(MEMCONTINUUM_PROJECT="../victim")
        line, _ = self.run_hook("Stop", env=env)
        self.assertIn("outcome=skip reason=bad-project", line)
        self.assertEqual(list(Path(self.td).glob("victim*")), [])
        self.assertEqual(list(self.home.glob("*victim*")), [])

    def test_missing_home_directory_is_not_created(self):
        gone = Path(self.td) / "no-such-home"
        proc, _ = run_script(CODE_REFRESH_HOOK, self.payload("Stop"), self.refresh_env(MEMCONTINUUM_HOME=str(gone)))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(gone.exists())

    def test_symlink_marker_is_not_followed_or_replaced(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def s():\n    return 1\n")
        victim = Path(self.td) / "victim.txt"
        victim.write_text("precious\n")
        self.marker.symlink_to(victim)
        line, _ = self.run_hook("SessionStart")
        self.assertIn("marker=write-failed", line)
        self.assertEqual(victim.read_text(), "precious\n")
        self.assertTrue(self.marker.is_symlink())

    def test_stale_marker_temps_are_swept_by_age_and_exact_name(self):
        self.build_code_index()
        old = self.home / f"{self.project}.code-stale.new.4242"
        fresh = self.home / f"{self.project}.code-stale.new.4343"
        lookalike = self.home / f"{self.project}.code-stale.new.4242.bak"
        other = self.home / f"{self.project}.code-stale-keep"
        for f in (old, fresh, lookalike, other):
            f.write_text("x")
        aged = time.time() - 3600
        os.utime(old, (aged, aged))
        os.utime(lookalike, (aged, aged))
        os.utime(other, (aged, aged))
        self.run_hook("Stop")
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), "a fresh temp of an overlapping run is never a candidate")
        self.assertTrue(lookalike.exists())
        self.assertTrue(other.exists())

    def test_no_watchdog_logs_a_skip_and_does_nothing(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def nw():\n    return 1\n")
        env = self.refresh_env(MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh")
        line, _ = self.run_hook("Stop", env=env)
        self.assertIn("outcome=skip reason=no-watchdog", line)
        self.assertNotIn("nw", self.chunk_symbols())

    def test_completes_under_poisoned_pythonpath(self):
        self.build_code_index()
        line, _ = self.run_hook("Stop", env=self.poisoned_base_env(
            MEMCONTINUUM_CODE_ROOTS=json.dumps([str(self.code_root)])))
        self.assertIn("outcome=skip reason=current", line)

    def test_hostile_session_id_cannot_forge_log_keys(self):
        self.build_code_index()
        payload = json.dumps({"session_id": "x outcome=refreshed\nfake=1", "hook_event_name": "Stop"})
        line, _ = self.run_hook("Stop", payload=payload)
        self.assertNotIn("\n", line)
        self.assertEqual(line.count("outcome="), 1, line)

    def test_malformed_payload_still_logs_one_line(self):
        self.build_code_index()
        line, _ = self.run_hook("Stop", payload="{{{ garbage")
        self.assertIn("event=unknown", line)

    # -- fix round 1 (TOP-0137 L2 step 4) ------------------------------------

    def db_fingerprint(self):
        """Every table's name and row count, plus the file bytes' sha256."""
        conn = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        try:
            names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            counts = {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
        finally:
            conn.close()
        return counts, hashlib.sha256(self.db.read_bytes()).hexdigest()

    def set_schema(self, version):
        conn = sqlite3.connect(self.db)
        try:
            if version is None:
                conn.execute("DROP TABLE code_schema")
            else:
                conn.execute("UPDATE code_schema SET version=?", (version,))
            conn.commit()
        finally:
            conn.close()

    def test_old_schema_db_is_never_rebuilt_on_either_event(self):
        for label, version in (("schema-0", None), ("schema-previous", memidx.CODE_SCHEMA_VERSION - 1)):
            for event in ("Stop", "SessionStart"):
                with self.subTest(db=label, event=event):
                    for f in (self.db, self.marker):
                        f.unlink(missing_ok=True)
                    self.build_code_index()
                    (self.code_root / "src" / "mapped.py").write_text(f"def old_{label.replace('-', '_')}_{event}():\n    return 1\n")
                    self.set_schema(version)
                    before = self.db_fingerprint()
                    line, _ = self.run_hook(event)
                    self.assertIn("outcome=skip reason=stale-marked", line)
                    self.assertEqual(self.db_fingerprint(), before, "the old-schema db must be byte-identical")
                    self.assertRegex(self.marker.read_text(),
                                     r"^reason=stale schema-older-than-engine since=\S+ roots=0\n$")

    def test_db_newer_than_engine_fails_without_touching_it(self):
        self.build_code_index()
        self.set_schema(memidx.CODE_SCHEMA_VERSION + 1)
        before = self.db_fingerprint()
        for event in ("Stop", "SessionStart"):
            line, _ = self.run_hook(event)
            self.assertIn("outcome=failed reason=db-newer-than-engine", line)
        self.assertEqual(self.db_fingerprint(), before)

    def test_lock_error_skips_and_never_follows_a_planted_symlink(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def lock_err():\n    return 1\n")
        victim = Path(self.td) / "lock-victim.txt"
        victim.write_text("precious\n")
        self.lock.symlink_to(victim)
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=skip reason=lock-error", line)
        self.assertEqual(victim.read_text(), "precious\n")
        self.assertNotIn("lock_err", self.chunk_symbols())
        self.assertFalse(self.marker.exists())

    def test_missing_root_on_sessionstart_writes_the_degraded_marker(self):
        self.build_code_index()
        self.marker.write_text("reason=refreshing since=2026-01-01T00:00:00+00:00 roots=1\n")
        moved = Path(self.td) / "code-moved"
        self.code_root.rename(moved)
        try:
            line, _ = self.run_hook("SessionStart")
        finally:
            moved.rename(self.code_root)
        self.assertIn("outcome=skip reason=degraded", line)
        self.assertRegex(self.marker.read_text(),
                         r"^reason=degraded missing-root=1 not-indexed=0 since=\S+ roots=1\n$")

    def test_current_preflight_with_a_missing_root_never_clears_the_marker_on_stop(self):
        self.build_code_index()
        moved = Path(self.td) / "code-moved"
        self.code_root.rename(moved)
        try:
            line, _ = self.run_hook("Stop")
        finally:
            moved.rename(self.code_root)
        self.assertIn("outcome=skip reason=degraded", line)
        self.assertTrue(self.marker.exists())

    def test_stop_heal_that_leaves_a_root_missing_is_not_refreshed(self):
        self.build_code_index()
        other = Path(self.td) / "other-indexed"
        (other / "src").mkdir(parents=True)
        (other / "src" / "o.py").write_text("def other_fn():\n    return 0\n")
        git_init(other)
        self.build_code_index(root=other)
        env = self.refresh_env(MEMCONTINUUM_CODE_ROOTS=json.dumps([str(self.code_root), str(other)]))
        line, _ = self.run_hook("Stop", env=env)
        self.assertIn("outcome=skip reason=current", line)
        (self.code_root / "src" / "mapped.py").write_text("def healed_but_degraded():\n    return 1\n")
        gone = Path(self.td) / "other-gone"
        other.rename(gone)
        try:
            line, _ = self.run_hook("Stop", env=env)
        finally:
            gone.rename(other)
        self.assertIn("outcome=failed reason=degraded", line)
        self.assertNotIn("outcome=refreshed", line)
        self.assertIn("healed_but_degraded", self.chunk_symbols())
        self.assertRegex(self.marker.read_text(),
                         r"^reason=degraded missing-root=1 not-indexed=0 since=\S+ roots=2\n$")

    def test_stdin_held_open_does_not_hang_the_hook(self):
        self.build_code_index()
        env = self.refresh_env()
        start = time.monotonic()
        proc = subprocess.Popen([MC_BASH, str(CODE_REFRESH_HOOK)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        try:
            proc.stdin.write(self.payload("Stop", session_id="s-held"))
            proc.stdin.flush()  # the writer stays connected: stdin is NOT closed
            rc = proc.wait(timeout=15)
        finally:
            elapsed = time.monotonic() - start
            if proc.poll() is None:
                proc.kill()
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, 8.0, f"a held-open stdin hung the hook for {elapsed:.1f}s")
        lines = self.log_lines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("event=Stop session=s-held", lines[0])

    def test_marker_unwritable_on_stop_does_not_start_the_heal(self):
        self.build_code_index()
        (self.code_root / "src" / "mapped.py").write_text("def should_not_heal():\n    return 1\n")
        victim = Path(self.td) / "victim.txt"
        victim.write_text("precious\n")
        self.marker.symlink_to(victim)
        line, _ = self.run_hook("Stop")
        self.assertIn("outcome=skip reason=marker-unwritable", line)
        self.assertIn("marker=write-failed", line)
        self.assertNotIn("should_not_heal", self.chunk_symbols())
        self.assertEqual(victim.read_text(), "precious\n")

    def test_watchdog_kill_with_a_missing_home_creates_nothing(self):
        """The shared launcher's kill-line logger (hooks/mc-watchdog.sh) used
        to os.makedirs the home; a hung guarded child under a missing home
        must be killed silently."""
        gone = Path(self.td) / "no-home-at-kill"
        hang = Path(self.td) / "hang.sh"
        ran = Path(self.td) / "child-ran.txt"
        hang.write_text(f"#!/usr/bin/env bash\necho started > {shlex.quote(str(ran))}\nsleep 60\n")
        driver = (
            f'SCRIPT_DIR={shlex.quote(str(HOOKS_DIR))}\n'
            f'source {shlex.quote(str(HOOKS_DIR / "mc-watchdog.sh"))}\n'
            f'MC_WATCHDOG_BUDGET=1 "$MC_GUARD_PY" -c "$MC_WATCHDOG_LAUNCHER_PY" "$BASH" {shlex.quote(str(hang))}\n'
        )
        env = self.refresh_env(MEMCONTINUUM_HOME=str(gone))
        start = time.monotonic()
        proc = subprocess.run([MC_BASH, "-c", driver], env=env, capture_output=True, text=True, timeout=20)
        self.assertLess(time.monotonic() - start, 6.0, proc.stderr)
        self.assertTrue(ran.exists(), "the hanging child never started: the launcher did not run it")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertFalse(gone.exists(), "the kill-line logger must not create a missing home")

    def test_budget_is_normalised_once_by_the_shell(self):
        self.build_code_index()
        out = Path(self.td) / "sweep-age.txt"
        py = self.patched_python(
            f"memidx._sweep_code_stale_temps = lambda m, age: open({str(out)!r}, 'w').write(repr(age))")
        for raw in (".5", "1e-3", "abc"):
            with self.subTest(budget=raw):
                out.unlink(missing_ok=True)
                self.run_hook("Stop", env=self.refresh_env(
                    MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_CODE_REFRESH_BUDGET=raw))
                self.assertEqual(out.read_text(), "16.0", "shell default 8 s, doubled")
        out.unlink(missing_ok=True)
        self.run_hook("Stop", env=self.refresh_env(MEMCONTINUUM_PYTHON=py, MEMCONTINUUM_CODE_REFRESH_BUDGET="3"))
        self.assertEqual(out.read_text(), "6.0")

    def test_sweep_age_fallback_is_twice_the_default_budget(self):
        """Write-safety rule 5: no usable budget counts as 30 s, so 60 s."""
        self.assertEqual(memidx._code_refresh_sweep_age(None), 60.0)
        self.assertEqual(memidx._code_refresh_sweep_age(0), 60.0)
        self.assertEqual(memidx._code_refresh_sweep_age(8.0), 16.0)

    def test_git_listing_timeout_is_half_the_watchdog_budget(self):
        self.build_code_index()
        out = Path(self.td) / "git-timeouts.txt"
        src = (
            "import subprocess as _sp\n"
            "_orig = _sp.run\n"
            "def _rec(*a, **k):\n"
            "    if a and a[0][:1] == ['git']:\n"
            "        for _n in ('ls-files', 'check-ignore', 'rev-parse'):\n"
            "            if _n in a[0]:\n"
            f"                open({str(out)!r}, 'a').write(_n + ' ' + repr(k.get('timeout')) + '\\n')\n"
            "    return _orig(*a, **k)\n"
            "memidx.subprocess.run = _rec\n"
        )
        py = self.patched_python(src)

        def split(text):
            listing, revparse = set(), set()
            for line in text.splitlines():
                name, _, val = line.partition(" ")
                (revparse if name == "rev-parse" else listing).add(val)
            return listing, revparse

        for event, want_list, want_rev in (("SessionStart", "1.0", "1.0"), ("Stop", "4.0", "2.0")):
            if out.exists():
                out.unlink()
            self.run_hook(event, env=self.refresh_env(MEMCONTINUUM_PYTHON=py))
            text = out.read_text()
            listing, revparse = split(text)
            self.assertTrue(revparse, f"{event}: no rev-parse call recorded: {text!r}")
            self.assertEqual(listing, {want_list}, f"{event}: {text}")
            self.assertEqual(revparse, {want_rev}, f"{event}: {text}")

    def test_git_head_sha_timeout_is_capped_at_2s_and_follows_a_lower_ls_timeout(self):
        done = mock.Mock(returncode=0, stdout="abc\n")
        for env_val, want in ((None, 2.0), ("1", 1.0)):
            with mock.patch.dict(os.environ):
                os.environ.pop("MEMCONTINUUM_GIT_LS_TIMEOUT", None)
                if env_val is not None:
                    os.environ["MEMCONTINUUM_GIT_LS_TIMEOUT"] = env_val
                with mock.patch.object(memidx.subprocess, "run", return_value=done) as run:
                    self.assertEqual(memidx._git_head_sha(Path(self.td)), "abc")
                self.assertEqual(run.call_args.kwargs["timeout"], want, env_val)

    def test_git_ls_timeout_helper_defaults_and_normalises(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("MEMCONTINUUM_GIT_LS_TIMEOUT", None)
            self.assertEqual(memidx._git_ls_timeout(), 5.0)
            for raw, want in (("1.5", 1.5), ("junk", 5.0), ("0", 5.0), ("-2", 5.0), ("nan", 5.0), ("inf", 5.0)):
                os.environ["MEMCONTINUUM_GIT_LS_TIMEOUT"] = raw
                self.assertEqual(memidx._git_ls_timeout(), want, raw)

    def test_truncated_payload_prefix_still_yields_the_event(self):
        self.build_code_index()
        big = json.dumps({"session_id": "s-big", "hook_event_name": "Stop", "last_assistant_message": "x" * 9000})
        line, _ = self.run_hook("Stop", payload=big)
        self.assertIn("event=Stop session=s-big", line)

    def test_sessionstart_remind_surfaces_the_marker_then_stops(self):
        env = self.base_env()
        self.marker.write_text("reason=reindex-failed rc=3 since=2026-10-22T10:00:00+00:00 roots=1\n")
        proc, _ = run_script(SESSIONSTART_HOOK, self.session_start_payload("s-cs-1", source="startup"), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn(
            f"code index for {self.project} may be stale: reindex-failed rc=3 since 2026-10-22T10:00:00+00:00 "
            "-- memidx.py code-reindex refreshes it", ctx)
        self.assertIn("code-stale=shown", (self.home / "hook.log").read_text())
        self.marker.unlink()
        proc, _ = run_script(SESSIONSTART_HOOK, self.session_start_payload("s-cs-2", source="startup"), env)
        self.assertEqual(proc.stdout, "")

    def test_sessionstart_remind_ignores_an_unparseable_marker(self):
        self.marker.write_text("garbage\n")
        proc, _ = run_script(SESSIONSTART_HOOK, self.session_start_payload("s-cs-3", source="startup"), self.base_env())
        self.assertEqual(proc.stdout, "")


# ---------------------------------------------------------------------------
# 6b. R5 (TOP-0123 L5): write-side hooks see every code root
# ---------------------------------------------------------------------------


class TestMultiRootWriteSide(HookTestBase):
    """Design R5 (audit MC-P1-05, TOP-0123 L5): the five write-side hooks
    receive every configured code root (JSON list, physical paths), not
    just the first; ledger rows carry which physical root they matched;
    `mc_code_roots`/`mc_extract_fields` (memlib.sh) gain the tokens the
    hooks below need. `self.code_root` (HookTestBase) is root A;
    `self.code_root_b` is a second, sibling git repo -- root B."""

    def setUp(self):
        super().setUp()
        self.code_root_b = Path(self.td) / "code-b"
        self.code_root_b.mkdir()
        _write(self.code_root_b / "src" / "b.py", "# b\n")
        git_init(self.code_root_b)

    def multiroot_env(self, **overrides):
        roots = [str(self.code_root.resolve()), str(self.code_root_b.resolve())]
        env = self.base_env(
            MEMCONTINUUM_CODE_ROOT=roots[0],
            MEMCONTINUUM_CODE_ROOTS=json.dumps(roots),
        )
        env.update(overrides)
        return env

    def _memidx_argv_shim(self):
        """A MEMCONTINUUM_PYTHON replacement that transparently forwards to
        the real venv python (same technique as the existing hang_py/slow_py
        shims elsewhere in this file) but first appends the FULL argv to a
        log file whenever one of the args names memidx.py -- a spy, not a
        stub: the real memidx.py still runs and the hook still gets real
        output, so this proves how many times/with what args it was called
        without reimplementing any of its logic."""
        argv_log = Path(self.td) / "argv.log"
        shim = Path(self.td) / "memidx-argv-shim.sh"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *memidx.py) printf '%s\\n' \"$*\" >> \"$MC_TEST_ARGV_LOG\" ;;\n"
            "  esac\n"
            "done\n"
            "exec \"$MC_TEST_REAL_PYTHON\" \"$@\"\n"
        )
        shim.chmod(0o755)
        return shim, argv_log

    # -- 2: ledger rows carry root/source; same relative path in two roots
    #       does not collide; a path under neither root is out-of-scope ----

    def test_ledger_row_under_second_root_carries_root_and_source(self):
        session_id = "s-multiroot-ledger-b"
        fpath = str(self.code_root_b / "src" / "b.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, _ = run_script(LEDGER_HOOK, payload, self.multiroot_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        self.assertEqual(len(state["ledger"]), 1, state["ledger"])
        entry = state["ledger"][0]
        self.assertEqual(entry["path"], fpath)
        self.assertEqual(entry["kind"], "code")
        self.assertEqual(entry["root"], str(self.code_root_b.resolve()))
        self.assertEqual(entry["source"], "tool")

    def test_ledger_same_relative_path_under_two_roots_yields_two_rows(self):
        session_id = "s-multiroot-ledger-samerel"
        _write(self.code_root_b / "src" / "mapped.py", "# also mapped\n")
        fpath_a = str(self.code_root / "src" / "mapped.py")
        fpath_b = str(self.code_root_b / "src" / "mapped.py")
        run_script(LEDGER_HOOK, self.post_tool_use_payload(session_id, fpath_a), self.multiroot_env())
        run_script(LEDGER_HOOK, self.post_tool_use_payload(session_id, fpath_b), self.multiroot_env())
        state = self.load_state(session_id)
        by_path = {e["path"]: e for e in state["ledger"]}
        self.assertEqual(set(by_path), {fpath_a, fpath_b})
        self.assertEqual(by_path[fpath_a]["root"], str(self.code_root.resolve()))
        self.assertEqual(by_path[fpath_b]["root"], str(self.code_root_b.resolve()))

    def test_ledger_store_row_has_empty_root(self):
        session_id = "s-multiroot-ledger-store"
        fpath = str(self.store_root / "topics" / "testing" / "mapped-topic.md")
        payload = self.post_tool_use_payload(session_id, fpath, tool_name="Write")
        proc, _ = run_script(LEDGER_HOOK, payload, self.multiroot_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == fpath][0]
        self.assertEqual(entry["kind"], "store")
        self.assertEqual(entry.get("root"), "")

    def test_ledger_row_under_nested_root_uses_longest_match(self):
        """Ruling 131 (coordinator, folded from task-7-review.md NIT-1): the
        ledger's own containment check picks the LONGEST containing root
        when roots nest, the same rule memidx.py's `unmapped --code-root`
        uses (`_unmapped_best_root`) -- so the ledger's `root` annotation
        always names the same root a later `unmapped` classification would
        use for the same path (before this fix the ledger picked whichever
        root came FIRST in `mc_code_roots`' emission order instead)."""
        session_id = "s-multiroot-nested"
        outer = self.code_root  # root A
        inner = self.code_root / "nested-b"  # root B, physically inside A
        inner.mkdir()
        _write(inner / "src" / "inner.py", "# inner\n")
        git_init(inner)

        env = self.multiroot_env(
            MEMCONTINUUM_CODE_ROOTS=json.dumps([str(outer.resolve()), str(inner.resolve())]),
        )
        fpath = str(inner / "src" / "inner.py")
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, _ = run_script(LEDGER_HOOK, payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        entry = [e for e in state["ledger"] if e["path"] == fpath][0]
        self.assertEqual(entry["root"], str(inner.resolve()), state["ledger"])

    def test_ledger_path_under_neither_root_is_out_of_scope(self):
        session_id = "s-multiroot-ledger-outside"
        fpath = "/tmp/somewhere/else/multiroot-nowhere.py"
        payload = self.post_tool_use_payload(session_id, fpath)
        proc, _ = run_script(LEDGER_HOOK, payload, self.multiroot_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        state = self.load_state(session_id)
        paths = [e["path"] for e in state.get("ledger", [])]
        self.assertNotIn(fpath, paths)

    # -- memlib.sh: mc_code_roots / mc_extract_fields's new tokens ----------

    def test_mc_code_roots_prints_every_root_and_falls_back_to_single(self):
        caller = Path(self.td) / "code-roots-caller.sh"
        caller.write_text(f'#!/usr/bin/env bash\nset -u\nsource "{MEMLIB}"\nmc_code_roots\n')

        proc = subprocess.run(
            [MC_BASH, str(caller)], capture_output=True, text=True, env=self.multiroot_env(), timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [l for l in proc.stdout.splitlines() if l]
        self.assertEqual(lines, [str(self.code_root.resolve()), str(self.code_root_b.resolve())])

        # Falls back to the single MEMCONTINUUM_CODE_ROOT when the list
        # variable is unset (old-shape wiring, or a hand-written config).
        proc2 = subprocess.run(
            [MC_BASH, str(caller)], capture_output=True, text=True, env=self.base_env(), timeout=10,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        lines2 = [l for l in proc2.stdout.splitlines() if l]
        self.assertEqual(lines2, [str(self.code_root)])

    def test_mc_extract_fields_gains_notebook_path_token(self):
        """`tool_input.notebook_path` is a NEW special token (Task 8 uses it,
        added here so that task does not need to touch memlib.sh itself).
        `tool_name` needs no code change at all -- it is already a plain
        top-level key, handled by mc_extract_fields's existing generic
        branch -- this same call proves that too."""
        caller = Path(self.td) / "extract-fields-caller.sh"
        caller.write_text(
            f'#!/usr/bin/env bash\nset -u\nsource "{MEMLIB}"\n'
            'mc_extract_fields "$1" tool_name tool_input.notebook_path\n'
        )
        payload = json.dumps({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "/x/nb.ipynb"}})
        proc = subprocess.run(
            [MC_BASH, str(caller), payload], capture_output=True, text=True, env=self.base_env(), timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TOOL_NAME=NotebookEdit", proc.stdout)
        self.assertIn("NOTEBOOK_PATH=/x/nb.ipynb", proc.stdout)

    # -- 4/5: userprompt-remind.sh / precompact-persist.sh see every root ---

    def test_userprompt_remind_multiroot_coverage_and_head_changed(self):
        session_id = "s-multiroot-userprompt"
        fpath_a = str(self.code_root / "src" / "unmapped.py")
        fpath_b = str(self.code_root_b / "src" / "b.py")
        self.seed_ledger(session_id, [(fpath_a, "code"), (fpath_b, "code")])
        state = self.load_state(session_id)
        state["start_code_shas"] = {
            str(self.code_root.resolve()): git_head(self.code_root),
            str(self.code_root_b.resolve()): git_head(self.code_root_b),
        }
        self.state_file(session_id).write_text(json.dumps(state))

        # Advance root B's HEAD only.
        _write(self.code_root_b / "src" / "new.py", "# new\n")
        subprocess.run(["git", "add", "-A"], cwd=self.code_root_b, check=True)
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                        "commit", "-q", "-m", "b change"], cwd=self.code_root_b, check=True)

        shim, argv_log = self._memidx_argv_shim()
        env = self.multiroot_env(
            MEMCONTINUUM_PYTHON=str(shim), MC_TEST_REAL_PYTHON=VENV_PYTHON, MC_TEST_ARGV_LOG=str(argv_log),
        )
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), env, timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue(argv_log.exists(), proc.stdout + proc.stderr)
        argv_lines = [l for l in argv_log.read_text().splitlines() if " unmapped " in l]
        self.assertEqual(len(argv_lines), 1, argv_log.read_text())
        line = argv_lines[0]
        self.assertEqual(line.count("--code-root"), 2, line)
        self.assertIn(f"--code-root {self.code_root.resolve()}", line)
        self.assertIn(f"--code-root {self.code_root_b.resolve()}", line)

        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("src/unmapped.py", ctx)
        self.assertIn("src/b.py", ctx)
        self.assertIn("code HEAD changed: yes", ctx)

    def test_precompact_persist_multiroot_coverage_and_head_changed(self):
        session_id = "s-multiroot-precompact"
        fpath_a = str(self.code_root / "src" / "unmapped.py")
        fpath_b = str(self.code_root_b / "src" / "b.py")
        self.seed_ledger(session_id, [(fpath_a, "code"), (fpath_b, "code")])
        state = self.load_state(session_id)
        state["start_code_shas"] = {
            str(self.code_root.resolve()): git_head(self.code_root),
            str(self.code_root_b.resolve()): git_head(self.code_root_b),
        }
        self.state_file(session_id).write_text(json.dumps(state))

        # Advance root B's HEAD only.
        _write(self.code_root_b / "src" / "new.py", "# new\n")
        subprocess.run(["git", "add", "-A"], cwd=self.code_root_b, check=True)
        subprocess.run(["git", "-c", "user.email=a@b.c", "-c", "user.name=a",
                        "commit", "-q", "-m", "b change"], cwd=self.code_root_b, check=True)

        shim, argv_log = self._memidx_argv_shim()
        env = self.multiroot_env(
            MEMCONTINUUM_PYTHON=str(shim), MC_TEST_REAL_PYTHON=VENV_PYTHON, MC_TEST_ARGV_LOG=str(argv_log),
        )
        proc, _ = run_script(PRECOMPACT_HOOK, self.pre_compact_payload(session_id), env, timeout=10.0)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue(argv_log.exists(), proc.stdout + proc.stderr)
        argv_lines = [l for l in argv_log.read_text().splitlines() if " unmapped " in l]
        self.assertEqual(len(argv_lines), 1, argv_log.read_text())
        line = argv_lines[0]
        self.assertEqual(line.count("--code-root"), 2, line)
        self.assertIn(f"--code-root {self.code_root.resolve()}", line)
        self.assertIn(f"--code-root {self.code_root_b.resolve()}", line)

        pending = self.load_state(session_id)["pending"]
        self.assertIn("src/unmapped.py", pending["unmapped"])
        self.assertIn("src/b.py", pending["unmapped"])
        self.assertTrue(pending["code_head_changed"])

    # -- LOW-4 (task-7-review.md): a root ABSENT from start_code_shas falls
    #    back to legacy_start ONLY when it IS the first configured root ----

    def test_userprompt_remind_root_missing_from_start_map_is_not_falsely_changed(self):
        """A root missing from `start_code_shas` (not "no commits yet" --
        genuinely absent, e.g. the transitional window before a resume
        repopulates the map) must be treated as unknown/skipped, never
        compared against a DIFFERENT root's start sha -- root B's actual
        current HEAD can never equal root A's start sha (two unrelated git
        repos), so the pre-fix fallback always misread this as "changed:
        yes"."""
        session_id = "s-multiroot-lowfour-userprompt"
        fpath_a = str(self.code_root / "src" / "unmapped.py")
        self.seed_ledger(session_id, [(fpath_a, "code")])
        state = self.load_state(session_id)
        state["start_code_sha"] = git_head(self.code_root)
        state["start_code_shas"] = {str(self.code_root.resolve()): git_head(self.code_root)}
        self.state_file(session_id).write_text(json.dumps(state))

        proc, _ = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.multiroot_env(), timeout=10.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("code HEAD changed: no", ctx, ctx)

    def test_precompact_persist_root_missing_from_start_map_is_not_falsely_changed(self):
        session_id = "s-multiroot-lowfour-precompact"
        fpath_a = str(self.code_root / "src" / "unmapped.py")
        self.seed_ledger(session_id, [(fpath_a, "code")])
        state = self.load_state(session_id)
        state["start_code_sha"] = git_head(self.code_root)
        state["start_code_shas"] = {str(self.code_root.resolve()): git_head(self.code_root)}
        self.state_file(session_id).write_text(json.dumps(state))

        proc, _ = run_script(
            PRECOMPACT_HOOK, self.pre_compact_payload(session_id), self.multiroot_env(), timeout=10.0,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        pending = self.load_state(session_id)["pending"]
        self.assertFalse(pending["code_head_changed"], pending)

    # -- 5: sessionstart-remind.sh records start_code_shas for every root ---

    def test_sessionstart_remind_records_start_code_shas_for_both_roots(self):
        session_id = "s-multiroot-sessionstart"
        proc, _ = run_script(
            SESSIONSTART_HOOK, self.session_start_payload(session_id, "startup"), self.multiroot_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        state = self.load_state(session_id)
        self.assertEqual(state.get("start_code_sha"), git_head(self.code_root))
        self.assertEqual(
            state.get("start_code_shas"),
            {
                str(self.code_root.resolve()): git_head(self.code_root),
                str(self.code_root_b.resolve()): git_head(self.code_root_b),
            },
        )


# ---------------------------------------------------------------------------
# 7. memlib.sh exists / is sourceable
# ---------------------------------------------------------------------------


class TestMemlib(unittest.TestCase):
    def test_memlib_exists_and_is_valid_bash(self):
        self.assertTrue(MEMLIB.exists())
        result = subprocess.run([MC_BASH, "-n", str(MEMLIB)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_memlib_never_writes_under_a_store_root(self):
        text = MEMLIB.read_text()
        self.assertNotIn("MEMCONTINUUM_ROOT\" >", text)
        self.assertNotIn(">\"$MEMCONTINUUM_ROOT", text)

    def test_mc_log_appends_project(self):
        """Liveness metric fix: mc_log (the shared logging path every
        memlib.sh-sourcing hook uses) must append project=<MC_PROJECT> to
        every line it writes.

        Round-2 Codex gate finding: the old version of this test asserted
        neither the subprocess's return code nor the SPECIFIC "probe
        outcome=ok" line -- it only checked that "project=mclog-proj"
        appeared SOMEWHERE in hook.log. If MEMCONTINUUM_PYTHON doesn't
        resolve in the caller's environment, memlib.sh's own "no python
        resolved" diagnostic (sourced before mc_log is even called) ALSO
        carries project=mclog-proj -- so that line alone could satisfy the
        old assertion even if `mc_log` itself were broken or had stopped
        adding the field entirely. Explicit MEMCONTINUUM_PYTHON (so the
        "no python resolved" line never fires) plus an exact-line
        assertion closes that gap."""
        td = tempfile.mkdtemp(prefix="memcontinuum-memlib-mclog-")
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        home = Path(td) / "home"
        home.mkdir()
        caller = Path(td) / "caller.sh"
        caller.write_text(
            f'#!/usr/bin/env bash\nset -u\nsource "{MEMLIB}"\nmc_log "probe outcome=ok"\n'
        )
        env = clean_env(
            MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_PROJECT="mclog-proj",
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        proc = subprocess.run([MC_BASH, str(caller)], capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertNotIn("no python resolved", log_text, "MEMCONTINUUM_PYTHON was set -- this must never fire")
        matching = [l for l in log_text.splitlines() if "probe outcome=ok" in l]
        self.assertEqual(len(matching), 1, log_text)
        self.assertIn("project=mclog-proj", matching[0])

    def test_no_python_resolved_line_carries_project(self):
        """The fail-open 'no python resolved' diagnostic (memlib.sh, printed
        BEFORE mc_log even exists) must also carry project= -- it is a real
        hook.log line and would otherwise silently escape the liveness
        metric's 'EVERY line carries project=' guarantee."""
        td = tempfile.mkdtemp(prefix="memcontinuum-memlib-nopy-")
        self.addCleanup(shutil.rmtree, td, ignore_errors=True)
        home = Path(td) / "home"
        home.mkdir()
        caller = Path(td) / "caller.sh"
        caller.write_text(f'#!/usr/bin/env bash\nset -u\nsource "{MEMLIB}"\n')
        env = clean_env(
            MEMCONTINUUM_HOME=str(home),
            MEMCONTINUUM_PROJECT="nopy-proj",
            MEMCONTINUUM_PYTHON="/no/such/python",
        )
        subprocess.run([MC_BASH, str(caller)], capture_output=True, text=True, env=env, timeout=10)
        log_text = (home / "hook.log").read_text()
        self.assertIn("no python resolved", log_text)
        self.assertIn("project=nopy-proj", log_text)


# ---------------------------------------------------------------------------
# 6b. mc_rotate_hook_log (hooks/memlib.sh) -- direct unit coverage
#
# eval-topic-logging section 5 (owner-approved add-on): nothing used to
# truncate or prune hook.log (mc_prune_old_state only ever clears session-
# state JSON). Measured on a real machine: 1.77 MB / 10,481 lines over ten
# days, ~177 KB/day -- unbounded growth, and every `memidx.py stats` run
# reads the whole file. mc_rotate_hook_log is the rotation primitive;
# TestSessionStartHookLogRotation below is the end-to-end proof that
# sessionstart-remind.sh actually calls it at the right (and only the
# right) session boundary.
# ---------------------------------------------------------------------------


class TestMcRotateHookLog(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-rotate-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.home = Path(self.td) / "home"
        self.home.mkdir()

    def _call_rotate(self, **env_overrides):
        caller = Path(self.td) / "rotate-caller.sh"
        caller.write_text(
            f'#!/usr/bin/env bash\nset -u\nsource "{MEMLIB}"\nmc_rotate_hook_log\necho "RC=$?"\n'
        )
        env = clean_env(
            MEMCONTINUUM_HOME=str(self.home),
            MEMCONTINUUM_PROJECT="rotate-proj",
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        env.update(env_overrides)
        return subprocess.run(
            [MC_BASH, str(caller)], capture_output=True, text=True, env=env, timeout=10
        )

    def test_below_threshold_never_rotates(self):
        hook_log = self.home / "hook.log"
        hook_log.write_text("small\n")
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="1000000")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout, proc.stdout)
        self.assertFalse((self.home / "hook.log.1").exists())
        self.assertEqual(hook_log.read_text(), "small\n")

    def test_above_threshold_rotates_content_byte_for_byte_and_new_log_starts_empty(self):
        hook_log = self.home / "hook.log"
        content = "x" * 5000
        hook_log.write_text(content)
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="100")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout, proc.stdout)
        rotated = self.home / "hook.log.1"
        self.assertTrue(rotated.exists(), "hook.log.1 must exist once the threshold is crossed")
        self.assertEqual(rotated.read_text(), content)
        self.assertTrue(hook_log.exists(), "a fresh hook.log must exist right after rotation")
        self.assertEqual(hook_log.read_text(), "", "the new hook.log must start empty")

    def test_no_hook_log_at_all_is_a_silent_no_op(self):
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout, proc.stdout)
        self.assertFalse((self.home / "hook.log").exists())
        self.assertFalse((self.home / "hook.log.1").exists())

    def test_default_threshold_is_five_mib_when_env_unset(self):
        hook_log = self.home / "hook.log"
        hook_log.write_text("y" * (5 * 1024 * 1024 - 10))  # just under the documented default
        proc = self._call_rotate()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((self.home / "hook.log.1").exists())

    def test_keep_1_reproduces_original_replace_dot_1_never_creates_dot_2(self):
        """hook.log: bounded retention of N rotated files --
        MEMCONTINUUM_LOG_KEEP=1 must reproduce the pre-feature, two-
        file-total policy byte-for-byte: each rotation replaces
        hook.log.1 in place and no hook.log.2 is ever created."""
        hook_log = self.home / "hook.log"
        hook_log.write_text("first\n" * 100)
        self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="1")
        rotated = self.home / "hook.log.1"
        self.assertEqual(rotated.read_text(), "first\n" * 100)

        hook_log.write_text("second\n" * 100)
        self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="1")
        self.assertEqual(rotated.read_text(), "second\n" * 100)
        self.assertFalse((self.home / "hook.log.2").exists())

    def test_default_keep_is_twelve_when_env_unset(self):
        """No MEMCONTINUUM_LOG_KEEP set -- 13 rotations must retain exactly
        .1 .. .12 (pinning the default at 12, not merely "more than one")
        and drop the oldest (marker-1) the moment a 13th would-be file
        would exist."""
        hook_log = self.home / "hook.log"
        for i in range(1, 14):
            hook_log.write_text(f"marker-{i}\n" + ("x" * 100 + "\n") * 5)
            proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10")
            self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertFalse((self.home / "hook.log.13").exists())
        self.assertIn("marker-13\n", (self.home / "hook.log.1").read_text())
        self.assertIn(
            "marker-2\n", (self.home / "hook.log.12").read_text(),
            "default MEMCONTINUUM_LOG_KEEP must be 12: the 13th rotation's "
            "shift must still carry marker-2 (the 2nd write) into .12",
        )
        for n in range(1, 13):
            self.assertNotIn(
                "marker-1\n", (self.home / f"hook.log.{n}").read_text(),
                f"marker-1 (the 1st write, now the 13th-oldest) must be gone from .{n}",
            )

    def test_keep_3_shifts_across_four_rotations_and_drops_the_oldest(self):
        """hook.log: bounded retention of N rotated files -- KEEP=3 across
        four session-start rotations must produce exactly .1 .2 .3 (each
        with its own content, verified by a marker line), and the content
        from the very first rotation (now older than the retained window)
        must be gone."""
        hook_log = self.home / "hook.log"
        markers = ["marker-A", "marker-B", "marker-C", "marker-D"]
        for marker in markers:
            hook_log.write_text(f"{marker}\n" + ("x" * 100 + "\n") * 5)
            proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="3")
            self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertFalse((self.home / "hook.log.4").exists())
        # Newest rotated content (marker-D, the 4th write) is never rotated
        # in this loop -- the rotation call after writing marker-D moves
        # marker-D's own content to .1; marker-C ends up at .2, marker-B at
        # .3, and marker-A (the oldest, now past the KEEP=3 window) is gone.
        self.assertIn("marker-D", (self.home / "hook.log.1").read_text())
        self.assertIn("marker-C", (self.home / "hook.log.2").read_text())
        self.assertIn("marker-B", (self.home / "hook.log.3").read_text())
        for p in (self.home / "hook.log.1", self.home / "hook.log.2", self.home / "hook.log.3"):
            self.assertNotIn("marker-A", p.read_text())

    def test_keep_out_of_range_or_malformed_falls_back_to_default_cleanly(self):
        """Round-2 review MAJOR: bash's own arithmetic expansion treats a
        leading-zero KEEP as octal -- KEEP=08 is an invalid octal digit and
        would abort mid-shift with the claim already made (stranding the
        just-claimed content as a permanent hook.log.rotating.<pid> orphan,
        since the arithmetic error aborts the enclosing script before the
        landing step ever runs) -- and an unbounded KEEP (e.g. 999999999)
        would spin the shift loop toward a de-facto-infinite SessionStart
        hang. Every one of these must instead fall back to the documented
        default (12) and complete the rotation cleanly: RC=0, content
        landed at .1, and -- the specific regression this guards -- no
        hook.log.rotating.<pid> orphan left behind. (KEEP=1001 gets its
        own dedicated test below, with a sharper observable than this
        loop's -- a single rotation here can't distinguish "fell back to
        12" from "used 1001 literally", since both land the same content
        at .1.)"""
        for bad_keep in ("0", "08", "012", "abc", "999999999", "-1"):
            with self.subTest(keep=bad_keep):
                # round-2 review NIT: this cleanup must run even when a
                # subTest's own asserts above raise -- unittest's subTest
                # catches the failure and moves on to the NEXT bad_keep
                # value, but a bare (non-finally) cleanup below the asserts
                # would then never run for the failing iteration, leaving
                # its leftover files to poison every iteration after it.
                try:
                    hook_log = self.home / "hook.log"
                    hook_log.write_text("first\n" * 100)
                    proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP=bad_keep)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("RC=0", proc.stdout, proc.stdout)
                    self.assertEqual((self.home / "hook.log.1").read_text(), "first\n" * 100)
                    self.assertEqual(
                        list(self.home.glob("hook.log.rotating.*")), [],
                        f"KEEP={bad_keep!r} must not strand a .rotating.<pid> orphan",
                    )
                finally:
                    for p in self.home.iterdir():
                        p.unlink()

    def test_keep_1001_falls_back_to_twelve_via_prune_probe(self):
        """KEEP=1001 sits just outside the documented [1, 1000] range and
        must fall back to 12 -- pinned via the prune step's own observable
        effect (round-2 review NIT: don't lean on the 999999999 case's
        de-facto-hang timeout to also cover this boundary) rather than a
        huge, slow loop of real rotations. Pre-seed .12 and .13, then
        rotate once with KEEP=1001: if KEEP truly fell back to 12, the
        prune loop starts at .13 (keep+1) and removes it while leaving
        .12 (still within a 12-file bound) alone; if the literal 1001
        were used instead, the prune loop would start at .1002 and never
        touch either file, leaving .13 behind too."""
        (self.home / "hook.log.12").write_text("twelve\n")
        (self.home / "hook.log.13").write_text("thirteen\n")
        hook_log = self.home / "hook.log"
        hook_log.write_text("first\n" * 100)
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="1001")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            (self.home / "hook.log.12").read_text(), "twelve\n",
            "KEEP=1001 must fall back to 12 -- .12 must be left alone",
        )
        self.assertFalse(
            (self.home / "hook.log.13").exists(),
            "KEEP=1001 must fall back to 12 -- .13 (keep+1) must be pruned",
        )

    def test_keep_leading_zero_falls_back_to_twelve_not_its_octal_value(self):
        """KEEP=012 is the specific silent-wrong-number case (round-2
        review MAJOR): bash's arithmetic expansion reads it as octal 12,
        i.e. decimal 10, NOT the documented default of 12 -- a bug that
        only a two-digit-suffix, many-rotations probe can actually
        distinguish from a correctly-defaulting 12 (fewer rotations look
        identical either way). 11 rotations with KEEP="012": if the octal
        bug were still present (effective KEEP=10), the 11th rotation's
        shift would drop marker-1 (the oldest) one rotation early; with
        the fix (falls back to 12), nothing is dropped yet and marker-1
        must still be present at .11."""
        hook_log = self.home / "hook.log"
        for i in range(1, 12):
            hook_log.write_text(f"marker-{i}\n" + ("x" * 100 + "\n") * 5)
            proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="012")
            self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertIn("marker-11\n", (self.home / "hook.log.1").read_text())
        self.assertIn(
            "marker-1\n", (self.home / "hook.log.11").read_text(),
            "KEEP=\"012\" must fall back to the default of 12, not octal 10 -- "
            "marker-1 must survive 11 rotations either way",
        )

    def test_lowering_keep_prunes_files_beyond_the_new_bound(self):
        """Round-2 review MINOR (both reviewers): lowering
        MEMCONTINUUM_LOG_KEEP after files beyond the new bound already
        exist must actually enforce the smaller bound, not just stop
        adding to the old one. KEEP=4 across four rotations fills
        .1 .. .4; a fifth rotation with KEEP=2 must land exactly .1 .2 and
        remove .3 and .4 outright."""
        hook_log = self.home / "hook.log"
        for marker in ("marker-1", "marker-2", "marker-3", "marker-4"):
            hook_log.write_text(f"{marker}\n" + ("x" * 100 + "\n") * 5)
            proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="4")
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.home / "hook.log.4").exists(), "setup: KEEP=4 must have filled .1..4")

        hook_log.write_text("marker-5\n" + ("x" * 100 + "\n") * 5)
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="2")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertIn("marker-5\n", (self.home / "hook.log.1").read_text())
        self.assertIn("marker-4\n", (self.home / "hook.log.2").read_text())
        self.assertFalse((self.home / "hook.log.3").exists(), "KEEP=2 must prune the old .3")
        self.assertFalse((self.home / "hook.log.4").exists(), "KEEP=2 must prune the old .4")

    def test_interrupted_shift_recovers_orphan_without_dropping_an_in_window_file(self):
        """Round-2 review MINOR (both reviewers, Grok's worked sequence):
        sessionstart-remind.sh's 2s watchdog can kill mc_rotate_hook_log
        AFTER a shift has fully completed but BEFORE that shift's own
        claimed content lands at .1 -- this constructs exactly that on-disk
        state directly (rather than racing a real SIGKILL against two
        specific mv calls, which the primitive's own comment already
        proves is not timing-sensitive: the .1-missing check makes the
        shift idempotent regardless of how many of its steps already ran).
        KEEP=5 here gives plenty of headroom for all four real windows
        (the new claim, the recovered orphan, and the two already-shifted
        files) so NOTHING here is mathematically forced to be dropped --
        unlike this method's KEEP=3 sibling below, any loss here is purely
        the bug, not arithmetic. The pre-fix bug drops marker-B anyway (a
        naive re-shift, confused by the .1 hole, moves marker-A on top of
        it) and leaves marker-D a permanent, invisible orphan; the fix
        recovers marker-D into the chain ahead of marker-A and marker-B,
        with the orphan file gone and nothing lost."""
        # KEEP=5 was already home to marker-A/marker-B at .2/.3 (.1 empty).
        # A rotation claiming marker-D ran its shift to full completion
        # (marker-B -> .3, marker-A -> .2, nothing dropped -- five slots was
        # room enough) and was killed before landing marker-D itself.
        (self.home / "hook.log.2").write_text("marker-A\n")
        (self.home / "hook.log.3").write_text("marker-B\n")
        dead_pid = "999999999"  # far past any real pid_max -- kill -0 always fails
        orphan = self.home / f"hook.log.rotating.{dead_pid}"
        orphan.write_text("marker-D\n")
        # hook.log itself is absent, matching the real post-claim state.

        hook_log = self.home / "hook.log"
        hook_log.write_text("marker-E\n" + ("x" * 100 + "\n") * 5)
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="5")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertIn("marker-E\n", (self.home / "hook.log.1").read_text())
        self.assertIn(
            "marker-D\n", (self.home / "hook.log.2").read_text(),
            "the recovered orphan (marker-D) must land ahead of marker-A and marker-B",
        )
        self.assertIn("marker-A\n", (self.home / "hook.log.3").read_text())
        self.assertIn(
            "marker-B\n", (self.home / "hook.log.4").read_text(),
            "marker-B must survive: with KEEP=5 and only 4 real windows "
            "(E, D, A, B) nothing needs to be dropped at all",
        )
        self.assertFalse((self.home / "hook.log.5").exists())
        self.assertEqual(
            list(self.home.glob("hook.log.rotating.*")), [],
            "the recovered orphan must not remain on disk under its old name",
        )

    def test_interrupted_shift_recovery_matches_two_sequential_uninterrupted_rotations(self):
        """The same fixture as above, but at KEEP=3 -- the reviewers' own
        worked example. Here there genuinely are more real windows (E, D,
        A, B) than slots (3), so marker-B (the oldest of the four) is
        correctly dropped -- but it must be dropped as the RIGHT victim,
        not an arbitrary one a hole-confused naive re-shift would pick.
        The fixed result must be byte-for-byte what two separate,
        uninterrupted rotations (one landing marker-D, then one landing
        marker-E) would have produced -- proving recovery doesn't merely
        avoid a crash, it reproduces the correct history."""
        (self.home / "hook.log.2").write_text("marker-A\n")
        (self.home / "hook.log.3").write_text("marker-B\n")
        dead_pid = "999999998"
        padded_marker_d = "marker-D\n" + ("x" * 100 + "\n") * 5
        (self.home / f"hook.log.rotating.{dead_pid}").write_text(padded_marker_d)

        hook_log = self.home / "hook.log"
        hook_log.write_text("marker-E\n" + ("x" * 100 + "\n") * 5)
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="3")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # Reference: what an uninterrupted "land D, then land E" sequence
        # produces from the same starting .2=A/.3=B, computed independently
        # via two direct rotation cycles against a fresh sibling home dir
        # (MEMCONTINUUM_HOME overridden per-call, same as _call_rotate's
        # own env_overrides mechanism -- self.home stays the recovery run
        # above, untouched by this reference computation).
        ref_home = Path(self.td) / "reference-home"
        ref_home.mkdir()
        (ref_home / "hook.log.2").write_text("marker-A\n")
        (ref_home / "hook.log.3").write_text("marker-B\n")
        ref_hook_log = ref_home / "hook.log"
        ref_hook_log.write_text("marker-D\n" + ("x" * 100 + "\n") * 5)
        ref_proc1 = self._call_rotate(
            MEMCONTINUUM_HOME=str(ref_home), MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="3",
        )
        self.assertEqual(ref_proc1.returncode, 0, ref_proc1.stderr)
        ref_hook_log.write_text("marker-E\n" + ("x" * 100 + "\n") * 5)
        ref_proc2 = self._call_rotate(
            MEMCONTINUUM_HOME=str(ref_home), MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="3",
        )
        self.assertEqual(ref_proc2.returncode, 0, ref_proc2.stderr)

        for n in (1, 2, 3):
            self.assertEqual(
                (self.home / f"hook.log.{n}").read_text(),
                (ref_home / f"hook.log.{n}").read_text(),
                f".{n} must match the uninterrupted two-rotation reference exactly",
            )
        self.assertFalse((self.home / "hook.log.4").exists())
        self.assertEqual(list(self.home.glob("hook.log.rotating.*")), [])

    def test_interrupted_shift_recovery_never_touches_a_live_pid_orphan(self):
        """The other half of the same fix: a `.rotating.<pid>` file whose
        pid is genuinely still running (another session's claim truly in
        flight right now) must be left completely alone -- only a DEAD
        pid's leftover is a real orphan."""
        live_pid = str(os.getpid())  # this test process itself -- certainly alive
        live_orphan = self.home / f"hook.log.rotating.{live_pid}"
        live_orphan.write_text("still-claiming\n")

        hook_log = self.home / "hook.log"
        hook_log.write_text("marker-1\n" + ("x" * 100 + "\n") * 5)
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10", MEMCONTINUUM_LOG_KEEP="3")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue(live_orphan.exists(), "a live-pid orphan must never be touched")
        self.assertEqual(live_orphan.read_text(), "still-claiming\n")
        self.assertIn("marker-1\n", (self.home / "hook.log.1").read_text())

    def test_orphan_recovery_survives_a_space_in_memcontinuum_home(self):
        """Round-2 review MINOR: `for orphan in $(mc_rotate_orphans_oldest_first)`
        used to word-split (and glob-expand) the command substitution's
        output -- the only such unquoted expansion anywhere in
        hooks/*.sh. Reproduced with the reviewer's own experiment: a
        MEMCONTINUUM_HOME containing a space splits one real orphan path
        into two bogus words, neither of which passes `[ -f "$orphan" ]`,
        so recovery silently no-ops (rc=0, orphan left on disk, .1=E
        .2=A as if the orphan never existed). Fixed via `while read` over
        a heredoc, immune to both word-splitting and globbing -- this
        pins the FIXED result: the orphan lands in the chain (at .2,
        ahead of the pre-existing .1=A which shifts to .3) rather than
        being silently skipped."""
        space_home = Path(self.td) / "c7 with space"
        space_home.mkdir()
        (space_home / "hook.log.1").write_text("marker-A\n")
        dead_pid = "999999997"
        (space_home / f"hook.log.rotating.{dead_pid}").write_text("marker-D\n")
        hook_log = space_home / "hook.log"
        hook_log.write_text("marker-E\n" + ("x" * 100 + "\n") * 5)

        proc = self._call_rotate(MEMCONTINUUM_HOME=str(space_home), MEMCONTINUUM_LOG_MAX_BYTES="10")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertIn("marker-E\n", (space_home / "hook.log.1").read_text())
        self.assertIn(
            "marker-D\n", (space_home / "hook.log.2").read_text(),
            "the orphan must be recovered into the chain even when "
            "MEMCONTINUUM_HOME contains a space -- a word-splitting bug "
            "here would silently skip it instead, leaving marker-A "
            "(not marker-D) at .2",
        )
        self.assertIn("marker-A\n", (space_home / "hook.log.3").read_text())
        self.assertEqual(
            list(space_home.glob("hook.log.rotating.*")), [],
            "no orphan may remain on disk under its old name",
        )

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permission bits")
    def test_unwritable_home_dir_fails_open_leaves_log_alone(self):
        hook_log = self.home / "hook.log"
        content = "x" * 5000
        hook_log.write_text(content)
        self.home.chmod(0o555)  # read+execute only -- mv/rename inside it must fail
        self.addCleanup(self.home.chmod, 0o755)
        proc = self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10")
        self.home.chmod(0o755)  # restore before the assertions below read the directory
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RC=0", proc.stdout, proc.stdout)
        self.assertFalse((self.home / "hook.log.1").exists())
        self.assertEqual(hook_log.read_text(), content, "an unwritable home must leave the log untouched")


# ---------------------------------------------------------------------------
# 7b. mc_path_under_root (hooks/mc-path-lib.sh) -- direct unit coverage
# ---------------------------------------------------------------------------


class TestMcPathUnderRoot(unittest.TestCase):
    """Direct unit coverage for mc_path_under_root (hooks/mc-path-lib.sh),
    the one shared containment primitive hooks/newfile-nudge.sh and
    hooks/ledger-post-edit.sh both call. Previously exercised only
    indirectly through those two hooks' own tests -- symlink-paths review
    round 1, finding 5. Runs under MC_BASH (bash 3.2 too, via
    tests/run_bash32.sh)."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-path-under-root-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

    def _call(self, file_path, root):
        """Runs `mc_path_under_root FILE_PATH ROOT; echo $?` under MC_BASH
        and returns the integer return code. FILE_PATH/ROOT are passed as
        script arguments ($1/$2), never interpolated into the script text,
        so no quoting concerns for either (including one containing a
        literal glob character)."""
        caller = Path(self.td) / "probe.sh"
        caller.write_text(
            f'#!/usr/bin/env bash\nsource "{MC_PATH_LIB}"\n'
            'mc_path_under_root "$1" "$2"\necho $?\n'
        )
        proc = subprocess.run(
            [MC_BASH, str(caller), str(file_path), str(root)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(proc.stderr, "", proc.stderr)
        return int(proc.stdout.strip())

    def test_exact_root_is_under(self):
        root = Path(self.td) / "root"
        root.mkdir()
        self.assertEqual(self._call(root, root), 0)

    def test_a_child_of_root_is_under(self):
        root = Path(self.td) / "root"
        (root / "sub").mkdir(parents=True)
        target = root / "sub" / "file.txt"
        self.assertEqual(self._call(target, root), 0)

    def test_a_sibling_whose_name_starts_with_the_root_name_is_not_under(self):
        """/foo must never match a /foobar ancestor -- segment-aware, not
        a bare string-prefix test (mirrors newfile-nudge.sh's own
        pre-existing outside-the-code-root test, but exercises the
        primitive directly)."""
        root = Path(self.td) / "foo"
        root.mkdir()
        sibling = Path(self.td) / "foobar"
        sibling.mkdir()
        target = sibling / "file.txt"
        self.assertEqual(self._call(target, root), 1)

    def test_a_symlinked_child_resolves_under_root(self):
        real_root = Path(self.td) / "real-root"
        (real_root / "sub").mkdir(parents=True)
        link = Path(self.td) / "link-to-root"
        link.symlink_to(real_root, target_is_directory=True)
        target_via_link = link / "sub" / "file.txt"
        # Root given by the symlink, target reached through the same
        # symlink.
        self.assertEqual(self._call(target_via_link, link), 0)
        # Root given by its REAL path, target reached through the
        # symlink -- both resolve to the same physical location.
        self.assertEqual(self._call(target_via_link, real_root), 0)

    def test_a_root_containing_a_glob_character_is_matched_literally(self):
        """A store/code root whose physical directory name contains a
        shell glob metacharacter must be compared literally, never
        interpreted as a wildcard (finding 1) -- neither missing a real
        match nor, the more dangerous direction, falsely widening one."""
        root = Path(self.td) / "fo*o"
        (root / "sub").mkdir(parents=True)
        target = root / "sub" / "file.txt"
        self.assertEqual(self._call(target, root), 0)
        # The glob must not accidentally WIDEN the match either: a
        # differently-named sibling a literal "fo*o" wildcard interpretation
        # would match must stay outside.
        sibling = Path(self.td) / "foXo"
        sibling.mkdir()
        sibling_target = sibling / "file.txt"
        self.assertEqual(self._call(sibling_target, root), 1)


# ---------------------------------------------------------------------------
# macOS port (docs/DESIGN.md SS8 port note, 2026-08-30): flock -> python
# fcntl, timeout -> python watchdog. TDD per the port instructions for the
# three mechanics the pre-port test suite did not already pin.
# ---------------------------------------------------------------------------


@unittest.skipUnless(VENV_PYTHON, _SKIP_NO_VENV)
class TestMacOSPortMechanics(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-port-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

    # ---- item 1: flock -> python fcntl -------------------------------

    def _run_lock_contention_scenario(self, memlib_path: Path, wait_seconds: float):
        """Holds a real exclusive flock on STATE_FILE.lock from a separate
        process, then calls mc_update_state_json through a tiny caller
        script that sources memlib_path directly -- not through any hook's
        own outer watchdog, which (exactly like userprompt-remind.sh's
        pre-port outer `timeout 2` already did for that one hook) can
        otherwise legitimately preempt a lock wait that is itself
        approaching its own deadline. That whole-run-vs-per-lock race is a
        real, accepted trade-off of giving every hook a single whole-run
        budget (item 2) -- this test is about the LOCK mechanism itself
        (item 1), so it drives memlib.sh directly."""
        home = Path(self.td) / f"home-{wait_seconds}"
        home.mkdir()
        state_file = home / "sessions" / "proj" / "s1.json"
        state_file.parent.mkdir(parents=True)
        lockfile = Path(str(state_file) + ".lock")

        holder = subprocess.Popen([
            VENV_PYTHON,
            "-c",
            "import fcntl, time, sys\n"
            "fd = open(sys.argv[1], 'a+')\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "time.sleep(float(sys.argv[2]))\n",
            str(lockfile),
            str(wait_seconds),
        ])
        self.addCleanup(lambda: holder.wait(timeout=10))
        time.sleep(0.3)  # let the holder actually acquire the lock first

        caller = Path(self.td) / f"caller-{wait_seconds}.sh"
        caller.write_text(
            "#!/usr/bin/env bash\n"
            "set -u\n"
            f'source "{memlib_path}"\n'
            f'mc_update_state_json "{state_file}" \'\n'
            "state[\"should_not_appear\"] = True\n"
            "print(json.dumps(state))\n"
            "'\n"
            'echo "RC=$?"\n'
        )
        caller.chmod(0o755)
        env = clean_env(MEMCONTINUUM_HOME=str(home), MEMCONTINUUM_PYTHON=VENV_PYTHON)
        start = time.monotonic()
        proc = subprocess.run(
            [MC_BASH, str(caller)], capture_output=True, text=True, env=env, timeout=10.0
        )
        elapsed = time.monotonic() - start
        return proc, elapsed, home, state_file

    def test_lock_timeout_fails_open_with_log_line_and_state_untouched(self):
        """Item 1's port TDD requirement: the new fcntl-based lock must
        still fail open with a log line, bounded to ~2s, and must never
        touch the state file -- exactly what the old `flock -w 2` did."""
        proc, elapsed, home, state_file = self._run_lock_contention_scenario(MEMLIB, 5.0)
        self.assertIn("RC=1", proc.stdout, proc.stderr)
        self.assertLess(elapsed, 4.0, "the lock wait must be bounded to ~2s, not block indefinitely")
        self.assertGreaterEqual(
            elapsed, 1.5, "must actually have waited out the ~2s deadline, not skipped it"
        )
        log_text = (home / "hook.log").read_text()
        self.assertIn("outcome=lock-timeout", log_text)
        self.assertFalse(state_file.exists(), "a lock-timeout write must never touch the state file")

    def test_control_broken_lock_deadline_would_blow_the_bound(self):
        """Control experiment (project rule: a fix's test must fail on the
        pre-fix code, or it's decoration). Patches the real memlib.sh's 2s
        lock deadline out (into a temp copy -- the real file is never
        touched) so the lock-wait loop cannot give up, and proves the
        assertion above (`elapsed < 4.0`) actually goes red without a
        working deadline -- not vacuously true regardless of the
        mechanism."""
        # Re-gate round 3 (MAJOR 1): the literal `+ 2.0` moved into a
        # DEFAULT for the optional DEADLINE_SECONDS argument
        # mc_fallback_write_state's own short-deadline callers now pass
        # explicitly -- the line under test is `_deadline = time.time() +
        # _deadline_s` (a variable) for every caller, defaulted to 2.0
        # only via `_deadline_s`'s own resolution a few lines above. This
        # control still targets the SAME mechanism every default-2.0s
        # caller (including this test's own _run_lock_contention_scenario,
        # which calls mc_update_state_json with no 3rd argument) goes
        # through -- hardcoding 30.0 here breaks the deadline for that
        # caller exactly as the old literal-2.0 replacement did.
        original = MEMLIB.read_text()
        self.assertIn("_deadline = time.time() + _deadline_s", original)
        broken = original.replace(
            "_deadline = time.time() + _deadline_s", "_deadline = time.time() + 30.0", 1
        )
        self.assertNotEqual(broken, original)
        broken_memlib = Path(self.td) / "memlib-broken-control.sh"
        broken_memlib.write_text(broken)

        proc, elapsed, home, state_file = self._run_lock_contention_scenario(broken_memlib, 5.0)
        self.assertGreaterEqual(
            elapsed,
            4.0,
            "with the deadline broken, the call must block past the bound the real test "
            "asserts under -- proving that assertion is load-bearing, not decoration",
        )

    # ---- item 2: timeout -> python watchdog ---------------------------

    def test_watchdog_kills_a_hung_grandchild_holding_stdout(self):
        """TDD requirement: 'watchdog kills a hung grandchild'. A python
        wrapper standing in for MEMCONTINUUM_PYTHON backgrounds a `sleep
        30` (inheriting stdout/stderr) before every real call it makes,
        then execs the real work. Empirically this hook's own internal
        `eval "$(mc_extract_fields ...)"` command substitution ends up
        waiting on that same orphaned descendant too, so the whole child
        script itself stalls past the 2s budget and the launcher's
        proc.wait(timeout=2) DOES raise -- this is the timeout path, not a
        success-path orphan. What this test actually pins: subprocess.run(
        ..., capture_output=True) on the test side will not see EOF on its
        stdout pipe until every process holding it exits, so if the
        watchdog's process-group kill did not really reach every
        descendant (e.g. a regression back to killing only proc.pid, not
        its whole pgid -- the original "orphaned-grandchild lesson" this
        guard exists to fix), the orphaned `sleep 30` would keep the pipe
        open and this call would block for the full 30s (or error out at
        this test's own 15s subprocess timeout) instead of completing in
        a few seconds."""
        home = Path(self.td) / "home-grandchild"
        home.mkdir()
        code_root = Path(self.td) / "code-grandchild"
        code_root.mkdir()
        git_init(code_root)
        fpath = code_root / "src" / "f.py"
        _write(fpath, "# f\n")

        wrapper = Path(self.td) / "grandchild-python"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *MC_WATCHDOG_LAUNCHER*) exec \"" + VENV_PYTHON + "\" \"$@\" ;;\n"
            "  esac\n"
            "done\n"
            "(sleep 30 &)\n"  # detached, inherits our stdout/stderr, survives us
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        wrapper.chmod(0o755)

        env = clean_env(
            MEMCONTINUUM_HOME=str(home),
            MEMCONTINUUM_PROJECT="proj",
            MEMCONTINUUM_CODE_ROOT=str(code_root),
            MEMCONTINUUM_PYTHON=str(wrapper),
        )
        payload = json.dumps(
            {
                "session_id": "s-grandchild",
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "cwd": str(code_root),
                "tool_input": {"file_path": str(fpath)},
            }
        )
        start = time.monotonic()
        proc = subprocess.run(
            [MC_BASH, str(LEDGER_HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            timeout=15.0,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(
            elapsed,
            5.0,
            f"an orphaned grandchild holding stdout open must not block completion, took {elapsed:.3f}s",
        )

    # ---- finding 1: launcher self-guards against being killed ---------

    def test_watchdog_launcher_sigterm_kills_the_inner_process_group(self):
        """Finding 1: the launcher spawns its guarded child with
        start_new_session=True but (before this fix) installed no
        SIGTERM/SIGINT handler and no atexit killpg -- if the harness
        kills the LAUNCHER itself (not just its guarded child), the
        child's whole process group survived unbounded, orphaned. Drives
        hooks/mc-watchdog.sh's MC_WATCHDOG_LAUNCHER_PY directly (the exact
        source every one of the five hooks' guard preambles runs) rather
        than through a full hook invocation, since the property under
        test -- the launcher's OWN signal handling -- is identical for
        all five and independent of which hook spawned it.

        This is deliberately RED on the pre-fix source: reverting just
        the `signal.signal(...)`/`atexit.register(_kill_group)` lines
        back out of mc-watchdog.sh (equivalent to the launcher blob every
        hook used to carry before this dedup) leaves the heartbeat child
        running well past the 1s bound this test asserts, because nothing
        catches the SIGTERM in time to kill its process group before the
        default handler just terminates the launcher outright."""
        launcher_py = subprocess.run(
            [MC_BASH, "-c", 'source "$1"; printf %s "$MC_WATCHDOG_LAUNCHER_PY"', "_",
             str(HOOKS_DIR / "mc-watchdog.sh")],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("MC_WATCHDOG_LAUNCHER", launcher_py)

        pidfile = Path(self.td) / "heartbeat-child.pid"
        child_script = Path(self.td) / "heartbeat-child.sh"
        child_script.write_text(
            "#!/usr/bin/env bash\n"
            f'echo $$ > "{pidfile}"\n'
            "while true; do sleep 0.05; done\n"
        )
        child_script.chmod(0o755)

        launcher_proc = subprocess.Popen(
            [VENV_PYTHON, "-c", launcher_py, MC_BASH, str(child_script)],
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            while not pidfile.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists(), "heartbeat child never started")
            child_pid = int(pidfile.read_text().strip())

            def child_alive():
                try:
                    os.kill(child_pid, 0)
                except OSError:
                    return False
                return True

            self.assertTrue(child_alive(), "heartbeat child died before the SIGTERM was even sent")

            os.kill(launcher_proc.pid, signal.SIGTERM)
            start = time.monotonic()
            while child_alive() and time.monotonic() - start < 1.0:
                time.sleep(0.02)
            died_after = time.monotonic() - start
            self.assertFalse(
                child_alive(),
                f"inner process group outlived the launcher's SIGTERM by >{died_after:.3f}s",
            )
            self.assertLess(died_after, 1.0, f"took {died_after:.3f}s -- expected ~0.5s")
        finally:
            try:
                os.killpg(launcher_proc.pid, signal.SIGKILL)
            except Exception:
                pass
            try:
                launcher_proc.wait(timeout=2)
            except Exception:
                pass
            try:
                if pidfile.exists():
                    os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)
            except Exception:
                pass

    # ---- finding 6: launcher startup signal race -----------------------

    def test_launcher_startup_signal_race_pending_flag_kills_promptly(self):
        """Finding 6 (LOW): a SIGTERM/SIGINT landing strictly between the
        launcher's `signal.signal(...)` registration and its own `proc =
        Popen(...)` assignment COMPLETING is exactly the window the pre-fix
        `_on_signal` handler could do nothing about: `proc` was still its
        initial `None`, so `_kill_group`'s `if proc is None: return` made
        the handler a silent no-op even though the real child process
        already existed. The fix installs a pending-kill flag the handler
        can set even before `proc` exists, checked right after Popen
        returns.

        Deterministic reproduction (real end-to-end signal timing across
        process boundaries is exactly the "hard to time" case the finding
        itself calls out): a driver script monkeypatches
        `subprocess.Popen` so the self-SIGTERM fires and is given time to
        be delivered (a short `time.sleep`, which DOES check for pending
        signals) INSIDE the wrapped Popen call -- after the real child
        process already exists (its pid is captured), but strictly before
        control returns to the launcher's own `proc = ...` assignment.
        This lands in the exact race window under test on every run, not
        as a matter of luck."""
        launcher_py = subprocess.run(
            [MC_BASH, "-c", 'source "$1"; printf %s "$MC_WATCHDOG_LAUNCHER_PY"', "_",
             str(HOOKS_DIR / "mc-watchdog.sh")],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("MC_WATCHDOG_LAUNCHER", launcher_py)

        pidfile = Path(self.td) / "race-heartbeat-child.pid"
        child_script = Path(self.td) / "race-heartbeat-child.sh"
        child_script.write_text(
            "#!/usr/bin/env bash\n"
            f'echo $$ > "{pidfile}"\n'
            "while true; do sleep 0.05; done\n"
        )
        child_script.chmod(0o755)

        pidholder_file = Path(self.td) / "child-pid-from-driver.txt"
        driver = Path(self.td) / "race_driver.py"
        driver.write_text(
            "import subprocess, sys, signal, os, time\n"
            "_real_popen = subprocess.Popen\n"
            f"_pidholder_path = {str(pidholder_file)!r}\n"
            "def _patched_popen(*a, **kw):\n"
            "    p = _real_popen(*a, **kw)\n"
            "    with open(_pidholder_path, 'w') as f:\n"
            "        f.write(str(p.pid))\n"
            "    os.kill(os.getpid(), signal.SIGTERM)\n"
            "    time.sleep(0.1)  # give the pending signal a chance to be delivered HERE\n"
            "    return p\n"
            "subprocess.Popen = _patched_popen\n"
            + launcher_py
        )

        # Deliberately NOT capture_output=True / PIPE here: the guarded
        # child inherits the driver's stdout/stderr (start_new_session=
        # True, no explicit redirection -- matches real passthrough
        # usage), so on the pre-fix bug the child survives orphaned,
        # holding those fds open, and a piped .communicate() would hang
        # on THAT instead of failing on the actual assertion below. A
        # devnull'd Popen + wait() isolates "does the launcher process
        # itself exit promptly" from "is the descendant still alive"
        # (checked separately, directly, right after).
        driver_proc = subprocess.Popen(
            [VENV_PYTHON, str(driver), MC_BASH, str(child_script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            returncode = driver_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            driver_proc.kill()
            driver_proc.wait(timeout=5)
            self.fail("the launcher driver process itself did not exit within 10s")
        # the injected SIGTERM's handler calls sys.exit(0) -- the driver
        # (== the launcher, with one monkeypatch) must exit cleanly, not
        # hang or crash.
        self.assertEqual(returncode, 0)

        self.assertTrue(pidholder_file.exists(), "the patched Popen was never reached")
        child_pid = int(pidholder_file.read_text().strip())

        def child_alive():
            try:
                os.kill(child_pid, 0)
            except OSError:
                return False
            return True

        deadline = time.monotonic() + 2.0
        while child_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        try:
            self.assertFalse(
                child_alive(),
                "a SIGTERM landing between signal.signal() and Popen() returning must "
                "still kill the guarded child's process group promptly (pending-kill flag)",
            )
        finally:
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except Exception:
                pass

    # ---- watchdog-kill log line (README claim made true) ---------------

    def test_watchdog_expiry_leaves_one_log_line(self):
        """Every watchdog kill must leave one log line (the README claims
        this; this makes it true): on budget expiry (subprocess.
        TimeoutExpired), the launcher itself -- not the killed child, which
        never gets the chance -- writes `outcome=watchdog-killed
        hook=<name>` to $MEMCONTINUUM_HOME/hook.log, where <name> is the
        guarded hook script's own basename (sys.argv[2] in the launcher's
        own invocation convention)."""
        launcher_py = subprocess.run(
            [MC_BASH, "-c", 'source "$1"; printf %s "$MC_WATCHDOG_LAUNCHER_PY"', "_",
             str(HOOKS_DIR / "mc-watchdog.sh")],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("MC_WATCHDOG_LAUNCHER", launcher_py)

        home = Path(self.td) / "expiry-home"
        home.mkdir()
        fake_hook = Path(self.td) / "fake-hook-name.sh"
        fake_hook.write_text("#!/usr/bin/env bash\nwhile true; do sleep 0.05; done\n")
        fake_hook.chmod(0o755)

        env = clean_env(MEMCONTINUUM_HOME=str(home), MC_WATCHDOG_BUDGET="0.3")
        proc = subprocess.run(
            [VENV_PYTHON, "-c", launcher_py, MC_BASH, str(fake_hook)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        log_path = home / "hook.log"
        self.assertTrue(log_path.exists(), "watchdog expiry must write hook.log")
        log_text = log_path.read_text()
        self.assertIn("outcome=watchdog-killed", log_text, log_text)
        self.assertIn("hook=fake-hook-name.sh", log_text, log_text)

    def test_watchdog_expiry_line_carries_offset_timestamp_and_pre_resolution_project(self):
        """Round-2 review finding: this line used to have a NAIVE
        timestamp (no UTC offset -- memidx.py stats' parser requires one,
        so it was ALWAYS unparseable) and no project= at all. Without
        MEMCONTINUUM_PROJECT in the launcher's own environment (the
        common case: this launcher runs BEFORE memlib.sh's own MC_PROJECT
        resolution, by design), it must say so explicitly via the literal
        "(pre-resolution)" rather than ever emitting a bare line."""
        launcher_py = subprocess.run(
            [MC_BASH, "-c", 'source "$1"; printf %s "$MC_WATCHDOG_LAUNCHER_PY"', "_",
             str(HOOKS_DIR / "mc-watchdog.sh")],
            capture_output=True, text=True, check=True,
        ).stdout

        home = Path(self.td) / "expiry-ts-home"
        home.mkdir()
        fake_hook = Path(self.td) / "fake-hook-ts.sh"
        fake_hook.write_text("#!/usr/bin/env bash\nwhile true; do sleep 0.05; done\n")
        fake_hook.chmod(0o755)

        env = clean_env(MEMCONTINUUM_HOME=str(home), MC_WATCHDOG_BUDGET="0.3")
        env.pop("MEMCONTINUUM_PROJECT", None)
        proc = subprocess.run(
            [VENV_PYTHON, "-c", launcher_py, MC_BASH, str(fake_hook)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        log_text = (home / "hook.log").read_text()
        matching = [l for l in log_text.splitlines() if "watchdog-killed" in l]
        self.assertEqual(len(matching), 1, log_text)
        line = matching[0]
        self.assertIn("project=(pre-resolution)", line, line)
        # ISO-with-offset timestamp: the same shape memidx.py's
        # _parse_hook_log_ts requires (datetime.fromisoformat, tz-aware).
        ts_token = line.split(" ", 1)[0]
        parsed = datetime.fromisoformat(ts_token)
        self.assertIsNotNone(parsed.tzinfo, f"timestamp {ts_token!r} must carry a UTC offset")

    def test_watchdog_expiry_line_uses_project_from_env_when_set(self):
        """The common installer-rendered shape: MEMCONTINUUM_PROJECT IS
        already baked into the hook line's own env before the launcher
        ever starts -- use it instead of the "(pre-resolution)" fallback."""
        launcher_py = subprocess.run(
            [MC_BASH, "-c", 'source "$1"; printf %s "$MC_WATCHDOG_LAUNCHER_PY"', "_",
             str(HOOKS_DIR / "mc-watchdog.sh")],
            capture_output=True, text=True, check=True,
        ).stdout

        home = Path(self.td) / "expiry-projenv-home"
        home.mkdir()
        fake_hook = Path(self.td) / "fake-hook-projenv.sh"
        fake_hook.write_text("#!/usr/bin/env bash\nwhile true; do sleep 0.05; done\n")
        fake_hook.chmod(0o755)

        env = clean_env(
            MEMCONTINUUM_HOME=str(home), MC_WATCHDOG_BUDGET="0.3", MEMCONTINUUM_PROJECT="wd-proj",
        )
        proc = subprocess.run(
            [VENV_PYTHON, "-c", launcher_py, MC_BASH, str(fake_hook)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        log_text = (home / "hook.log").read_text()
        self.assertIn("project=wd-proj", log_text, log_text)

    # ---- coordinator review fix: kill before writing the fallback ------

    def test_timeout_fallback_write_waits_for_the_child_to_be_reaped(self):
        """Coordinator review fix (F6 follow-up): the timeout-fallback
        write used to happen BEFORE `_kill_group()` -- a live, not-yet-
        killed child could still be writing to the SAME inherited stdout
        fd (no pipe between launcher and child) in that window, landing
        its own bytes after (or interleaved around) the parent's fallback
        write. Reordered: kill the group, reap it, THEN write the
        fallback -- for every guarded hook, not only pre-edit-chain.sh
        (this drives the shared launcher directly, independent of which
        hook spawned it, exactly like this class's other MC_WATCHDOG_
        LAUNCHER_PY-driving tests).

        Reproduction: the guarded child sleeps for exactly the watchdog
        budget (so, given real process-startup skew, it is still asleep
        at the instant the launcher's own `proc.wait(timeout=budget)`
        times out in every run observed on this machine) then bursts many
        small writes to stdout. On the pre-fix ordering, `_log_watchdog_
        kill()`'s own file I/O (a real, multi-millisecond `os.makedirs` +
        `open`/`write`/close before `_kill_group()` ever runs) is a wide
        enough window for the child to wake up and get several writes out
        before being killed, landing garbage around the fallback text. On
        the fixed ordering the child is confirmed dead before the parent
        ever touches stdout, so none of its writes can land at all --
        stdout must be exactly the fallback payload, nothing else."""
        launcher_py = subprocess.run(
            [MC_BASH, "-c", 'source "$1"; printf %s "$MC_WATCHDOG_LAUNCHER_PY"', "_",
             str(HOOKS_DIR / "mc-watchdog.sh")],
            capture_output=True, text=True, check=True,
        ).stdout
        self.assertIn("MC_WATCHDOG_LAUNCHER", launcher_py)

        home = Path(self.td) / "fallback-order-home"
        home.mkdir()
        budget = 0.3
        # Writes continuously from the start, straight through the budget
        # deadline and past it (until actually killed) -- not timed to land
        # in one narrow window, so the child is guaranteed to still be
        # alive and mid-write at whatever instant the launcher acts on the
        # timeout, on the pre-fix ordering as much as the fixed one.
        burst_child = Path(self.td) / "burst-child.sh"
        burst_child.write_text(
            "#!/usr/bin/env bash\n"
            "while true; do\n"
            "  printf 'INTERLEAVED-GARBAGE-'\n"
            "done\n"
        )
        burst_child.chmod(0o755)

        fallback = '{"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":"timed out"}}'
        env = clean_env(
            MEMCONTINUUM_HOME=str(home), MC_WATCHDOG_BUDGET=str(budget),
            MC_WATCHDOG_TIMEOUT_FALLBACK=fallback,
        )
        proc = subprocess.run(
            [VENV_PYTHON, "-c", launcher_py, MC_BASH, str(burst_child)],
            capture_output=True, text=True, env=env, timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The child writes continuously from the moment it starts, so SOME
        # of its own garbage landing before the fallback is expected and
        # harmless (it started well before the timeout could possibly
        # fire) -- that is not the bug under test. What the reorder fixes
        # is specifically: can the child write anything MORE once the
        # parent has decided to act on the timeout? On the pre-fix
        # ordering, `_log_watchdog_kill()`'s own file I/O plus the
        # fallback write both happen while the child is still alive and
        # unreaped, mid-loop -- more of its "INTERLEAVED-GARBAGE-" chunks
        # can and do land AFTER the fallback text in that window. On the
        # fixed ordering the child is confirmed dead (killed and reaped)
        # before the parent ever touches stdout, so nothing can follow the
        # fallback -- it must be the exact suffix of the captured output.
        self.assertTrue(
            proc.stdout.endswith(fallback),
            f"a not-yet-reaped child must never be able to write anything "
            f"after the timeout fallback: got {proc.stdout!r}",
        )
        # Also prove the fallback itself parses cleanly where it appears --
        # a corrupted/interleaved copy embedded in leading garbage would
        # still satisfy endswith() above by coincidence if a clean second
        # copy happened to trail it, so isolate exactly the suffix checked
        # above and parse THAT in isolation.
        json.loads(proc.stdout[-len(fallback):])

    # ---- item 2: stdout passthrough under the watchdog -----------------

    def test_stdout_passes_through_the_watchdog_byte_identical_to_unguarded(self):
        """TDD requirement: 'stdout passthrough under the watchdog'. These
        hooks' own outputs are deliberately small and capped by design (the
        coverage signal shows at most 8 paths + a count), so there is no
        real scenario that forces a pipe-buffer-sized payload through
        them -- chasing an artificial 64KB blob would test a shape these
        scripts never actually produce. The guarantee that actually matters
        is that the extra python launcher process the guard interposes
        never buffers, reorders, or truncates anything relative to running
        the exact same logic with no launcher in the way at all. Proven
        differentially: two sessions seeded with IDENTICAL ledgers (so
        classification renders byte-identical text) are run through the
        SAME script once normally (guarded, the real production path) and
        once with MC_UNDER_TIMEOUT pre-set (a real, exercised seam -- see
        every guard's own `if [ -z "${MC_UNDER_TIMEOUT:-}" ]` check --
        that skips straight to the child logic, no launcher at all); their
        stdout must be byte-identical."""
        code_root = Path(self.td) / "code-passthrough"
        code_root.mkdir()
        git_init(code_root)
        store_root = Path(self.td) / "store-passthrough"
        build_store_root(store_root)
        git_init(store_root)

        def make_session(home_name, session_id):
            home = Path(self.td) / home_name
            home.mkdir()
            db = home / "proj.sqlite"
            reindex(store_root, db, project="proj")
            state_file = home / "sessions" / "proj" / f"{session_id}.json"
            state_file.parent.mkdir(parents=True)
            ledger = []
            for i in range(20):
                p = code_root / "src" / f"passthrough-file-{i:03d}.py"
                if not p.exists():
                    _write(p, f"# {i}\n")
                ledger.append(
                    {
                        "path": str(p),
                        "kind": "code",
                        "content_sha256": hashlib.sha256(str(p).encode()).hexdigest(),
                        "seen_at": 1000.0,
                    }
                )
            state = {
                "session_id": session_id,
                "project": "proj",
                "ledger": ledger,
                "user_turn_count": 0,
                "last_inject_turn": -999,
                "last_inject_time": 0,
                "last_inject_ts": 0,
                "last_injected_pairs": [],
                "last_growth_turn": 0,
                "lookback_count": 0,
            }
            state_file.write_text(json.dumps(state))
            return home

        home_guarded = make_session("home-passthrough-guarded", "s-passthrough-guarded")
        home_unguarded = make_session("home-passthrough-unguarded", "s-passthrough-unguarded")

        def payload_for(session_id, prompt_id):
            return json.dumps(
                {
                    "session_id": session_id,
                    "hook_event_name": "UserPromptSubmit",
                    "source": "user",
                    "cwd": str(code_root),
                    "prompt_id": prompt_id,
                    "user_input": "hello",
                }
            )

        env_guarded = clean_env(
            MEMCONTINUUM_HOME=str(home_guarded),
            MEMCONTINUUM_PROJECT="proj",
            MEMCONTINUUM_ROOT=str(store_root),
            MEMCONTINUUM_CODE_ROOT=str(code_root),
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        env_unguarded = dict(env_guarded)
        env_unguarded["MC_UNDER_TIMEOUT"] = "1"
        env_unguarded["MEMCONTINUUM_HOME"] = str(home_unguarded)

        proc_g, _ = run_script(
            USERPROMPT_HOOK, payload_for("s-passthrough-guarded", "pass-1"), env_guarded, timeout=10.0
        )
        proc_u, _ = run_script(
            USERPROMPT_HOOK, payload_for("s-passthrough-unguarded", "pass-1"), env_unguarded, timeout=10.0
        )
        self.assertEqual(proc_g.returncode, 0, proc_g.stderr)
        self.assertEqual(proc_u.returncode, 0, proc_u.stderr)
        self.assertTrue(proc_g.stdout.strip(), "guarded run should have injected")
        self.assertTrue(proc_u.stdout.strip(), "unguarded run should have injected")
        self.assertEqual(
            proc_g.stdout,
            proc_u.stdout,
            "the watchdog launcher must pass stdout through byte-identical to the unguarded path",
        )
        json.loads(proc_g.stdout)  # must also parse cleanly on its own
        self.assertIn("passthrough-file-000.py", proc_g.stdout)


NEWFILE_NUDGE_HOOK = HOOKS_DIR / "newfile-nudge.sh"


class TestNewFileNudgeHook(unittest.TestCase):
    """Finding 8 (NEW HOOK): hooks/newfile-nudge.sh -- a PreToolUse
    Write-only hook, deliberately separate from pre-edit-chain.sh, that
    fires ONLY when tool_input.file_path does not exist yet (and isn't a
    symlink), sits under a configured code root, and has an indexed
    source extension. Runs under the shared watchdog; never blocks; never
    calls memidx.py."""

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-newfile-nudge-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.code_root = Path(self.td) / "code"
        self.code_root.mkdir()
        self.home = Path(self.td) / "home"
        self.home.mkdir()

    def base_env(self, **overrides):
        env = clean_env(
            MEMCONTINUUM_HOME=str(self.home),
            MEMCONTINUUM_CODE_ROOT=str(self.code_root),
            MEMCONTINUUM_PYTHON=VENV_PYTHON,
        )
        env.update(overrides)
        return env

    def payload_for(self, file_path: str, session_id: str = "s-newfile-nudge"):
        return json.dumps({
            "session_id": session_id,
            "hook_event_name": "PreToolUse",
            "tool_name": "Write",
            "cwd": str(self.code_root),
            "tool_input": {"file_path": file_path},
        })

    def test_bash_syntax_valid(self):
        result = subprocess.run(
            [MC_BASH, "-n", str(NEWFILE_NUDGE_HOOK)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fires_on_new_swift_file_under_code_root(self):
        target = self.code_root / "Sources" / "NewThing.swift"
        proc, elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(target.exists(), "the hook must never create the file itself")
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn(str(self.code_root), ctx)
        # design R3 (audit MC-P1-02): "current" dropped from the message --
        # the word now means content-proven, which this hook has no way to
        # check without paying for a python+sqlite read on every Write.
        self.assertIn("confirm the code index is initialized and not stale", ctx)
        self.assertIn("code-search", ctx)
        self.assertIn("New source file under", ctx)
        # exactly one line of additionalContext.
        self.assertEqual(len(ctx.splitlines()), 1, ctx)

    def test_search_fallback_hit_appended_to_the_reminder(self):
        """search fallback (TOP-0133 L1): the wired-file reminder above
        stays intact; the fallback's own guess is APPENDED, never
        replacing it. MEMCONTINUUM_FALLBACK_MODE=fts: no embedding model
        dependency."""
        store = Path(self.td) / "store"
        (store / "topics").mkdir(parents=True)
        (store / "topics" / "needle.md").write_text(
            "---\ntype: topic\nid: TOP-7002\ntitle: Needle handling policy two\n"
            "code_refs:\n  - somewhere/else/unrelated.py\n"
            "links:\n"
            '  - link: L1\n    status: active\n    '
            'ruling: {text: "handle the needle case two", authority: owner-verbatim, source: s}\n'
            "---\nBody.\n"
        )
        reindex(store, self.home / "default.sqlite")

        target = self.code_root / "Sources" / "needle.swift"
        env = self.base_env(MEMCONTINUUM_FALLBACK_MODE="fts")
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        # the ORIGINAL reminder is still there...
        self.assertIn("New source file under", ctx)
        self.assertIn("confirm the code index is initialized", ctx)
        # ...with the fallback's guess appended, labelled, never as a match.
        self.assertIn(
            "No recorded decision binds this file. Nearest by search -- may be unrelated:",
            ctx,
        )
        self.assertIn("TOP-7002", ctx)
        self.assertIn("handle the needle case two", ctx)

        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=nudged", log_text)
        self.assertIn("fb_outcome=search-fallback ", log_text)
        self.assertIn("fb_hits=1", log_text)
        self.assertIn("fb_ids=TOP-7002", log_text)

        state_file = self.home / "sessions" / "default" / "s-newfile-nudge.json"
        state = json.loads(state_file.read_text())
        fallbacks = state.get("search_fallbacks")
        self.assertTrue(fallbacks)
        self.assertEqual(fallbacks[0]["id"], "TOP-7002")
        self.assertEqual(fallbacks[0]["hook"], "newfile-nudge")

    def _git_code_root_with_worktree(self):
        """code_root as a real git repo with a linked worktree checked out
        INSIDE it (`<code_root>/.worktrees/x`) -- the only worktree layout
        the settings `if` glob (Write(//<code_root>/**)) ever admits."""
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for cmd in (
            ["git", "init", "-q"],
            ["git", "commit", "-q", "--allow-empty", "-m", "init"],
            ["git", "worktree", "add", "-q", "-b", "wt-x", str(self.code_root / ".worktrees" / "x")],
        ):
            subprocess.run(cmd, cwd=self.code_root, check=True, env=env)

    def _fb_q(self, target, cwd):
        payload = json.loads(self.payload_for(str(target)))
        payload["cwd"] = str(cwd)
        proc, _ = run_script(NEWFILE_NUDGE_HOOK, json.dumps(payload), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        m = re.search(r"\bfb_q=(\S*)", log_text.splitlines()[-1])
        self.assertIsNotNone(m, log_text)
        return m.group(1)

    def test_fallback_query_uses_the_worktree_remapped_path_with_cwd_outside_the_code_root(self):
        """Measured defect: a new file in an in-root worktree, session cwd
        OUTSIDE the code root, queried on the raw machine path (home user dev
        ... worktrees x ...). The query is built after the same remap
        pre-edit-chain.sh applies, so it is the file's own words only --
        exactly what pre-edit-chain.sh's q= is for the same file."""
        self._git_code_root_with_worktree()
        outside = Path(self.td) / "launch-dir"
        outside.mkdir()
        target = self.code_root / ".worktrees" / "x" / "Sources" / "Foo" / "BarBaz.swift"
        fb_q = self._fb_q(target, outside)
        self.assertEqual(fb_q, "foo+bar+baz")
        for noise in ("worktrees", "wt-x", "code", "home", "memcontinuum", "tmp", "newfile"):
            self.assertNotIn(noise, fb_q.split("+"))

    def test_fallback_query_strips_the_code_root_for_a_plain_file_with_cwd_outside(self):
        """Same cwd-outside layout, no worktree: the code root is the
        configured root (the nudge's settings entry carries no STRIP_PREFIX),
        so the machine path above it never reaches the query."""
        outside = Path(self.td) / "launch-dir"
        outside.mkdir()
        target = self.code_root / "Sources" / "Foo" / "BarBaz.swift"
        self.assertEqual(self._fb_q(target, outside), "foo+bar+baz")

    def test_search_fallback_returns_decision_records_only(self):
        """The fallback is a guess about a DECISION: a store README, an
        inbox README and a sources/ record that match the file's words far
        better than the real topic are never returned."""
        store = Path(self.td) / "store"
        (store / "topics").mkdir(parents=True)
        (store / "sources").mkdir()
        (store / "inbox" / "codex").mkdir(parents=True)
        (store / "README.md").write_text("# Store\n" + "needle " * 20 + "\n")
        (store / "inbox" / "codex" / "README.md").write_text("needle " * 20 + "\n")
        (store / "sources" / "sitting.md").write_text(
            "---\ntype: source\nid: SITTING-1\ntitle: needle needle\n---\n" + "needle " * 20 + "\n")
        (store / "topics" / "needle.md").write_text(
            "---\ntype: topic\nid: TOP-7003\ntitle: Needle handling policy three\n"
            "links:\n"
            '  - link: L1\n    status: active\n    '
            'ruling: {text: "handle the needle case three", authority: owner-verbatim, source: s}\n'
            "---\nBody.\n"
        )
        # With MEMCONTINUUM_ROOT set the hook's project is the store dir's name.
        reindex(store, self.home / "store.sqlite", project="store")
        target = self.code_root / "Sources" / "needle.swift"
        env = self.base_env(MEMCONTINUUM_FALLBACK_MODE="fts", MEMCONTINUUM_ROOT=str(store))
        proc, _ = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_line = (self.home / "hook.log").read_text().splitlines()[-1]
        self.assertIn("fb_ids=TOP-7003", log_line)
        self.assertNotIn("README", log_line)
        self.assertNotIn("SITTING", log_line)

    def test_search_fallback_empty_still_leaves_reminder_one_line(self):
        """No index at all (this class's base_env sets no project/root, so
        the db file never exists) -- the fallback must skip cleanly
        (no memidx.py call, see newfile-nudge.sh's own db-existence guard)
        and the reminder stays exactly the one line it always was."""
        target = self.code_root / "Sources" / "NoIndexYet.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(len(ctx.splitlines()), 1, ctx)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("fb_outcome=search-fallback-empty", log_text)
        self.assertIn("fb_reason=no-hits", log_text)

    def test_store_root_file_skip_now_logs_fb_reason_store_root(self):
        """MINOR fix-round item: this skip used to log NOTHING at all
        (FB_EXTRA stayed empty) -- now matches pre-edit-chain.sh's own
        identical store-root skip, naming itself on the same `outcome=
        nudged` line via fb_outcome=/fb_reason=."""
        # The store must be NESTED under code_root -- a path outside the
        # code root never reaches even the wired-extension gate (see
        # test_silent_for_a_path_outside_the_code_root above), and a
        # non-wired extension (.md) would exit at "not-indexed-extension"
        # before ever reaching this hook's own store-root fallback skip.
        store = self.code_root / "mem-store"
        store.mkdir()
        target = store / "Something.swift"
        env = self.base_env(MEMCONTINUUM_ROOT=str(store))
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("fb_outcome=search-fallback-empty", log_text)
        self.assertIn("fb_reason=store-root", log_text)

    def test_hybrid_envelope_dict_shape_is_parsed_not_treated_as_zero_hits(self):
        """Addendum (PR #21 merged): hybrid `search --json` now ALWAYS
        wraps hits in `{"results": [...], "fusion": ...}`, never a bare
        list -- the hook's own JSON parser must read `results` off the
        dict, not assume a list (or every hybrid-mode fallback would
        silently read as zero hits). A fake memidx.py stands in for the
        real one and returns exactly that envelope shape, argv-
        independent, to pin the parsing itself rather than depend on a
        real embedding model producing that shape."""
        fake = Path(self.td) / "fake-memidx.py"
        fake.write_text(
            "import json, sys\n"
            "if sys.argv[1] == 'search':\n"
            "    print(json.dumps({\n"
            "        'results': [{'id': 'TOP-9111', 'title': 'Envelope shape topic', "
            "'path': 'x.md', 'chain_text': 'TOP-9111 chain text here'}],\n"
            "        'fusion': 'rrf',\n"
            "    }))\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        wrapper = Path(self.td) / "fake-py-wrapper"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f'REAL_PY="{VENV_PYTHON}"\n'
            'if [ "$1" = "-c" ]; then\n'
            '    exec "$REAL_PY" "$@"\n'
            'fi\n'
            'shift\n'
            f'exec "$REAL_PY" "{fake}" "$@"\n'
        )
        wrapper.chmod(0o755)

        target = self.code_root / "Sources" / "envelope.swift"
        env = self.base_env(MEMCONTINUUM_PYTHON=str(wrapper))
        # the db must exist (any content) so the hook's own db-existence
        # guard does not skip the subprocess call entirely.
        (self.home / "default.sqlite").write_bytes(b"")
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TOP-9111", ctx)
        self.assertIn("chain text here", ctx)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("fb_outcome=search-fallback ", log_text)
        self.assertIn("fb_hits=1", log_text)

    def _run_with_fake_search(self, payload_obj):
        """Run the new-file hook with a fake memidx.py search that returns
        payload_obj (a search --json envelope) regardless of argv."""
        fake = Path(self.td) / "fake-memidx-floor.py"
        fake.write_text(
            "import json, sys\n"
            "if sys.argv[1] == 'search':\n"
            f"    print(json.dumps({payload_obj!r}))\n"
            "    sys.exit(0)\n"
            "sys.exit(1)\n"
        )
        wrapper = Path(self.td) / "fake-py-wrapper-floor"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f'REAL_PY="{VENV_PYTHON}"\n'
            'if [ "$1" = "-c" ]; then\n'
            '    exec "$REAL_PY" "$@"\n'
            'fi\n'
            'shift\n'
            f'exec "$REAL_PY" "{fake}" "$@"\n'
        )
        wrapper.chmod(0o755)
        (self.home / "default.sqlite").write_bytes(b"")
        target = self.code_root / "Sources" / "floorcase.swift"
        env = self.base_env(MEMCONTINUUM_PYTHON=str(wrapper))
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        return ctx, (self.home / "hook.log").read_text()

    @staticmethod
    def _floor_envelope(second_score, **extra):
        return {"results": [
            {"id": "TOP-9201", "title": "First", "path": "a.md", "chain_text": "chain nine-two-oh-one", "score": 0.0328},
            {"id": "TOP-9202", "title": "Second", "path": "b.md", "chain_text": "chain nine-two-oh-two", "score": second_score},
        ], **extra}

    def test_second_floor_drops_a_below_floor_second_hit_end_to_end(self):
        """TOP-0133 L5: RRF envelope, second score below the 0.0314 floor ->
        only the first hit is rendered, the drop is logged, and the dropped
        id is not recorded in session state."""
        ctx, log_text = self._run_with_fake_search(self._floor_envelope(0.0200, fusion="rrf"))
        self.assertIn("chain nine-two-oh-one", ctx)
        self.assertNotIn("chain nine-two-oh-two", ctx)
        self.assertIn("fb_hits=1", log_text)
        self.assertIn("fb_ids=TOP-9201", log_text)
        self.assertIn("fb_floor_dropped=TOP-9202", log_text)
        state = json.loads((self.home / "sessions" / "default" / "s-newfile-nudge.json").read_text())
        self.assertEqual([e["id"] for e in state["search_fallbacks"]], ["TOP-9201"])

    def test_second_floor_keeps_both_hits_when_not_rrf(self):
        ctx, log_text = self._run_with_fake_search(self._floor_envelope(0.0200, fusion="vector-only"))
        self.assertIn("chain nine-two-oh-two", ctx)
        self.assertIn("fb_hits=2", log_text)
        self.assertNotIn("fb_floor_dropped", log_text)

    def test_silent_for_an_existing_file(self):
        target = self.code_root / "Existing.swift"
        target.write_text("// already here\n")
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)

    def test_silent_for_a_non_indexed_extension(self):
        target = self.code_root / "Notes.md"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)

    def test_silent_for_a_path_outside_the_code_root(self):
        outside = Path(self.td) / "outside" / "NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(outside)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)

    def test_silent_for_a_dot_dot_traversal_path(self):
        # MEDIUM (2026-08-31 review): the under-root check used to be a
        # plain lexical `case "$FILE_PATH" in "$CODE_ROOT"/*` prefix match
        # -- `<code_root>/../outside/x.swift` starts with the code-root
        # string textually while actually resolving to a sibling
        # directory OUTSIDE it. Must stay silent.
        outside_sibling = Path(self.td) / "outside"
        outside_sibling.mkdir()
        traversal = f"{self.code_root}/../outside/NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(traversal), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)

    def test_silent_for_a_symlinked_parent_escaping_the_code_root(self):
        # A path that is lexically under the code root at every segment,
        # but whose nearest EXISTING ancestor directory is actually a
        # symlink pointing outside it -- must resolve the real path before
        # judging containment, not trust the string.
        real_outside = Path(self.td) / "real-outside"
        real_outside.mkdir()
        escape_link = self.code_root / "escape-link"
        escape_link.symlink_to(real_outside, target_is_directory=True)
        target = escape_link / "NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)

    def test_silent_for_a_broken_symlink(self):
        # `-e` is false for a broken symlink -- `-L` must be checked too,
        # or a broken symlink would be misread as "a brand-new file".
        target = self.code_root / "Linked.swift"
        target.symlink_to(self.code_root / "does-not-exist-target.swift")
        self.assertFalse(target.exists())  # confirms it's genuinely broken
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)

    def test_silent_with_no_code_root_configured(self):
        target = self.code_root / "NewThing.swift"
        env = self.base_env()
        del env["MEMCONTINUUM_CODE_ROOT"]
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)

    def test_logs_exactly_one_line_per_invocation(self):
        target = self.code_root / "Logged.swift"
        run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        lines = [l for l in log_text.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, log_text)
        self.assertIn("newfile-nudge", lines[0])
        self.assertIn("outcome=nudged", lines[0])

    def test_log_line_carries_project(self):
        """Liveness metric fix: newfile-nudge.sh doesn't source memlib.sh
        (its own independent logger, like pre-edit-chain.sh), so it needs
        its own project= resolution -- MEMCONTINUUM_PROJECT when set."""
        target = self.code_root / "WithProject.swift"
        env = self.base_env(MEMCONTINUUM_PROJECT="explicit-proj")
        run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("project=explicit-proj", log_text)

    def test_log_line_project_defaults_when_unset(self):
        """No MEMCONTINUUM_PROJECT and no MEMCONTINUUM_ROOT configured (this
        class's base_env sets neither) -- must still log project=default,
        never an empty/missing project= token."""
        target = self.code_root / "DefaultProject.swift"
        run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("project=default", log_text)

    def test_never_writes_under_code_root_or_store(self):
        target = self.code_root / "SideEffectFree.swift"
        before = set(self.code_root.rglob("*"))
        run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        after = set(self.code_root.rglob("*"))
        self.assertEqual(before, after, "newfile-nudge.sh must never write under the code root")

    def test_completes_under_poisoned_pythonpath(self):
        target = self.code_root / "Poisoned.swift"
        env = poisoned_env(self.td, **self.base_env())
        proc, elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertLess(elapsed, 3.0, f"took {elapsed:.3f}s")

    def test_p95_latency_over_20_runs(self):
        """Finding 8: report the number, don't assert a tight (e.g. 10ms)
        bound -- bash+jq (or the python JSON fallback) plus the shared
        watchdog launcher's own python startup make sub-100ms unrealistic
        to guarantee cross-machine. Asserts only a generous outer bound so
        a real regression (e.g. the watchdog no longer short-circuiting)
        still fails the suite."""
        samples = []
        for i in range(20):
            target = self.code_root / f"Latency{i}.swift"
            _proc, elapsed = run_script(
                NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env()
            )
            samples.append(elapsed)
        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        print(f"\nnewfile-nudge.sh p95 latency over 20 runs: {p95 * 1000:.1f}ms "
              f"(min {samples[0]*1000:.1f}ms, max {samples[-1]*1000:.1f}ms)")
        self.assertLess(p95, 1.5, f"p95 {p95:.3f}s far exceeds a generous 1.5s outer bound")

    def test_fail_open_when_watchdog_lib_missing(self):
        """R1 regression, round 4 gate: mc-watchdog.sh missing/unsourceable
        used to leave MC_GUARD_PY unset, and `[ -x "$MC_GUARD_PY" ]` under
        `set -u` aborted the hook with 'unbound variable' instead of
        falling through unguarded -- a PreToolUse hook FAILING instead of
        failing open. Must still exit 0, never block, and still do the
        real nudge work (proving the fallthrough, not just non-crashing)."""
        target = self.code_root / "WatchdogLibMissing.swift"
        env = self.base_env(MC_WATCHDOG_LIB_PATH="/nonexistent/mc-watchdog.sh")
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("unbound variable", proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("New source file under", ctx)

    # -- Task 9: env-driven extension gate -----------------------------

    def test_wired_env_lets_a_new_py_file_through(self):
        """MEMCONTINUUM_LANG_EXTS="*.swift *.py" -- a new .py file must
        pass the gate exactly like .swift does today (fires, nudged)."""
        target = self.code_root / "new_thing.py"
        env = self.base_env(MEMCONTINUUM_LANG_EXTS="*.swift *.py")
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("New source file under", ctx)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=nudged", log_text)

    def test_known_but_not_wired_extension_logs_language_available_not_wired(self):
        """Only *.swift is wired; KNOWN includes *.py -- a new .py file
        must now ALSO surface a structured, numbered decision-point nudge
        naming "python" (newlang-nudge N1), on top of the outcome this
        test pinned before the feature existed. Still logs
        outcome=language-available-not-wired, not the plain
        not-indexed-extension -- that outcome literal is unchanged
        (memidx.py stats / tests/test_stats.py pin it); only the stdout
        behavior and the log line's extra `nudge=` field are new."""
        target = self.code_root / "new_thing.py"
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="*.swift",
            MEMCONTINUUM_KNOWN_EXTS="*.swift *.py",
        )
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("python", ctx.lower())
        # N1 (owner ruling, TOP-0128): a structured numbered-choice
        # prompt, not a one-line notice -- three options, one per line
        # after the header line.
        lines = ctx.splitlines()
        self.assertEqual(len(lines), 4, ctx)
        self.assertTrue(lines[1].startswith("1. "), ctx)
        self.assertTrue(lines[2].startswith("2. "), ctx)
        self.assertTrue(lines[3].startswith("3. "), ctx)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=language-available-not-wired", log_text)
        self.assertIn("nudge=shown", log_text)
        self.assertNotIn("outcome=not-indexed-extension", log_text)

    def test_unknown_extension_logs_not_indexed_extension(self):
        """A .rs file matches neither WIRED nor KNOWN -- plain
        not-indexed-extension, same as any other non-indexed extension."""
        target = self.code_root / "new_thing.rs"
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="*.swift",
            MEMCONTINUUM_KNOWN_EXTS="*.swift *.py",
        )
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=not-indexed-extension", log_text)

    def test_unset_lang_exts_env_is_byte_identical_legacy_swift_only(self):
        """No MEMCONTINUUM_LANG_EXTS/MEMCONTINUUM_KNOWN_EXTS at all (the
        un-re-rendered legacy wiring) must reproduce the original
        hardcoded `*.swift`-only gate byte-identically: .swift still
        fires, and an unwired extension with no KNOWN list falls straight
        to not-indexed-extension (no language-available-not-wired, since
        KNOWN falls back to WIRED when unset)."""
        env = self.base_env()
        self.assertNotIn("MEMCONTINUUM_LANG_EXTS", env)
        self.assertNotIn("MEMCONTINUUM_KNOWN_EXTS", env)

        swift_target = self.code_root / "Legacy.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(swift_target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("New source file under", ctx)

        py_target = self.code_root / "legacy_thing.py"
        proc2, _elapsed2 = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(py_target)), env)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc2.stdout.strip(), "", proc2.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=not-indexed-extension", log_text)
        self.assertNotIn("outcome=language-available-not-wired", log_text)

    def test_explicit_empty_lang_exts_matches_nothing_not_the_swift_fallback(self):
        """Ruling 6 (Task 10 fix round): an EXPLICITLY empty
        MEMCONTINUUM_LANG_EXTS (rendered as `MEMCONTINUUM_LANG_EXTS=''` for
        language-less wiring, never omitted) must NOT fall back to the
        legacy `*.swift` default the way UNSET does --
        `${MEMCONTINUUM_LANG_EXTS-*.swift}` (no colon) only substitutes on
        unset, so a set-but-empty value matches nothing and a new .swift
        file stays silent with outcome=not-indexed-extension. Distinct
        from test_unset_lang_exts_env_is_byte_identical_legacy_swift_only,
        which covers the UNSET case and is unchanged by this fix."""
        env = self.base_env(MEMCONTINUUM_LANG_EXTS="")
        self.assertIn("MEMCONTINUUM_LANG_EXTS", env)
        self.assertEqual(env["MEMCONTINUUM_LANG_EXTS"], "")

        target = self.code_root / "NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=not-indexed-extension", log_text)
        self.assertNotIn("outcome=nudged", log_text)

    def test_explicit_empty_lang_exts_with_known_exts_logs_language_available_not_wired(self):
        """The actual production shape Task 10 renders for language-less
        wiring with a real code root: MEMCONTINUUM_LANG_EXTS='' alongside
        a non-empty MEMCONTINUUM_KNOWN_EXTS (always rendered, per Task
        10). A new .py file (KNOWN but not WIRED, since WIRED is
        deliberately empty) must log language-available-not-wired, not
        the plain not-indexed-extension, and never nudge."""
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="",
            MEMCONTINUUM_KNOWN_EXTS="*.py *.swift",
        )
        target = self.code_root / "new_thing.py"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=language-available-not-wired", log_text)
        self.assertNotIn("outcome=not-indexed-extension", log_text)

    # --- T1: the noglob guard, asserted for what it is actually for -------

    def _cwd_with_matching_files(self):
        """A working directory holding files that MATCH the glob list
        (a.py, b.swift). _ext_matches expands `$2` unquoted to split the
        space-separated pattern list on IFS, so without `set -f` bracketing
        the loop the shell would glob-expand `*.py`/`*.swift` against
        exactly these files and compare the path against filenames instead
        of patterns."""
        cwd = Path(self.td) / "globbable"
        cwd.mkdir(exist_ok=True)
        (cwd / "a.py").write_text("x = 1\n")
        (cwd / "b.swift").write_text("// x\n")
        return cwd

    def test_noglob_guard_still_matches_with_matching_files_in_cwd(self):
        env = self.base_env(MEMCONTINUUM_LANG_EXTS="*.py *.swift")
        target = self.code_root / "Sources" / "NewThing.swift"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env,
            cwd=self._cwd_with_matching_files(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout or "{}")
        self.assertIn(
            "New source file under",
            data.get("hookSpecificOutput", {}).get("additionalContext", ""),
            proc.stdout,
        )
        self.assertIn("outcome=nudged", (self.home / "hook.log").read_text())

    def test_noglob_guard_still_rejects_with_matching_files_in_cwd(self):
        """The other half of the same guard: a non-matching path must stay
        non-matching even when the cwd holds files the patterns would
        expand to. Without `set -f`, `*.py` expands to the literal `a.py`
        sitting here, and `case "$FILE_PATH" in a.py)` no longer means what
        the pattern list said."""
        env = self.base_env(MEMCONTINUUM_LANG_EXTS="*.py *.swift")
        target = self.code_root / "Notes.md"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env,
            cwd=self._cwd_with_matching_files(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        self.assertIn("outcome=not-indexed-extension", (self.home / "hook.log").read_text())

    # --- B4: MEMCONTINUUM_NEVER_EXTS -------------------------------------

    def test_never_extension_finishes_silently_before_the_wired_gate(self):
        """B4: an extension the human said "never" to is checked FIRST and
        finishes with its own outcome -- even if it would otherwise be a
        wired, nudge-worthy extension."""
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="*.py *.swift *.cs",
            MEMCONTINUUM_KNOWN_EXTS="*.py *.swift",
            MEMCONTINUUM_NEVER_EXTS="*.cs",
        )
        target = self.code_root / "Program.cs"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=never-extension", log_text)
        self.assertNotIn("outcome=nudged", log_text)

    def test_never_extension_does_not_silence_other_extensions(self):
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="*.py *.swift",
            MEMCONTINUUM_NEVER_EXTS="*.cs",
        )
        target = self.code_root / "NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("New source file under", proc.stdout, proc.stdout)

    def test_unset_never_exts_changes_nothing(self):
        env = self.base_env(MEMCONTINUUM_LANG_EXTS="*.swift")
        self.assertNotIn("MEMCONTINUUM_NEVER_EXTS", env)
        target = self.code_root / "NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("New source file under", proc.stdout, proc.stdout)

    def test_empty_never_exts_matches_nothing(self):
        env = self.base_env(MEMCONTINUUM_LANG_EXTS="*.swift", MEMCONTINUUM_NEVER_EXTS="")
        target = self.code_root / "NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("New source file under", proc.stdout, proc.stdout)

    # --- newlang-nudge N1/N2: the user-visible new-language nudge --------
    #
    # Letters (a)-(g) below match .superpowers/sdd/newlang-nudge/brief.md's
    # own red-test list verbatim, so a reviewer can check this class
    # against that list line for line.

    def python_wired_known_ts_env(self, **overrides):
        """A python-wired project where the engine also supports
        typescript (KNOWN) but this project never wired it (not in
        WIRED) -- the brief's own scenario for (a)-(d)."""
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="*.py",
            MEMCONTINUUM_KNOWN_EXTS="*.py *.ts",
        )
        env.update(overrides)
        return env

    def test_a_new_language_nudges_once_naming_it(self):
        """(a) a new .ts file in a python-wired project -> the nudge
        fires once, naming typescript, as a structured three-option
        decision point (N1, owner ruling TOP-0128) -- not a one-line
        notice with no way to answer."""
        target = self.code_root / "thing.ts"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.python_wired_known_ts_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("typescript", ctx.lower())
        self.assertIn("not wired", ctx.lower())
        lines = ctx.splitlines()
        self.assertEqual(len(lines), 4, ctx)
        header, opt1, opt2, opt3 = lines

        # Option 1: wire it now. Honest about cost -- names the COMPLETE
        # parameter set, never read as automatic or a one-flag change
        # (N2 of the original brief).
        self.assertTrue(opt1.startswith("1. "), ctx)
        self.assertIn("wire it", opt1.lower())
        self.assertIn("complete", opt1.lower())

        # Option 2: the permanent decline, via the mechanism that already
        # exists (--never-ext / MEMCONTINUUM_NEVER_EXTS) -- named per
        # EXTENSION (.ts), not just the language, and honest that its
        # cost is conditional (one command, or the same full re-run as
        # option 1) rather than implying it is always one command.
        self.assertTrue(opt2.startswith("2. "), ctx)
        self.assertIn("never mention", opt2.lower())
        self.assertIn(".ts", opt2)
        self.assertIn("recorded permanently", opt2.lower())
        # Names the actual mechanism (the brief: "the decline path names
        # the real mechanism") -- a stable flag name, not a restated
        # computed procedure, same class as "repo-init.sh" already named
        # in option 1.
        self.assertIn("--never-ext", opt2)
        self.assertIn("one command", opt2.lower())
        self.assertIn("full re-run", opt2.lower())

        # Option 3: not now -- nothing is recorded, stays unwired until
        # decided. TOP-0110 L3: this hook's decision point reaches the
        # assistant through additionalContext, never the human directly,
        # so -- same as the SessionStart consent flow's "Not now" options
        # -- it may not promise a future re-ask ("asked again next
        # session"), even though the per-session dedupe this hook already
        # builds does mean a NEW session gets a fresh chance to show it.
        self.assertTrue(opt3.startswith("3. "), ctx)
        self.assertIn("not now", opt3.lower())
        self.assertIn("nothing is recorded", opt3.lower())
        self.assertIn("unwired", opt3.lower())
        self.assertNotIn("asked again", opt3.lower())
        self.assertNotIn("next session", opt3.lower())

        # N1: point at the skill that documents the re-wiring/decline
        # procedures; never restate either procedure itself (INC-0117: a
        # restated computed rule/procedure is a copy that can drift out
        # from under the code/skill that actually owns it).
        self.assertIn("memcontinuum skill", ctx.lower())
        for restated_flag in ("--adopt-only", "--record-decision", "--code-root", "--langs", "--project"):
            self.assertNotIn(restated_flag, ctx)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=language-available-not-wired", log_text)
        self.assertIn("nudge=shown", log_text)

    def test_a2_multi_extension_language_lists_every_extension(self):
        """javascript covers four extensions (.js/.jsx/.mjs/.cjs) --
        option 2's decline mechanism (--never-ext) is per-extension, so
        naming just the one that triggered this nudge would silently
        leave the other three still nagging. All four must be named."""
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="*.py",
            MEMCONTINUUM_KNOWN_EXTS="*.py *.js *.jsx *.mjs *.cjs",
        )
        target = self.code_root / "thing.js"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        opt2 = ctx.splitlines()[2]
        self.assertTrue(opt2.startswith("2. "), ctx)
        for ext in (".js", ".jsx", ".mjs", ".cjs"):
            self.assertIn(ext, opt2, opt2)

    def test_b_second_file_same_language_same_session_is_suppressed(self):
        """(b) a second new .ts file in the SAME session -> no second
        nudge on stdout, and the log distinguishes the suppression from
        the first, shown occurrence."""
        env = self.python_wired_known_ts_env()
        first = self.code_root / "first.ts"
        second = self.code_root / "second.ts"
        proc1, _ = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(first)), env)
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertNotEqual(proc1.stdout.strip(), "", "first occurrence must nudge")

        proc2, _ = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(second)), env)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertEqual(proc2.stdout.strip(), "", proc2.stdout)

        log_text = (self.home / "hook.log").read_text()
        lines = [l for l in log_text.splitlines() if "language-available-not-wired" in l]
        self.assertEqual(len(lines), 2, log_text)
        self.assertIn("nudge=shown", lines[0])
        self.assertIn("nudge=suppressed", lines[1])

    def test_c_new_session_nudges_again(self):
        """(c) the SAME language, a NEW session_id -> fires again.
        Dedupe is per session, not permanent: coordinator's ruling (see
        the hook's own comment) is that a permanent "already told you"
        marker means a user who missed the line once never hears it
        again."""
        env = self.python_wired_known_ts_env()
        first = self.code_root / "first.ts"
        run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(first), session_id="s-session-one"), env)

        second = self.code_root / "second.ts"
        proc2, _ = run_script(
            NEWFILE_NUDGE_HOOK,
            self.payload_for(str(second), session_id="s-session-two"),
            env,
        )
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        data = json.loads(proc2.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("typescript", ctx.lower())
        log_text = (self.home / "hook.log").read_text()
        shown = [l for l in log_text.splitlines() if "nudge=shown" in l]
        self.assertEqual(len(shown), 2, log_text)

    def test_d_wired_extension_in_same_project_is_unaffected(self):
        """(d) a .py file in that same python-wired project -> unchanged
        behaviour: today's plain "New source file under" message, no
        new-language line, no nudge= field, no language-available-not-
        wired outcome at all."""
        target = self.code_root / "thing.py"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.python_wired_known_ts_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("New source file under", ctx)
        self.assertNotIn("typescript", ctx.lower())
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=nudged", log_text)
        self.assertNotIn("language-available-not-wired", log_text)
        self.assertNotIn("nudge=", log_text)

    def test_e_unsupported_extension_stays_silent(self):
        """(e) an extension the engine does not support at all (not in
        WIRED or KNOWN) -> silent, exactly as today."""
        target = self.code_root / "thing.zzz"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.python_wired_known_ts_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=not-indexed-extension", log_text)
        self.assertNotIn("nudge=", log_text)

    def test_f_language_less_wiring_stays_unchanged(self):
        """(f) language-less wiring (MEMCONTINUUM_LANG_EXTS explicitly
        empty, Ruling 6) -> unchanged: no nudge for ANY known extension,
        even though every write there is technically "known but not
        wired" -- there is no complete wired set to name at all, so the
        nudge is gated on WIRED_EXTS being non-empty (see the hook's own
        comment). Same scenario
        test_explicit_empty_lang_exts_with_known_exts_logs_language_available_not_wired
        already pins for the outcome literal; this test pins that the
        NEW stdout/log behavior stays equally silent."""
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="",
            MEMCONTINUUM_KNOWN_EXTS="*.py *.ts",
        )
        target = self.code_root / "thing.ts"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=language-available-not-wired", log_text)
        self.assertNotIn("nudge=shown", log_text)

    def test_g_wired_path_gains_no_subprocess(self):
        """(g) latency on the wired-extension path is unchanged. Timing
        alone is noisy, so this is a structural proxy: a wired-extension
        write must never create the sessions/ state directory
        mc_update_state_json manages, proving the new dedupe machinery
        (memlib.sh, the state-file read/lock/write) never even runs on
        this path. See test_p95_latency_over_20_runs for the timing
        number itself (reported before/after in the task report)."""
        target = self.code_root / "Sources" / "NewThing.swift"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("New source file under", proc.stdout)
        self.assertFalse(
            (self.home / "sessions").exists(),
            "a wired-extension write must not touch per-session state at all",
        )

    # --- N2 fix round: containment gates the VISIBLE nudge too -----------

    def test_h_outside_code_root_known_unwired_stays_silent(self):
        """N2 (Grok 1, LOW): a known-but-unwired-language write OUTSIDE the
        code root must stay silent, the same as a wired extension does on
        the same path (test_silent_for_a_path_outside_the_code_root) --
        before this fix the two disagreed. The log-only DETECTION outcome
        still fires (unchanged, pre-existing, not scoped to the code
        root); only the visible nudge and the per-session dedupe state
        are gated."""
        outside = Path(self.td) / "outside" / "thing.ts"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(outside)), self.python_wired_known_ts_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=language-available-not-wired", log_text)
        self.assertIn("nudge=outside-code-root", log_text)
        self.assertNotIn("nudge=shown", log_text)
        self.assertFalse(
            (self.home / "sessions").exists(),
            "an out-of-root write must not consume the session's dedupe slot",
        )

    def test_i_dot_dot_traversal_known_unwired_stays_silent(self):
        """N2: the same `<code_root>/../outside/x.ts` traversal shape
        test_silent_for_a_dot_dot_traversal_path already pins for the
        wired path must also stay silent for a known-but-unwired
        extension -- mc_path_under_root rejects the literal `..` segment
        regardless of which branch calls it."""
        outside_sibling = Path(self.td) / "outside"
        outside_sibling.mkdir()
        traversal = f"{self.code_root}/../outside/thing.ts"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(traversal), self.python_wired_known_ts_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("nudge=outside-code-root", log_text)

    def test_j_no_code_root_configured_known_unwired_stays_silent(self):
        """N2: symmetric with the wired path's own
        test_silent_with_no_code_root_configured -- no MEMCONTINUUM_CODE_ROOT
        at all means there is no containment to prove, so the newlang
        nudge (like the wired-file reminder) must stay silent rather than
        show unconditionally."""
        env = self.python_wired_known_ts_env()
        del env["MEMCONTINUUM_CODE_ROOT"]
        target = self.code_root / "thing.ts"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("nudge=outside-code-root", log_text)

    # --- N3 fix round: build before mark, so a build failure never -------
    # --- marks a language as told when nothing was actually shown --------

    def _python_that_fails_only_the_json_build(self):
        """A MEMCONTINUUM_PYTHON replacement that transparently forwards
        every call to the real venv python (same technique as
        _memidx_argv_shim elsewhere in this file) EXCEPT one whose `-c`
        body contains the JSON envelope's own marker string -- that one
        call fails outright, simulating _build_additional_context's
        python step breaking (or being cut off by the watchdog mid-
        build) without touching payload/session-id parsing, which this
        hook's own known-but-unwired branch also runs through $PY."""
        shim = Path(self.td) / "fails-only-json-build.sh"
        shim.write_text(
            "#!/usr/bin/env bash\n"
            "for a in \"$@\"; do\n"
            "  case \"$a\" in\n"
            "    *hookSpecificOutput*) exit 1 ;;\n"
            "  esac\n"
            "done\n"
            f'exec "{VENV_PYTHON}" "$@"\n'
        )
        shim.chmod(0o755)
        return shim

    @unittest.skipUnless(VENV_PYTHON, "needs a real venv python to forward to")
    def test_k_build_failure_logs_build_failed_and_never_marks(self):
        shim = self._python_that_fails_only_the_json_build()
        env = self.python_wired_known_ts_env(MEMCONTINUUM_PYTHON=str(shim))
        target = self.code_root / "first.ts"
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("nudge=build-failed", log_text)
        # Non-vacuous, same shape as test_g_wired_path_gains_no_subprocess:
        # a build failure that never reaches the mark step must not create
        # sessions/ at all -- the build step itself never touches it.
        self.assertFalse(
            (self.home / "sessions").exists(),
            "a build failure must never reach the per-session mark step",
        )
        # The old mark-then-emit order would have left "typescript" in
        # nudged_langs here even though nothing was ever shown --
        # build-then-mark must not.
        state_file = self.home / "sessions" / "default" / "s-newfile-nudge.json"
        if state_file.exists():
            state = json.loads(state_file.read_text())
            self.assertNotIn("typescript", state.get("nudged_langs", []), state)

    @unittest.skipUnless(VENV_PYTHON, "needs a real venv python to forward to")
    def test_l_build_failure_does_not_lose_the_nudge_a_retry_still_shows(self):
        """The real proof N3 fixes something, not just that nothing is
        marked: the SAME language, SAME session, retried with a working
        python after a build failure must still show -- the old
        mark-then-emit order would have suppressed this retry."""
        shim = self._python_that_fails_only_the_json_build()
        failing_env = self.python_wired_known_ts_env(MEMCONTINUUM_PYTHON=str(shim))
        first = self.code_root / "first.ts"
        proc1, _ = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(first)), failing_env)
        self.assertEqual(proc1.returncode, 0, proc1.stderr)
        self.assertEqual(proc1.stdout.strip(), "", proc1.stdout)

        working_env = self.python_wired_known_ts_env()  # real VENV_PYTHON
        second = self.code_root / "second.ts"
        proc2, _ = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(second)), working_env)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        data = json.loads(proc2.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("typescript", ctx.lower())
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("nudge=build-failed", log_text)
        self.assertIn("nudge=shown", log_text)

    def test_m_string_valued_nudged_langs_does_not_suppress_and_self_heals(self):
        """Reviewer finding: nudged_langs is only ever WRITTEN as a JSON
        list (this same transform), but a hand-written or corrupted state
        file could hold a bare string instead -- e.g.
        {"nudged_langs": "typescript"}. Python's `in` on a str is a
        substring test, so `"typescript" in "typescript"` is True and the
        dedupe check would silently suppress the very nudge the string
        never actually recorded (worse: "typescriptfoo" would suppress
        "typescript" too, on substring alone). The membership test must
        require an actual list of strings; anything else is treated the
        same way a wholly garbage state file already is (reset, fail
        OPEN toward showing) -- never a second suppression path."""
        state_file = self.home / "sessions" / "default" / "s-newfile-nudge.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps({"nudged_langs": "typescript"}))

        target = self.code_root / "thing.ts"
        proc, _elapsed = run_script(
            NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), self.python_wired_known_ts_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        ctx = data["hookSpecificOutput"]["additionalContext"]
        self.assertIn("typescript", ctx.lower())
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("nudge=shown", log_text)
        self.assertNotIn("nudge=suppressed", log_text)

        # The state file must now be rewritten to a well-formed list --
        # self-healed, not left corrupted for the next write to trip over.
        healed = json.loads(state_file.read_text())
        self.assertEqual(healed.get("nudged_langs"), ["typescript"], healed)

    def test_language_name_matches_the_engine_registry_for_every_known_language(self):
        """Every chunkers.LANGUAGE_TABLE row's language name must be what
        the hook actually names for EVERY ONE of that row's extensions
        (not just the first -- javascript alone has four) when KNOWN but
        not WIRED -- exercised through the real hook subprocess (no
        source-text matching), so a new LANGUAGE_TABLE row with no
        matching arm in the hook's own extension->name table fails HERE,
        loudly, rather than drifting silently the way INC-0117's stale
        copy did for nine days."""
        for lang, row in chunkers.LANGUAGE_TABLE.items():
            known = " ".join("*" + e for e in row["extensions"])
            env = self.base_env(
                MEMCONTINUUM_LANG_EXTS="*.__never_wired__",
                MEMCONTINUUM_KNOWN_EXTS=f"*.__never_wired__ {known}",
            )
            # Every extension on the row, not just the first -- javascript
            # alone has four (.js/.jsx/.mjs/.cjs) and the hook's own case
            # table has to name all of them "javascript". Each probe gets
            # its own session_id so the per-session dedupe (tested
            # separately above) never suppresses a later probe in this loop.
            for i, ext in enumerate(row["extensions"]):
                target = self.code_root / f"probe-{lang}-{i}{ext}"
                proc, _elapsed = run_script(
                    NEWFILE_NUDGE_HOOK,
                    self.payload_for(str(target), session_id=f"s-lang-table-{lang}-{i}"),
                    env,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                data = json.loads(proc.stdout or "{}")
                ctx = data.get("hookSpecificOutput", {}).get("additionalContext", "")
                self.assertIn(
                    lang, ctx.lower(),
                    f"extension {ext!r} (LANGUAGE_TABLE row {lang!r}) is not named "
                    f"in the nudge: {ctx!r}",
                )

    def test_compound_excludes_hook_guard_matches_registry_exactly(self):
        """Merge-review nit on newlang-nudge: the behavioral test just
        below this one only loops over chunkers.COMPOUND_EXCLUDES, so it
        catches an ADDITION to the registry with no matching hook arm (a
        new compound extension would wrongly nudge/imply-wire a language
        chunkers will never classify it as) but not a REMOVAL -- if an
        entry is dropped from COMPOUND_EXCLUDES without touching the
        hook's static case arm, nothing there notices, and the hook goes
        on silently suppressing an extension lang_for_path has started
        classifying again. That is one-way drift detection dressed up as
        two-way. This test parses the hook's actual guarded set straight
        out of its case arm (the `*.ext | ...) finish
        "not-indexed-extension"` line in hooks/newfile-nudge.sh -- not a
        second hardcoded copy of the current extensions, which would
        just relocate the same staleness risk) and asserts SET EQUALITY
        against chunkers.COMPOUND_EXCLUDES, so either direction of drift
        fails here, and the failure message names which side (hook or
        registry) the odd-one-out extension is missing from."""
        hook_text = NEWFILE_NUDGE_HOOK.read_text()
        m = re.search(
            r'^\s*((?:\*\.[^\s|)]+\s*\|\s*)*\*\.[^\s|)]+)\)\s*finish "not-indexed-extension"',
            hook_text,
            re.MULTILINE,
        )
        self.assertIsNotNone(
            m, "hooks/newfile-nudge.sh: no `*.ext | ...) finish \"not-indexed-extension\"` "
            "case arm found -- has the compound-extension guard been rewritten?"
        )
        guarded = {part.strip().lstrip("*") for part in m.group(1).split("|")}
        registry = set(chunkers.COMPOUND_EXCLUDES)
        added_to_registry = sorted(registry - guarded)
        removed_from_registry = sorted(guarded - registry)
        self.assertEqual(
            guarded, registry,
            "hooks/newfile-nudge.sh's guarded set and chunkers.COMPOUND_EXCLUDES "
            f"drifted apart -- in COMPOUND_EXCLUDES but not guarded by the hook: "
            f"{added_to_registry}; guarded by the hook but no longer in "
            f"COMPOUND_EXCLUDES: {removed_from_registry}",
        )

    def test_compound_excluded_extensions_never_nudge_or_wire(self):
        """Reviewer re-raise (round 2): chunkers.COMPOUND_EXCLUDES
        (chunkers/__init__.py) means a file like foo.d.ts is NEVER
        typescript -- chunkers.lang_for_path checks COMPOUND_EXCLUDES
        FIRST, unconditionally, before it ever consults LANGUAGE_TABLE,
        and wiring typescript does not change that (verified directly:
        memidx.lang_for_source_file, "the single resolution rule the
        whole code-index side shares", calls chunkers.lang_for_path
        first). The hook's own WIRED_EXTS/KNOWN_EXTS globs are rendered
        straight from chunkers.known_extensions()/wired_extensions(),
        plain per-language extension tuples with no compound-extension
        awareness -- a bash `*.ts` case pattern matches `foo.d.ts` just
        as readily as `thing.ts`. Registry-driven against the real
        chunkers.COMPOUND_EXCLUDES set (not a hardcoded list here), so
        this exercises real hook behavior for every extension the
        registry currently names; test_compound_excludes_hook_guard_matches_registry_exactly
        just above is what actually guarantees the two sets cannot drift
        apart in either direction. Mirrors
        test_language_name_matches_the_engine_registry_for_every_known_language
        higher up."""
        for compound in sorted(chunkers.COMPOUND_EXCLUDES):
            host_ext = os.path.splitext(compound)[1]  # ".d.ts" -> ".ts"

            # (a) the host extension KNOWN but not WIRED: must stay
            # silent -- never the language-available-not-wired nudge,
            # which would otherwise name a language wiring won't help.
            env_known = self.base_env(
                MEMCONTINUUM_LANG_EXTS="*.py",
                MEMCONTINUUM_KNOWN_EXTS=f"*.py *{host_ext}",
            )
            target_known = self.code_root / f"probe-known-{compound.replace('.', '_')}{compound}"
            proc_known, _elapsed = run_script(
                NEWFILE_NUDGE_HOOK,
                self.payload_for(str(target_known), session_id=f"s-compound-known-{compound}"),
                env_known,
            )
            self.assertEqual(proc_known.returncode, 0, proc_known.stderr)
            self.assertEqual(proc_known.stdout.strip(), "", f"{compound}: {proc_known.stdout}")
            log_text = (self.home / "hook.log").read_text()
            self.assertIn(
                "outcome=not-indexed-extension", log_text,
                f"{compound}: {log_text}",
            )
            self.assertNotIn(
                "outcome=language-available-not-wired", log_text,
                f"{compound}: {log_text}",
            )

            # (b) the host extension WIRED: must also stay silent --
            # never the plain "New source file under" reminder, which
            # would falsely claim code-search covers this file.
            (self.home / "hook.log").unlink()
            env_wired = self.base_env(MEMCONTINUUM_LANG_EXTS=f"*{host_ext}")
            target_wired = self.code_root / f"probe-wired-{compound.replace('.', '_')}{compound}"
            proc_wired, _elapsed2 = run_script(
                NEWFILE_NUDGE_HOOK,
                self.payload_for(str(target_wired), session_id=f"s-compound-wired-{compound}"),
                env_wired,
            )
            self.assertEqual(proc_wired.returncode, 0, proc_wired.stderr)
            self.assertEqual(proc_wired.stdout.strip(), "", f"{compound}: {proc_wired.stdout}")
            log_text2 = (self.home / "hook.log").read_text()
            self.assertIn(
                "outcome=not-indexed-extension", log_text2,
                f"{compound}: {log_text2}",
            )


class TestF2AutoCallers(unittest.TestCase):
    """F2 (coordinator ruling 69): the --auto callers -- unmapped's
    in-process self-heal, precompact-persist.sh's direct reindex call, and
    (design R8, audit MC-P2-02, TOP-0123 L7) post-commit-reindex.sh's own
    bounded content pass, now three -- and repo-init.sh's install-time
    reindex staying an explicit --no-embed initializer without --auto."""

    def test_unmapped_self_heal_passes_auto(self):
        src = inspect.getsource(memidx.cmd_unmapped)
        self.assertIn("auto=True", src)

    def test_precompact_persist_reindex_call_passes_auto(self):
        text = (TOOLS_DIR / "hooks" / "precompact-persist.sh").read_text()
        self.assertIn("--auto", text)

    def test_repo_init_install_time_reindex_does_not_pass_auto(self):
        text = (TOOLS_DIR / "scripts" / "repo-init.sh").read_text()
        line = next(l for l in text.splitlines() if "REINDEX_CMD=" in l and "code-reindex" not in l)
        self.assertIn("--no-embed", line)
        self.assertNotIn("--auto", line)


class TestCwdTokenEncodersAgree(unittest.TestCase):
    """INC-0130 gate round 1: hooks/memlib.sh mc_cwd_token and
    memidx._log_path_token are the same encoder; one input, identical output."""

    CASES = [
        "/x project=other session=fake",
        '/q"uo\\te\'s/back\\slash/100%/tab\there',
        "/mnt/d/Мой проект/\u00e9\u4e2d",
        "/plain/safe_path-1.2~x:y!z",
        "/" + "a" * 300 + "\u00e9" * 150,
        "",
    ]

    def _bash(self, path):
        proc = subprocess.run(
            [MC_BASH, "-c", f'source {shlex.quote(str(MEMLIB))} 2>/dev/null; mc_cwd_token "$1"', "x", path],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_bash_and_python_tokens_agree_and_round_trip(self):
        for path in self.CASES:
            with self.subTest(path=path[:40]):
                tok = self._bash(path)
                self.assertEqual(tok, "cwd=" + memidx._log_path_token(path))
                self.assertNotRegex(tok[4:], r"[\s=\"\\]")
                if len(path.encode()) <= 512:
                    self.assertEqual(urllib.parse.unquote(tok[4:]) if path else tok[4:], path or "-")

    def test_a_control_character_is_encoded_not_dropped(self):
        self.assertEqual(self._bash("/a\x01b\nc"), "cwd=/a%01b%0Ac")
        self.assertEqual(memidx._log_path_token("/a\x01b\nc"), "/a%01b%0Ac")


if __name__ == "__main__":
    unittest.main()
