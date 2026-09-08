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
import inspect
import io
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
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
        prompt_key="user_input",
    ):
        """Real-shaped by default (fix-round 2026-08-31): a live
        UserPromptSubmit payload carries no `source` field at all -- that
        field belongs to SessionStart -- so `source` is omitted unless a
        test explicitly passes one (to prove its presence, if it ever
        shows up again, is a no-op the hook ignores)."""
        d = {
            "session_id": session_id,
            "hook_event_name": "UserPromptSubmit",
            "cwd": str(self.code_root),
            prompt_key: "this text must never be read by the hook",
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
        self.assertFalse(
            (HOOKS_DIR / ".." / ".venv" / "bin" / "python").resolve().exists(),
            "this test relies on no engine .venv existing in this checkout",
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
        load-bearing, not decoration."""
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

    def test_second_rotation_replaces_dot_1_never_creates_dot_2(self):
        hook_log = self.home / "hook.log"
        hook_log.write_text("first\n" * 100)
        self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10")
        rotated = self.home / "hook.log.1"
        self.assertEqual(rotated.read_text(), "first\n" * 100)

        hook_log.write_text("second\n" * 100)
        self._call_rotate(MEMCONTINUUM_LOG_MAX_BYTES="10")
        self.assertEqual(rotated.read_text(), "second\n" * 100)
        self.assertFalse((self.home / "hook.log.2").exists())

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

    def payload_for(self, file_path: str):
        return json.dumps({
            "session_id": "s-newfile-nudge",
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
        must stay silent on stdout (never nudged for an unwired language)
        but log outcome=language-available-not-wired, not the plain
        not-indexed-extension."""
        target = self.code_root / "new_thing.py"
        env = self.base_env(
            MEMCONTINUUM_LANG_EXTS="*.swift",
            MEMCONTINUUM_KNOWN_EXTS="*.swift *.py",
        )
        proc, _elapsed = run_script(NEWFILE_NUDGE_HOOK, self.payload_for(str(target)), env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "", proc.stdout)
        log_text = (self.home / "hook.log").read_text()
        self.assertIn("outcome=language-available-not-wired", log_text)
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


if __name__ == "__main__":
    unittest.main()
