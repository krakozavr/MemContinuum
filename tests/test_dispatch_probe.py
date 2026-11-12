"""tests/test_dispatch_probe.py -- unit tests for the plumbing of
bench/dispatch_probe.py and the X* scenarios of bench/pretooluse_probe_hook.py
(TOP-0144 L1, "delivery at the prompt boundary"). No real Claude Code session
is ever started here: the hook is fed synthetic payloads through a subprocess
and the analysis helpers run over hand-written events.
"""
from __future__ import annotations

import json
import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(TOOLS_DIR / "bench"))

from tests.test_update import tmpdir  # noqa: E402 -- the one cleaned-up temp-dir helper

import dispatch_probe as dp  # noqa: E402
import pretooluse_ordering_probe as op  # noqa: E402
import pretooluse_rewrite_deny_probe as rp  # noqa: E402
import probe_fixture as pf  # noqa: E402
import pretooluse_probe_hook as hook  # noqa: E402

HOOK = TOOLS_DIR / "bench" / "pretooluse_probe_hook.py"
NONCE = "MC-DISP-unittest"
DISPATCH_INPUT = {"description": "d", "prompt": "do the thing\n", "run_in_background": False}


class HookCase(unittest.TestCase):
    def run_hook(self, scenario, payload, **knobs):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        (root / "nonce.txt").write_text(NONCE, encoding="utf-8")
        (root / "counter.txt").write_text("0", encoding="utf-8")
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PROBE_SCENARIO": scenario,
            "PROBE_NONCE_FILE": str(root / "nonce.txt"),
            "PROBE_LOG": str(root / "log.jsonl"),
            "PROBE_COUNTER": str(root / "counter.txt"),
            "TMPDIR": str(root),
            **knobs,
        }
        done = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)["hookSpecificOutput"] if done.stdout.strip() else None
        log = [json.loads(line) for line in (root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        return out, log

    @staticmethod
    def pre(tool="Agent", tool_input=None):
        return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input or dict(DISPATCH_INPUT),
                "agent_id": None}


class TestDispatchScenarios(HookCase):
    def test_x0_logs_whole_payload_and_returns_nothing(self):
        out, log = self.run_hook("X0", self.pre())
        self.assertIsNone(out)
        self.assertEqual(log[0]["event"], "PreToolUse")
        self.assertEqual(log[0]["payload"]["tool_input"], DISPATCH_INPUT)

    def test_x1_appends_block_to_whole_object(self):
        out, _ = self.run_hook("X1", self.pre())
        new = out["updatedInput"]
        self.assertEqual(set(new), set(DISPATCH_INPUT))
        self.assertTrue(new["prompt"].startswith(DISPATCH_INPUT["prompt"]))
        self.assertIn(f"MCPROBE-LINE START {NONCE}-S", new["prompt"])
        self.assertIn(f"MCPROBE-LINE END {NONCE}-E", new["prompt"])

    def test_x1_block_size_follows_knob_and_keeps_end_marker_last(self):
        out, _ = self.run_hook("X1", self.pre(), PROBE_APPEND_BYTES="8000")
        prompt = out["updatedInput"]["prompt"]
        self.assertGreater(len(prompt), 8000)
        self.assertLess(len(prompt), 8000 + 1000)
        self.assertIn(f"-E\n[/MC-DELIVERY-BLOCK]", prompt)

    def test_x2_returns_prompt_only(self):
        out, _ = self.run_hook("X2", self.pre())
        self.assertEqual(set(out["updatedInput"]), {"prompt"})

    def test_x3_invalid_kinds(self):
        wrong, _ = self.run_hook("X3", self.pre(), PROBE_INVALID="wrongtype")
        self.assertIsInstance(wrong["updatedInput"]["prompt"], int)
        missing, _ = self.run_hook("X3", self.pre(), PROBE_INVALID="missing")
        self.assertNotIn("prompt", missing["updatedInput"])
        extra, _ = self.run_hook("X3", self.pre(), PROBE_INVALID="extra")
        self.assertIn("mc_unknown_field", extra["updatedInput"])

    def test_x4_context_only_and_x5_context_plus_append(self):
        ctx_only, _ = self.run_hook("X4", self.pre())
        self.assertNotIn("updatedInput", ctx_only)
        self.assertEqual(ctx_only["additionalContext"], f"MCPROBE-LINE ctx {NONCE}-C")
        both, _ = self.run_hook("X5", self.pre())
        self.assertIn("updatedInput", both)
        self.assertIn("additionalContext", both)

    def test_x6_only_subagent_start_returns_context(self):
        start, _ = self.run_hook("X6", {"hook_event_name": "SubagentStart", "agent_id": "a1"})
        self.assertEqual(start["hookEventName"], "SubagentStart")
        self.assertIn(f"{NONCE}-A", start["additionalContext"])
        pre, _ = self.run_hook("X6", self.pre())
        self.assertIsNone(pre)

    def test_non_dispatch_tool_is_logged_but_not_rewritten(self):
        out, log = self.run_hook("X1", self.pre(tool="Write", tool_input={"file_path": "f", "content": "c"}))
        self.assertIsNone(out)
        self.assertEqual(log[0]["tool_name"], "Write")

    def test_prompt_field_and_tool_name_knobs(self):
        out, _ = self.run_hook("X1", self.pre(tool="Task", tool_input={"task": "x"}), PROBE_PROMPT_FIELD="task")
        self.assertTrue(out["updatedInput"]["task"].startswith("x"))

    def _in_process(self, **env):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        (root / "counter.txt").write_text("0", encoding="utf-8")
        return root, env

    def test_sleep_knob_is_slept_and_elapsed_is_the_monotonic_difference(self):
        root, env = self._in_process(PROBE_SLEEP="0.3")
        slept = []
        ticks = iter([100.0, 102.5])
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            hook.dispatch_main("X1", self.pre(), NONCE, str(root / "log.jsonl"), str(root / "counter.txt"),
                               sleep=slept.append, monotonic=lambda: next(ticks))
        self.assertEqual(slept, [0.3])
        row = json.loads((root / "log.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(row["elapsed_s"], 2.5)

    def test_no_sleep_knob_means_no_sleep_and_non_dispatch_never_sleeps(self):
        root, env = self._in_process(PROBE_SLEEP="0")
        slept = []
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
            hook.dispatch_main("X1", self.pre(), NONCE, str(root / "log.jsonl"), str(root / "counter.txt"), sleep=slept.append)
        with mock.patch.dict(os.environ, {"PROBE_SLEEP": "9"}), contextlib.redirect_stdout(io.StringIO()):
            hook.dispatch_main("X1", self.pre(tool="Write", tool_input={}), NONCE, str(root / "log.jsonl"),
                               str(root / "counter.txt"), sleep=slept.append)
        self.assertEqual(slept, [])


class TestProbePlumbing(unittest.TestCase):
    def test_original_orchestrator_prompt_cannot_satisfy_the_marker_check(self):
        for background in (False, True):
            text = dp.orchestrator_prompt(background)
            self.assertFalse([ln for ln in text.splitlines() if ln.startswith(dp.MARKER)])
        self.assertIn("run_in_background", dp.orchestrator_prompt(True))
        self.assertNotIn("run_in_background", dp.orchestrator_prompt(False))

    def test_settings_wire_every_event_and_the_matcher(self):
        settings = dp.build_settings("cmd", 7, "Task")["hooks"]
        self.assertEqual(set(settings), set(dp.HOOK_EVENTS))
        self.assertEqual(settings["PreToolUse"][0]["matcher"], "Task")
        self.assertEqual(settings["SubagentStart"][0]["hooks"][0]["timeout"], 7)

    def test_fixture_keeps_nonce_out_of_the_project_tree(self):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        fixture = dp.build_fixture(root, Path(sys.executable), "X1", {"PROBE_SLEEP": "1"}, nonce=NONCE)
        self.assertEqual(fixture.nonce_file.read_text(encoding="utf-8"), NONCE)
        self.assertTrue(dp.contamination_check(fixture)["clean"])
        command = json.loads(fixture.settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertNotIn(NONCE, command)
        self.assertIn("PROBE_SLEEP=1", command)

    def test_nonce_paths_names_the_field(self):
        event = {"message": {"content": [{"text": f"x {NONCE}"}]}, "tool_use_result": {"prompt": NONCE}, "other": "n"}
        self.assertEqual(sorted(dp.nonce_paths(event, NONCE)), ["message.content[].text", "tool_use_result.prompt"])

    def test_dispatch_helpers_over_synthetic_events(self):
        events = [
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Agent", "input": {"prompt": "p"}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "bad"}]}},
        ]
        found = dp.dispatch_tool_uses(events)
        self.assertEqual([f["name"] for f in found], ["Agent"])
        results = dp.tool_results(events, "t1")
        self.assertTrue(results[0]["is_error"])

    def test_hook_log_summary_extracts_agent_fields(self):
        rows = dp.hook_log_summary([{"call_n": 1, "event": "PreToolUse", "tool_name": "Write", "ts": 1.0, "ts_end": 1.5,
                                     "payload": {"agent_id": "a1", "agent_type": "general-purpose", "session_id": "s"}}])
        self.assertEqual(rows[0]["agent_fields"]["agent_id"], "a1")
        self.assertEqual(rows[0]["elapsed_s"], 0.5)


class TestSharedProbeFixture(unittest.TestCase):
    """bench/probe_fixture.py is the one owner of the disposable-session
    machinery; one old-probe scenario and one dispatch scenario go through it."""

    def _captured_session(self, run, fixture):
        seen = {}
        real = subprocess.run

        def fake(argv, **kw):
            seen.update(argv=argv, **kw)
            return subprocess.CompletedProcess(argv, 0, "", "")

        subprocess.run = fake
        try:
            run(fixture)
        finally:
            subprocess.run = real
        return seen

    def test_old_probe_scenario_d4_env_settings_and_argv(self):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        fixture = rp.build_fixture(root, Path(sys.executable), "D4", "MC-RWDN-unittest")
        hook = json.loads(fixture.settings.read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
        self.assertEqual(hook[0]["matcher"], "Edit|Write")
        self.assertEqual(hook[0]["hooks"][0]["timeout"], 10)
        command = hook[0]["hooks"][0]["command"]
        self.assertTrue(command.startswith("PROBE_SCENARIO=D4 PROBE_NONCE_FILE="))
        self.assertIn(f"PROBE_REDIRECT={fixture.redirect}", command)
        self.assertNotIn("MC-RWDN-unittest", command)
        self.assertEqual(fixture.nonce_file.read_text(encoding="utf-8"), "MC-RWDN-unittest")
        self.assertEqual(fixture.hook_counter.read_text(encoding="utf-8"), "0")
        self.assertTrue(rp.contamination_check(fixture)["clean"])
        seen = self._captured_session(lambda f: rp.run_session(f, Path("/c/claude"), "m", "low", 0.5, 5), fixture)
        self.assertEqual(
            seen["env"],
            {"HOME": str(fixture.home), "CLAUDE_CONFIG_DIR": str(fixture.claude_config), "TMPDIR": str(fixture.tmp),
             "PATH": os.environ.get("PATH", ""), "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")},
        )
        self.assertEqual(seen["cwd"], str(fixture.project))
        self.assertEqual(seen["argv"][seen["argv"].index("--tools") + 1], "Read,Edit,Write")
        self.assertNotIn("--allowedTools", seen["argv"])

    def test_old_probe_ordering_session_env_adds_memcontinuum_home(self):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        fixture = op.Fixture(root, "MC-NONCE-unittest")
        seen = self._captured_session(lambda f: op.run_session(f, Path("/c/claude"), "m", "low", 1.0, 5), fixture)
        self.assertEqual(seen["env"]["MEMCONTINUUM_HOME"], str(fixture.mc_home))
        self.assertEqual(set(seen["env"]), {"HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "MEMCONTINUUM_HOME", "PATH", "ANTHROPIC_API_KEY"})
        self.assertEqual(seen["argv"], pf.claude_argv(Path("/c/claude"), op.PROMPT, "Read,Edit,Write", "m", "low", 1.0))

    def test_dispatch_scenario_env_settings_and_argv(self):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        fixture = dp.build_fixture(root, Path(sys.executable), "X1", {"PROBE_SLEEP": "1"}, nonce=NONCE, pre_matcher="Agent")
        settings = json.loads(fixture.settings.read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(settings["PreToolUse"][0]["matcher"], "Agent")
        self.assertEqual(settings["PostToolUse"][0]["matcher"], ".*")
        seen = self._captured_session(lambda f: dp.run_session(f, Path("/c/claude"), "m", "low", 1.0, 5, ["--x"], True), fixture)
        self.assertEqual(set(seen["env"]), {"HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "PATH", "ANTHROPIC_API_KEY"})
        self.assertEqual(seen["stdin"], subprocess.DEVNULL)
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Agent,Read,Write,Bash")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Bash(echo *)")
        self.assertEqual(argv[-1], "--x")

    def test_timeout_is_a_synthetic_result_only_when_asked(self):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        fixture = pf.BaseFixture(root, "n")
        argv = ["claude", "-p", "x"]
        expired = subprocess.TimeoutExpired(argv, 7, output=b"partial\xff")
        make = lambda **k: pf.run_claude(argv, fixture, {"PATH": ""}, 7, **k)  # noqa: E731
        with mock.patch.object(subprocess, "run", side_effect=expired) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                make()
            done = make(catch_timeout=True)
            self.assertEqual((done.returncode, done.stdout, done.stderr), (-1, "partial", "TIMEOUT after 7s"))
            detailed = make(catch_timeout=True, timeout_detail=True)
            self.assertEqual(detailed.stderr, f"TIMEOUT after 7s: {expired}")
            self.assertEqual(run.call_args.kwargs["timeout"], 7)

    def test_shared_jsonl_reader_is_strict_and_the_older_probe_inherits_it(self):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        fixture = rp.build_fixture(root, Path(sys.executable), "D4", "MC-RWDN-unittest")
        fixture.hook_log.write_bytes(b'{"a": 1}\n{"b": "\xff"}\n')
        with self.assertRaises(UnicodeDecodeError):
            pf.read_jsonl(fixture.hook_log)
        with self.assertRaises(UnicodeDecodeError):
            rp.read_hook_log(fixture)
        fixture.hook_log.write_text('{"a": 1}\nnot json\n', encoding="utf-8")
        self.assertEqual(pf.read_jsonl(fixture.hook_log), [{"a": 1}])

    def test_version_lookup_runs_inside_the_fixture_isolation(self):
        root = Path(tmpdir(self, "mc-dispatch-probe-test-"))
        fixture = dp.build_fixture(root, Path(sys.executable), "X1", {}, nonce=NONCE)
        done = subprocess.CompletedProcess([], 0, "2.1.270 (Claude Code)\n", "")
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            self.assertEqual(dp.claude_version(fixture, Path("/c/claude")), "2.1.270 (Claude Code)")
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/c/claude", "--version"])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env, pf.session_env(fixture))
        for key in ("HOME", "CLAUDE_CONFIG_DIR", "TMPDIR"):
            self.assertTrue(env[key].startswith(str(root)), key)
        self.assertEqual(run.call_args.kwargs["cwd"], str(fixture.project))


if __name__ == "__main__":
    unittest.main()
