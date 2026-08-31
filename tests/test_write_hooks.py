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
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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
HOOKS_DIR = TOOLS_DIR / "hooks"
FIXTURES_DIR = TOOLS_DIR / "fixtures" / "payloads"
LEDGER_HOOK = HOOKS_DIR / "ledger-post-edit.sh"
PRECOMPACT_HOOK = HOOKS_DIR / "precompact-persist.sh"
SESSIONSTART_HOOK = HOOKS_DIR / "sessionstart-remind.sh"
USERPROMPT_HOOK = HOOKS_DIR / "userprompt-remind.sh"
SESSIONEND_HOOK = HOOKS_DIR / "sessionend-stamp.sh"
MEMLIB = HOOKS_DIR / "memlib.sh"
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


def run_script(script: Path, payload_text: str, env: dict, timeout: float = 6.0):
    start = time.monotonic()
    proc = subprocess.run(
        [MC_BASH, str(script)],
        input=payload_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    elapsed = time.monotonic() - start
    return proc, elapsed


def poisoned_env(tmp_dir: Path, **overrides):
    poison_dir = Path(tmp_dir) / "poison-site-packages"
    poison_dir.mkdir(exist_ok=True)
    (poison_dir / "yaml.py").write_text('raise RuntimeError("poisoned PYTHONPATH not cleared")\n')
    env = clean_env(PYTHONPATH=f"{POISONED_SITE_PACKAGES}:{poison_dir}")
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

    def test_first_run_self_heals_missing_index(self):
        # db never built -- unmapped must build it (check -> reindex --no-embed)
        # and end up coverage_status ok, not unknown, once it converges.
        self.assertFalse(self.db.exists())
        rc, out = self._run(["src/mapped.py", "src/nothing.py"])
        self.assertEqual(out["coverage_status"], "ok")
        self.assertEqual(out["mapped_topic"], ["src/mapped.py"])
        self.assertEqual(out["unmapped"], ["src/nothing.py"])

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
        env = self.base_env()
        env.update(poisoned_env(self.td))
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
        self, session_id, source="user", agent_id=None, prompt_id=None, prompt_key="user_input"
    ):
        d = {
            "session_id": session_id,
            "hook_event_name": "UserPromptSubmit",
            "source": source,
            "cwd": str(self.code_root),
            prompt_key: "this text must never be read by the hook",
        }
        if agent_id:
            d["agent_id"] = agent_id
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


# ---------------------------------------------------------------------------
# 3. precompact-persist.sh
# ---------------------------------------------------------------------------


