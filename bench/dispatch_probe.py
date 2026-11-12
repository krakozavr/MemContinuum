#!/usr/bin/env python
"""dispatch_probe -- can a PreToolUse hook append to the prompt a dispatched
subagent receives?  Measured on a real, non-interactive Claude Code session
(TOP-0144 L1, "delivery at the prompt boundary": step 1 is this probe).

Reuses, by import, the machinery of bench/pretooluse_ordering_probe.py
(`parse_transcript`, `_default_claude_binary`) and the scenario-driven stand-in
hook bench/pretooluse_probe_hook.py (its X* scenarios); the fixture shape
(disposable HOME / CLAUDE_CONFIG_DIR / TMPDIR, project-scoped settings, a
per-run nonce that lives only in a file outside the project tree and is never
in the prompt) follows bench/pretooluse_rewrite_deny_probe.py.

Three independent evidence sources per run, never a model's self-report alone:
  1. transcript event order (stream-json + hook events),
  2. the stand-in hook's own call log (whole payload of every event),
  3. what the subagent demonstrably received: out/received.txt, written by the
     subagent, lists the lines it found beginning with MCPROBE-LINE; the
     orchestrator cannot supply the nonce, only the hook can.
Plus the on-disk session / subagent transcripts under CLAUDE_CONFIG_DIR.

Usage:
    PYTHONPATH= <python> bench/dispatch_probe.py --scenario X1 [--keep]
    ... --scenario {X0,X1,X2,X3,X4,X5,X6} [--invalid wrongtype|missing|extra]
        [--append-bytes N] [--sleep S] [--out DIR] [--subagent-ctx]
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

BENCH_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = BENCH_DIR.parent
PROBE_HOOK = BENCH_DIR / "pretooluse_probe_hook.py"

sys.path.insert(0, str(BENCH_DIR))
from pretooluse_ordering_probe import _default_claude_binary, parse_transcript  # noqa: E402

SCENARIOS = ("X0", "X1", "X2", "X3", "X4", "X5", "X6")
MARKER = "MCPROBE-LINE"
RECEIVED_RELPATH = "out/received.txt"
ORCH_RELPATH = "out/orchestrator.txt"
HOOK_EVENTS = ("PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop")

# The brief handed to the subagent. The marker word appears mid-sentence and
# never at the start of a line, so the ORIGINAL text cannot itself satisfy
# "a line beginning with the marker".
SUBAGENT_PROMPT = (
    "You are a probe subagent in an isolated fixture. Work only inside the current project directory.\n"
    "Step 1: scan the complete text of your instructions (this message and anything else you were given)\n"
    f"for lines that begin with the word {MARKER}. Write every such line, verbatim, one per line, to\n"
    f"{RECEIVED_RELPATH} with the Write tool; if there is no such line write the single word NONE.\n"
    "Step 2: run the shell command `echo subagent-bash-probe` with the Bash tool.\n"
    "Step 3: reply with the single word DONE."
)

ORCH_PROMPT = """This is an isolated measurement fixture. Work only inside this project directory.

1. Dispatch exactly one subagent with your subagent-dispatch tool. Give it a short description
   and pass as its prompt exactly the text between the two lines of equals signs, unchanged:
==========
{subprompt}
==========
2. When it returns, write a file {orch} (Write tool) containing every line, verbatim, from anything
   in your own context that begins with the word {marker} (apart from the text above); if there is
   none write the single word NONE.
3. Finish with one message that starts with BRIEF-SENT: followed by the exact prompt you passed to
   the subagent, then one sentence on what the subagent returned.
