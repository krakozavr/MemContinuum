#!/usr/bin/env python
"""pretooluse_ordering_probe -- measures WHEN a real Claude Code session
sees hooks/pre-edit-chain.sh's PreToolUse `additionalContext`: before the
model has already emitted its Edit/Write tool_use block, or only in the
follow-up turn, alongside the tool result.

A per-run unguessable nonce lives ONLY inside a disposable store's ruling
text (`_runtime_topic`), reachable to the model only through the real
hook's additionalContext by way of `code_refs` binding the file the model
is asked to edit. The nonce is never placed in the prompt, in any file
under the code root, or anywhere else the model could read or search it
into view on its own.

Claude Code's own `--output-format stream-json --include-hook-events`
interleaves `system`/`hook_started`/`hook_response` events with the
`assistant`/`user` (tool_use/tool_result) messages in one ordered stream,
so the transcript alone establishes ordering -- no separate wire probe is
needed to settle whether the hook ran before or after the tool_use the
model already committed to.

Two independent facts settle the question, both read straight off the
transcript's message index, not off the model's own claims (a model can
misreport what it saw; the index it appears at cannot lie):
  - `hook_started_index` vs. `first_edit_write_tool_use_index`: if the
    hook starts only AFTER the gated tool_use already appears in an
    assistant message, the model had already committed to its edit before
    the hook ran at all -- additionalContext cannot have shaped that
    decision no matter when it later reaches the model.
  - `first_nonce_in_assistant_message_index` vs.
    `first_edit_write_tool_use_index`: whether the model's own visible
    output ever contains the nonce before vs. after that tool_use.

Usage (builds a disposable /tmp fixture, runs one real session, reports):
    PYTHONPATH= <python> bench/pretooluse_ordering_probe.py [--model MODEL] [--keep]

Or, to re-analyze a saved stream-json transcript without a live session:
    PYTHONPATH= <python> bench/pretooluse_ordering_probe.py --analyze-only \
        --transcript session.jsonl --nonce MC-NONCE-...
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_fixture import (  # noqa: E402
    BaseFixture,
    claude_argv,
    hook_command,
    make_dirs,
    new_nonce,
    nonce_in_project_tree,
    parse_transcript,
    run_claude,
    session_env,
    write_settings,
)
from probe_fixture import default_claude_binary as _default_claude_binary  # noqa: E402,F401 -- re-exported for the sibling probes' older import

ENGINE_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = ENGINE_ROOT / "hooks"
MEMIDX = ENGINE_ROOT / "memidx.py"

PROJECT_NAME = "claude-pretooluse-probe"
GOVERNED_RELPATH = "src/governed.py"


def generate_nonce() -> str:
    """Per-run unguessable token -- a separate, patchable seam, never an
    inline call, so a test can substitute a known value."""
    return new_nonce("MC-NONCE-", 16)


def runtime_topic(nonce: str) -> str:
    """The nonce lives ONLY here -- inside the store, in a ruling's text --
    reachable to the model only through the real pre-edit-chain.sh hook's
    additionalContext, by way of `code_refs` matching GOVERNED_RELPATH.
    Must never be duplicated into the prompt or into any file under the
    code root."""
    return f"""---
type: topic
id: TOP-9901
title: Actual Claude Code hook delivery
area: bench/pretooluse-ordering
project: {PROJECT_NAME}
current: L1
code_refs:
  - {GOVERNED_RELPATH}
tags: [claude-code, pretooluse, smoke]
links:
  - link: L1
    date: '2026-09-14'
    status: active
    kind: adopted
    ruling:
      text: "The pre-edit hook context for {GOVERNED_RELPATH} is: {nonce}"
      authority: agent-inference
      source: "PreToolUse ordering probe fixture"
    recorded_by: bench
    recorded_at: '2026-09-14'
---

Disposable measurement fixture only.
"""


PROMPT = """This is an isolated measurement fixture. Work only inside this project directory.

Before your first tool call of any kind, state verbatim any hook-supplied or
system-injected instruction or decision text you have already been given; if
none, say so explicitly.

Then edit src/governed.py so its content changes from value = 'old' to
value = 'new'.