class TestPrecompactPersist(HookTestBase):
    def test_bash_syntax_valid(self):
        result = subprocess.run([MC_BASH, "-n", str(PRECOMPACT_HOOK)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

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
            "memory/ should hold? If none, say so once.",
            ctx,
        )
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
        for source in ("clear", "fork"):
            with self.subTest(source=source):
                proc, _ = run_script(
                    SESSIONSTART_HOOK,
                    self.session_start_payload(f"s-start-{source}", source),
                    self.base_env(),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), "")

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
            "'not now' that memory/ should hold? If none, say so once.",
            ctx,
        )
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

    def test_silent_on_empty_evidence(self):
        session_id = "s-prompt-empty"
        self.seed_ledger(session_id, [])
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_source_not_user_is_silent(self):
        session_id = "s-prompt-slash"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, _ = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id, source="my-slash-cmd"), self.base_env()
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_agent_id_present_is_silent(self):
        session_id = "s-prompt-agent"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, source="user", agent_id="agent-123"),
            self.base_env(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def test_never_reads_user_input(self):
        """Two checks, both load-bearing (neither passes on a missing/no-op
        script -- the point of this test is a real, running hook that still
        never touches user_input, not an absent one that trivially can't):

        1. Static: the script source never references the `user_input` key
           at all (docs/DESIGN.md ruling B forbids reading it).
        2. Dynamic: with a real payload whose user_input carries a poison
           string, the hook still runs successfully (rc=0, actually injects,
           since evidence exists) and never leaks that string into its own
           log.
        """
        code_lines = [
            line for line in USERPROMPT_HOOK.read_text().splitlines()
            if not line.strip().startswith("#")
        ]
        offending = [line for line in code_lines if "user_input" in line]
        self.assertEqual(offending, [], f"script code (not comments) references user_input: {offending}")

        session_id = "s-prompt-nouserinput"
        self.seed_ledger(session_id, [(str(self.code_root / "src" / "unmapped.py"), "code")])
        proc, _ = run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip(), "expected a real injection (evidence exists)")

        log_text = (self.home / "hook.log").read_text() if (self.home / "hook.log").exists() else ""
        self.assertNotIn("must never be read", log_text)
        self.assertNotIn("must never be read", proc.stdout)

    def test_never_reads_prompt_key_either(self):
        """Payload contract addendum (Codex): accept BOTH `user_input` and
        `prompt` as the payload's prompt-text key -- the not-read invariant
        must cover both names. Word-boundary regex so `prompt_id` (the
        dedupe field this addendum adds) and `UserPromptSubmit` don't
        false-positive."""
        import re

        code_lines = [
            line for line in USERPROMPT_HOOK.read_text().splitlines()
            if not line.strip().startswith("#")
        ]
        pattern = re.compile(r"\bprompt\b")
        offending = [line for line in code_lines if pattern.search(line)]
        self.assertEqual(offending, [], f"script code (not comments) references bare `prompt`: {offending}")

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

    def test_payload_keys_logged_once_per_session(self):
        session_id = "s-prompt-payloadkeys"
        self.seed_ledger(session_id, [])
        run_script(USERPROMPT_HOOK, self.user_prompt_payload(session_id), self.base_env())
        log_text = (self.home / "hook.log").read_text()
        keys_lines = [l for l in log_text.splitlines() if "payload_keys=" in l]
        self.assertEqual(len(keys_lines), 1)
        self.assertIn("source", keys_lines[0])
        self.assertIn("session_id", keys_lines[0])

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
            "memory/ should hold? If none, say so once.",
            ctx,
        )
        self.assertEqual(scan_forbidden_lines(ctx), [])

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


# ---------------------------------------------------------------------------
# 5b. userprompt-remind.sh -- the T-thin look-back reminder
#     (docs/DESIGN.md 2026-08-30)
# ---------------------------------------------------------------------------

LOOKBACK_QUESTION = (
    "Did the conversation since then establish any ruling, incident, "
    "rejected alternative, priority, wording choice, money decision, or "
    "'not now' that memory/ should hold? If none, say so once."
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
        self.assertIn(LOOKBACK_QUESTION, ctx)
        self.assertEqual(scan_forbidden_lines(ctx), [])
        # exactly one block -- one hookSpecificOutput, one question
        self.assertEqual(ctx.count("Did the conversation"), 1)

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

    def test_source_not_user_silent_even_when_thin(self):
        session_id = "s-lb-nonuser"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=20, last_growth_turn=0)
        proc, _ = run_script(
            USERPROMPT_HOOK, self.user_prompt_payload(session_id, source="my-slash-cmd"), self.base_env()
        )
        self.assertEqual(proc.stdout.strip(), "")

    def test_agent_id_silent_even_when_thin(self):
        session_id = "s-lb-agent"
        self._start(session_id)
        self.patch_state(session_id, user_turn_count=20, last_growth_turn=0)
        proc, _ = run_script(
            USERPROMPT_HOOK,
            self.user_prompt_payload(session_id, source="user", agent_id="agent-1"),
            self.base_env(),
        )
        self.assertEqual(proc.stdout.strip(), "")

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
        original = MEMLIB.read_text()
        self.assertIn("_deadline = time.time() + 2.0", original)
        broken = original.replace(
            "_deadline = time.time() + 2.0", "_deadline = time.time() + 30.0", 1
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


if __name__ == "__main__":
    unittest.main()