Do not search for hook or settings files.
"""


def generate_nonce() -> str:
    return f"MC-DISP-{secrets.token_hex(12)}"


class Fixture:
    def __init__(self, root: Path, scenario: str, nonce: str):
        self.root = root
        self.scenario = scenario
        self.nonce = nonce
        self.home = root / "home"
        self.claude_config = root / "claude-config"
        self.tmp = root / "tmp"
        self.project = root / "project"
        self.settings = self.project / ".claude" / "settings.json"
        self.hook_log = root / "hook-calls.jsonl"
        self.hook_counter = root / "hook-counter.txt"
        self.nonce_file = root / "nonce.txt"
        self.received = self.project / RECEIVED_RELPATH
        self.orch = self.project / ORCH_RELPATH


BACKGROUND_NOTE = (
    "   Set the dispatch tool's run_in_background parameter to true so the subagent runs in the background;\n"
    "   then wait until you have been told it finished (or until you can see it did) before step 2.\n"
)


def orchestrator_prompt(background: bool = False) -> str:
    text = ORCH_PROMPT.format(subprompt=SUBAGENT_PROMPT, orch=ORCH_RELPATH, marker=MARKER)
    if background:
        text = text.replace("==========\n2.", "==========\n" + BACKGROUND_NOTE + "2.", 1)
    return text


def build_hook_command(fixture: Fixture, python: Path, knobs: dict[str, str]) -> str:
    env = {
        "PROBE_SCENARIO": fixture.scenario,
        "PROBE_NONCE_FILE": str(fixture.nonce_file),
        "PROBE_LOG": str(fixture.hook_log),
        "PROBE_COUNTER": str(fixture.hook_counter),
        **knobs,
    }
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
    return f"{prefix} {shlex.quote(str(python))} {shlex.quote(str(PROBE_HOOK))}"


def build_settings(command: str, timeout: int, pre_matcher: str = ".*") -> dict[str, Any]:
    """Every event of interest wired to the one stand-in hook; PreToolUse with
    matcher `.*` so the subagent's OWN tool calls are logged too (Q6)."""
    entry = lambda matcher: [  # noqa: E731
        {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    ]
    return {
        "hooks": {
            "PreToolUse": entry(pre_matcher),
            "PostToolUse": entry(".*"),
            "SubagentStart": entry(".*"),
            "SubagentStop": entry(".*"),
        }
    }


def build_fixture(root: Path, python: Path, scenario: str, knobs: dict[str, str], nonce: str | None = None, hook_timeout: int = 30, pre_matcher: str = ".*") -> Fixture:
    nonce = nonce or generate_nonce()
    fixture = Fixture(root, scenario, nonce)
    for path in (fixture.home, fixture.claude_config, fixture.tmp, fixture.project / "out", fixture.project / ".claude"):
        path.mkdir(parents=True, exist_ok=True)
    fixture.hook_counter.write_text("0", encoding="utf-8")
    fixture.nonce_file.write_text(nonce, encoding="utf-8")  # outside project/, never in the prompt
    fixture.settings.write_text(
        json.dumps(build_settings(build_hook_command(fixture, python, knobs), hook_timeout, pre_matcher), indent=2) + "\n",
        encoding="utf-8",
    )
    return fixture


def contamination_check(fixture: Fixture) -> dict[str, Any]:
    hits = []
    for path in fixture.project.rglob("*"):
        if path.is_file() and fixture.nonce in path.read_text(encoding="utf-8", errors="ignore"):
            hits.append(str(path))
    prompts = orchestrator_prompt()
    return {"nonce_in_prompt": fixture.nonce in prompts, "nonce_in_project_tree": hits,
            "clean": not hits and fixture.nonce not in prompts}


def build_claude_argv(claude_binary: Path, model: str, effort: str, max_budget_usd: float, extra: list[str] | None = None, background: bool = False) -> list[str]:
    return [
        str(claude_binary), "-p", orchestrator_prompt(background),
        "--output-format", "stream-json", "--include-hook-events", "--verbose",
        "--strict-mcp-config", "--setting-sources", "project",
        "--tools", "Agent,Read,Write,Bash",
        "--allowedTools", "Bash(echo *)",
        "--permission-mode", "acceptEdits",
        "--model", model, "--effort", effort,
        "--max-budget-usd", str(max_budget_usd),
        *(extra or []),
    ]


def run_session(fixture: Fixture, claude_binary: Path, model: str, effort: str, max_budget_usd: float, timeout: int, extra: list[str] | None = None, background: bool = False) -> tuple[subprocess.CompletedProcess, float]:
    env = {
        "HOME": str(fixture.home), "CLAUDE_CONFIG_DIR": str(fixture.claude_config), "TMPDIR": str(fixture.tmp),
        "PATH": os.environ.get("PATH", ""), "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    }
    argv = build_claude_argv(claude_binary, model, effort, max_budget_usd, extra, background)
    t0 = time.time()
    try:
        done = subprocess.run(argv, cwd=str(fixture.project), env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", "ignore") if isinstance(exc.stdout, (bytes, bytearray)) else (exc.stdout or "")
        done = subprocess.CompletedProcess(argv, returncode=-1, stdout=out, stderr=f"TIMEOUT after {timeout}s")
    return done, time.time() - t0


# ---- analysis: pure functions over the transcript, hook log and disk -------


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def dispatch_tool_uses(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tool_use blocks in the ORCHESTRATOR stream that are not one of the plain
    tools the fixture offers -- i.e. the dispatch call, whatever it is named."""
    plain = {"Read", "Write", "Bash", "Edit"}
    found = []
    for index, ev in enumerate(events):
        if ev.get("type") != "assistant":
            continue
        for block in ev.get("message", {}).get("content", []) or []:
            if block.get("type") == "tool_use" and block.get("name") not in plain:
                found.append({"index": index, "id": block.get("id"), "name": block.get("name"),
                              "input": block.get("input"), "parent_tool_use_id": ev.get("parent_tool_use_id")})
    return found


def tool_results(events: list[dict[str, Any]], tool_use_id: str | None) -> list[dict[str, Any]]:
    out = []
    for index, ev in enumerate(events):
        if ev.get("type") != "user":
            continue
        content = ev.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result" and block.get("tool_use_id") == tool_use_id:
                out.append({"index": index, "is_error": block.get("is_error"),
                            "text": json.dumps(block.get("content"))[:1500]})
    return out


def nonce_paths(node: Any, nonce: str, prefix: str = "") -> list[str]:
    """JSON key paths (list indices collapsed to []) at which a string value
    contains the nonce -- tells a model-visible field (`message.content[]...`)
    from harness bookkeeping (`tool_use_result.prompt`, a hook's own stdout)."""
    found: list[str] = []
    if isinstance(node, str):
        if nonce in node:
            found.append(prefix)
    elif isinstance(node, dict):
        for k, v in node.items():
            found += nonce_paths(v, nonce, f"{prefix}.{k}" if prefix else k)
    elif isinstance(node, list):
        for v in node:
            found += nonce_paths(v, nonce, f"{prefix}[]")
    return found


def nonce_hits(events: list[dict[str, Any]], nonce: str) -> list[dict[str, Any]]:
    hits = []
    for index, ev in enumerate(events):
        raw = json.dumps(ev)
        if nonce in raw:
            hits.append({"index": index, "type": ev.get("type"), "subtype": ev.get("subtype"),
                         "parent_tool_use_id": ev.get("parent_tool_use_id"),
                         "paths": sorted(set(nonce_paths(ev, nonce)))})
    return hits


def hook_log_summary(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for rec in log:
        payload = rec.get("payload") or {}
        rows.append({
            "n": rec.get("call_n"), "event": rec.get("event"), "tool": rec.get("tool_name"),
            "payload_keys": sorted(payload.keys()),
            "agent_fields": {k: payload[k] for k in payload if "agent" in k.lower() or k in ("transcript_path", "session_id")},
            "elapsed_s": round((rec.get("ts_end") or 0) - (rec.get("ts") or 0), 3),
            "returned": rec.get("decision_returned"),
        })
    return rows


def disk_transcripts(fixture: Fixture) -> list[dict[str, Any]]:
    """Every session / subagent transcript the CLI wrote under CLAUDE_CONFIG_DIR,
    with the nonce occurrence count -- what a later audit could read."""
    rows = []
    for path in sorted(fixture.claude_config.rglob("*.jsonl")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        by_type: dict[str, int] = {}
        for line in text.splitlines():
            if fixture.nonce in line:
                try:
                    obj = json.loads(line)
                    kind = str(obj.get("type")) + (":" + str(obj["attachment"].get("type")) if isinstance(obj.get("attachment"), dict) else "")
                except (json.JSONDecodeError, AttributeError):
                    kind = "unparsed"
                by_type[kind] = by_type.get(kind, 0) + 1
        rows.append({"records_with_nonce_by_type": by_type, "path": str(path.relative_to(fixture.claude_config)), "bytes": len(text),
                     "nonce_start": text.count(fixture.nonce + "-S"), "nonce_end": text.count(fixture.nonce + "-E"),
                     "nonce_ctx": text.count(fixture.nonce + "-C"), "nonce_subagentstart": text.count(fixture.nonce + "-A")})
    return rows


def read_text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else None


def analyze(fixture: Fixture, events: list[dict[str, Any]]) -> dict[str, Any]:
    log = read_jsonl(fixture.hook_log)
    dispatches = dispatch_tool_uses(events)
    first = dispatches[0] if dispatches else None
    received = read_text(fixture.received)
    result_event = next((e for e in events if e.get("type") == "result"), None)
    return {
        "scenario": fixture.scenario,
        "event_count": len(events),
        "init_tools": next((e.get("tools") for e in events if e.get("subtype") == "init"), None),
        "model": next((e.get("model") for e in events if e.get("subtype") == "init"), None),
        "dispatch_tool_uses": dispatches,
        "dispatch_results": tool_results(events, first["id"]) if first else [],
        "hook_log": hook_log_summary(log),
        "hook_dispatch_payloads": [r.get("payload") for r in log if r.get("event") == "PreToolUse"
                                   and r.get("tool_name") == (first or {}).get("name")],
        "received_file": received,
        "received_has_start": bool(received and fixture.nonce + "-S" in received),
        "received_has_end": bool(received and fixture.nonce + "-E" in received),
        "received_has_ctx": bool(received and fixture.nonce + "-C" in received),
        "received_has_subagentstart": bool(received and fixture.nonce + "-A" in received),
        "orchestrator_file": read_text(fixture.orch),
        "nonce_hits_in_stream": nonce_hits(events, fixture.nonce),
        "disk_transcripts": disk_transcripts(fixture),
        "final_text": (result_event or {}).get("result"),
        "result_subtype": (result_event or {}).get("subtype"),
        "result_is_error": (result_event or {}).get("is_error"),
    }


def run_one(scenario: str, python: Path, claude_binary: Path, model: str, effort: str, max_budget_usd: float,
            timeout: int, out_dir: Path | None, knobs: dict[str, str], extra: list[str] | None = None, background: bool = False, hook_timeout: int = 30, pre_matcher: str = ".*") -> dict[str, Any]:
    root = Path(tempfile.mkdtemp(prefix=f"mc-disp-{scenario.lower()}-"))
    report: dict[str, Any] = {"scenario": scenario, "knobs": knobs, "background": background, "pre_matcher": pre_matcher, "fixture_root": str(root)}
    try:
        fixture = build_fixture(root, python, scenario, knobs, hook_timeout=hook_timeout, pre_matcher=pre_matcher)
        report["contamination_check"] = contamination_check(fixture)
        if not report["contamination_check"]["clean"]:
            report["status"] = "contaminated"
            return report
        report["claude_version"] = subprocess.run([str(claude_binary), "--version"], capture_output=True, text=True).stdout.strip()
        completed, wall = run_session(fixture, claude_binary, model, effort, max_budget_usd, timeout, extra, background)
        report.update(session_returncode=completed.returncode, session_stderr_tail=completed.stderr[-1500:], wall_s=round(wall, 2))
        events = parse_transcript(completed.stdout)
        report["analysis"] = analyze(fixture, events)
        report["status"] = "ok"
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = f"{scenario}{'-bg' if background else ''}{'-m' + ''.join(c for c in pre_matcher if c.isalnum()) if pre_matcher != '.*' else ''}-{'-'.join(f'{k}{v}' for k, v in sorted(knobs.items()))}-{int(time.time())}".strip("-")
            (out_dir / f"{stem}.stream.jsonl").write_text(completed.stdout, encoding="utf-8")
            (out_dir / f"{stem}.hook-calls.jsonl").write_text(fixture.hook_log.read_text(encoding="utf-8") if fixture.hook_log.exists() else "", encoding="utf-8")
            shutil.copytree(fixture.claude_config / "projects", out_dir / f"{stem}.disk-transcripts", dirs_exist_ok=True) if (fixture.claude_config / "projects").exists() else None
            (out_dir / f"{stem}.report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            report["artefacts"] = stem
        return report
    finally:
        shutil.rmtree(root, ignore_errors=True)


def knobs_from_args(args: argparse.Namespace) -> dict[str, str]:
    knobs: dict[str, str] = {}
    if args.invalid:
        knobs["PROBE_INVALID"] = args.invalid
    if args.append_bytes:
        knobs["PROBE_APPEND_BYTES"] = str(args.append_bytes)
    if args.sleep:
        knobs["PROBE_SLEEP"] = str(args.sleep)
    if args.subagent_ctx:
        knobs["PROBE_SUBAGENT_CTX"] = "1"
    if args.prompt_field:
        knobs["PROBE_PROMPT_FIELD"] = args.prompt_field
    return knobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--claude-binary", default=None)
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--max-budget-usd", type=float, default=1.0)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--invalid", choices=("wrongtype", "missing", "extra"))
    parser.add_argument("--append-bytes", type=int, default=0)
    parser.add_argument("--sleep", type=float, default=0)
    parser.add_argument("--subagent-ctx", action="store_true", help="also make the SubagentStart hook return additionalContext")
    parser.add_argument("--prompt-field", default=None, help="name of the dispatch tool's prompt field if it is not `prompt`")
    parser.add_argument("--background", action="store_true", help="ask the orchestrator to dispatch with run_in_background true (Q8)")
    parser.add_argument("--hook-timeout", type=int, default=30, help="timeout (s) written into the hook settings")
    parser.add_argument("--matcher", default=".*", help="PreToolUse matcher written into the settings (Q1: Agent vs Task)")
    parser.add_argument("--out", default=None, help="directory for raw run artefacts")
    parser.add_argument("--extra-arg", action="append", default=[], help="extra claude CLI argument (repeatable)")
    args = parser.parse_args(argv)
    claude_binary = Path(args.claude_binary) if args.claude_binary else _default_claude_binary()
    report = run_one(args.scenario, Path(args.python), claude_binary, args.model, args.effort, args.max_budget_usd,
                     args.timeout, Path(args.out) if args.out else None, knobs_from_args(args), args.extra_arg, args.background, args.hook_timeout, args.matcher)
    print(json.dumps(report, indent=2))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