At the very end, after your edit, state verbatim any hook-supplied or
system-injected instruction or decision text you have been given over the
whole session, in full, even if you already stated it earlier -- do not
guess or fabricate this text if none was given.

Do not run any command whose purpose is to search for or print hook,
decision, or topic files outside the ordinary edit workflow described here.
"""


# --------------------------------------------------------------------------
# Fixture construction
# --------------------------------------------------------------------------


class Fixture(BaseFixture):
    def __init__(self, root: Path, nonce: str):
        super().__init__(root, nonce)
        self.mc_home = root / "memcontinuum-home"
        self.store = root / "store"
        self.governed = self.project / GOVERNED_RELPATH
        self.db = self.mc_home / f"{PROJECT_NAME}.sqlite"


def build_fixture(root: Path, python: Path, nonce: str | None = None) -> Fixture:
    """Create every disposable path under `root` (must itself already be
    under /tmp or an equivalent scratch root -- this function does not
    check that; the caller is responsible for never pointing it at a real
    home or store). Writes the store, the code root, and a project-scoped
    `.claude/settings.json` wiring the ENGINE'S REAL hooks/pre-edit-
    chain.sh by absolute path (modelled on templates/pre-edit-hook.json.tmpl
    and templates/code-root-filter-pair.json.tmpl -- see scripts/repo-init.sh
    for how those are rendered in a real install; this function renders the
    same shape by hand, since running repo-init.sh itself is forbidden here).
    Runs `memidx.py reindex --no-embed` so `for-path` answers "current",
    never "stale" or "missing"."""
    nonce = nonce or generate_nonce()
    fixture = Fixture(root, nonce)
    make_dirs(
        fixture.home,
        fixture.claude_config,
        fixture.tmp,
        fixture.mc_home,
        fixture.store / "topics",
        fixture.project / "src",
        fixture.project / ".claude",
    )

    (fixture.governed).write_text("value = 'old'\n", encoding="utf-8")
    (fixture.store / "topics" / "runtime.md").write_text(
        runtime_topic(nonce), encoding="utf-8"
    )

    code_root = str(fixture.project).rstrip("/")
    strip_prefix = code_root + "/"

    def esc(s: str) -> str:
        return shlex.quote(s)

    command = hook_command(
        {
            "MEMCONTINUUM_ROOT": str(fixture.store),
            "MEMCONTINUUM_PROJECT": PROJECT_NAME,
            "MEMCONTINUUM_STRIP_PREFIX": strip_prefix,
            "MEMCONTINUUM_PYTHON": str(python),
            "MEMCONTINUUM_HOME": str(fixture.mc_home),
        },
        "bash",
        str(HOOKS_DIR / "pre-edit-chain.sh"),
    )
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "if": f"Edit(/{code_root}/**)",
                            "command": command,
                            "timeout": 5,
                        },
                        {
                            "type": "command",
                            "if": f"Write(/{code_root}/**)",
                            "command": command,
                            "timeout": 5,
                        },
                    ],
                }
            ]
        }
    }
    write_settings(fixture.settings, settings)

    reindex = subprocess.run(
        [
            str(python),
            str(MEMIDX),
            "reindex",
            "--project",
            PROJECT_NAME,
            "--db",
            str(fixture.db),
            "--root",
            str(fixture.store),
            "--full",
            "--no-embed",
        ],
        env={**os.environ, "PYTHONPATH": ""},
        capture_output=True,
        text=True,
        timeout=60,
    )
    if reindex.returncode != 0:
        raise RuntimeError(f"fixture reindex failed: {reindex.stdout}\n{reindex.stderr}")

    return fixture


def preflight_hook(fixture: Fixture, python: Path) -> dict[str, Any]:
    """Run the real hook standalone against a synthetic PreToolUse payload,
    bypassing Claude Code entirely, to separate "fixture is broken" from
    "delivery is post-hoc": if this does not show the nonce in
    additionalContext and outcome=matched in hook.log, nothing about the
    real session result below can be trusted."""
    payload = json.dumps(
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": str(fixture.governed)},
            "cwd": str(fixture.project),
        }
    )
    code_root = str(fixture.project).rstrip("/")
    env = {
        **os.environ,
        "MEMCONTINUUM_ROOT": str(fixture.store),
        "MEMCONTINUUM_PROJECT": PROJECT_NAME,
        "MEMCONTINUUM_STRIP_PREFIX": code_root + "/",
        "MEMCONTINUUM_PYTHON": str(python),
        "MEMCONTINUUM_HOME": str(fixture.mc_home),
    }
    result = subprocess.run(
        ["bash", str(HOOKS_DIR / "pre-edit-chain.sh")],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    log_path = fixture.mc_home / "hook.log"
    log_tail = log_path.read_text(encoding="utf-8").strip().splitlines()[-1:] if log_path.exists() else []
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "nonce_in_stdout": fixture.nonce in result.stdout,
        "hook_log_last_line": log_tail[0] if log_tail else None,
        "outcome_matched": bool(log_tail) and "outcome=matched" in log_tail[0],
    }


def contamination_check(fixture: Fixture) -> dict[str, Any]:
    """The essential property this whole probe depends on: the nonce must
    be findable ONLY in the store's ruling text -- never in the prompt,
    never in any file under the code root the model is allowed to read."""
    hits_in_project = nonce_in_project_tree(fixture.project, fixture.nonce)
    return {
        "nonce_in_prompt": fixture.nonce in PROMPT,
        "nonce_in_project_tree": hits_in_project,
        "clean": not hits_in_project and fixture.nonce not in PROMPT,
    }


# --------------------------------------------------------------------------
# Running the real session
# --------------------------------------------------------------------------


def build_claude_argv(claude_binary: Path, model: str, effort: str, max_budget_usd: float) -> list[str]:
    return claude_argv(claude_binary, PROMPT, "Read,Edit,Write", model, effort, max_budget_usd)


def run_session(fixture: Fixture, claude_binary: Path, model: str, effort: str, max_budget_usd: float, timeout: int) -> subprocess.CompletedProcess:
    env = session_env(fixture, {"MEMCONTINUUM_HOME": str(fixture.mc_home)})
    argv = build_claude_argv(claude_binary, model, effort, max_budget_usd)
    return run_claude(argv, fixture, env, timeout)


# --------------------------------------------------------------------------
# Ordering analysis -- pure function over a parsed transcript, no live
# session needed. This is the reusable, unit-testable core.
# --------------------------------------------------------------------------


def analyze_nonce_ordering(events: list[dict[str, Any]], nonce: str) -> dict[str, Any]:
    """Measure transcript EMISSION ORDER (message index in the
    `--output-format stream-json --include-hook-events` stream), not
    request order as such: `hook_started`/`hook_response` are the CLI's
    own record of when the hook actually ran relative to the surrounding
    assistant/tool_use messages, not something this harness has to infer
    indirectly. The
    decisive comparison is `hook_started_index` (when the hook that gates
    the edit actually started) against
    `first_edit_write_tool_use_index` (when the model's OWN Edit/Write
    tool_use block -- already carrying its old_string/new_string content --
    first appears): if the hook starts only after that index, the model
    had already committed to the edit's content before the hook ran at
    all, independent of when its context later reaches the model.

    `first_nonce_in_assistant_message_index` is the second, independent
    fact: the first index at which the model's own visible output (an
    `assistant` message's `text` block) contains the nonce -- i.e. the
    model has actually seen and can quote the hook's context, not just
    that the hook ran.
    """
    first_tool_use_index: int | None = None
    first_edit_write_tool_use_index: int | None = None
    hook_started_index: int | None = None
    hook_response_index: int | None = None
    hook_response_has_nonce = False
    nonce_assistant_hits: list[int] = []
    nonce_anywhere_hits: list[int] = []

    for index, event in enumerate(events):
        etype = event.get("type")
        raw = json.dumps(event)
        if nonce in raw:
            nonce_anywhere_hits.append(index)

        if etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use":
                    if first_tool_use_index is None:
                        first_tool_use_index = index
                    if block.get("name") in ("Edit", "Write") and first_edit_write_tool_use_index is None:
                        first_edit_write_tool_use_index = index
                elif block.get("type") == "text" and nonce in block.get("text", ""):
                    nonce_assistant_hits.append(index)
        elif etype == "system" and event.get("subtype") == "hook_started":
            if hook_started_index is None:
                hook_started_index = index
        elif etype == "system" and event.get("subtype") == "hook_response":
            if hook_response_index is None:
                hook_response_index = index
            if nonce in raw:
                hook_response_has_nonce = True

    gate_index = first_edit_write_tool_use_index

    def before(hit: int) -> bool:
        return gate_index is not None and hit < gate_index

    def after(hit: int) -> bool:
        return gate_index is not None and hit >= gate_index

    return {
        "first_tool_use_index": first_tool_use_index,
        "first_edit_write_tool_use_index": first_edit_write_tool_use_index,
        "hook_started_index": hook_started_index,
        "hook_response_index": hook_response_index,
        "hook_response_has_nonce": hook_response_has_nonce,
        "hook_started_after_gated_tool_use": (
            hook_started_index is not None
            and gate_index is not None
            and hook_started_index > gate_index
        ),
        "first_nonce_in_assistant_message_index": (
            nonce_assistant_hits[0] if nonce_assistant_hits else None
        ),
        "nonce_seen_before_first_edit_write_tool_use": any(before(h) for h in nonce_assistant_hits),
        "nonce_seen_after_first_edit_write_tool_use": any(after(h) for h in nonce_assistant_hits),
        "nonce_seen_never_by_model": not nonce_assistant_hits,
        "nonce_seen_anywhere_in_transcript_indices": nonce_anywhere_hits,
        "note": (
            "gate_index is the first Edit/Write tool_use block's index -- the "
            "call the hook actually fires for. 'before'/'after' compare "
            "against THAT index, not the first tool_use of any kind (a Read "
            "call the hook never gates must not count as the gate). "
            "hook_started_after_gated_tool_use is the architectural fact: "
            "the model had already emitted the tool_use (with its final "
            "old_string/new_string) before the hook that could have informed "
            "it even started, independent of when the model later sees the "
            "hook's text."
        ),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=str(ENGINE_ROOT / ".venv" / "bin" / "python"), help="engine venv python for memidx.py")
    parser.add_argument("--claude-binary", default=None, help="absolute path to the claude executable")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--keep", action="store_true", help="do not delete the /tmp fixture afterwards")
    parser.add_argument("--analyze-only", action="store_true", help="skip the fixture/session; analyze a saved transcript")
    parser.add_argument("--transcript", help="path to a saved stream-json transcript (with --analyze-only)")
    parser.add_argument("--nonce", help="the nonce to search for (with --analyze-only)")
    args = parser.parse_args(argv)

    if args.analyze_only:
        if not args.transcript or not args.nonce:
            parser.error("--analyze-only requires --transcript and --nonce")
        events = parse_transcript(Path(args.transcript).read_text(encoding="utf-8"))
        print(json.dumps(analyze_nonce_ordering(events, args.nonce), indent=2))
        return 0

    python = Path(args.python)
    if not python.exists():
        parser.error(f"engine python not found at {python}; pass --python")
    claude_binary = Path(args.claude_binary) if args.claude_binary else _default_claude_binary()

    root = Path(tempfile.mkdtemp(prefix="mc-nonce-fixture-"))
    report: dict[str, Any] = {"fixture_root": str(root)}
    try:
        fixture = build_fixture(root, python)
        report["nonce"] = fixture.nonce
        report["preflight"] = preflight_hook(fixture, python)
        report["contamination_check"] = contamination_check(fixture)
        if not report["preflight"]["outcome_matched"] or not report["preflight"]["nonce_in_stdout"]:
            report["status"] = "fixture-broken"
            print(json.dumps(report, indent=2))
            return 1
        if not report["contamination_check"]["clean"]:
            report["status"] = "contaminated"
            print(json.dumps(report, indent=2))
            return 1

        completed = run_session(fixture, claude_binary, args.model, args.effort, args.max_budget_usd, args.timeout)
        report["session_returncode"] = completed.returncode
        report["session_stderr_tail"] = completed.stderr[-2000:]
        events = parse_transcript(completed.stdout)
        report["transcript_event_count"] = len(events)
        report["nonce_ordering"] = analyze_nonce_ordering(events, fixture.nonce)
        report["governed_file_final_content"] = fixture.governed.read_text(encoding="utf-8")
        report["status"] = "ok"
        print(json.dumps(report, indent=2))
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
