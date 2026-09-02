"""Tests for `memidx.py stats` -- the liveness metric (backlog SS2,
INC-0103/INC-0105): reads hook.log and reports, per project, whether the
read side (pre-edit lookups) and the write side (nudges -> real store
commits) are alive within a trailing window.

Exercised in-process via memidx.cmd_stats (argparse.Namespace, like
TestUnmappedCommand in tests/test_write_hooks.py) -- no subprocess needed,
since `stats` never touches python-environment-dependent hook machinery,
just a plain text file and (optionally) `git log`.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)


def ts(hours_ago: float, when: datetime = NOW) -> str:
    return (when - timedelta(hours=hours_ago)).isoformat()


def args(**kw):
    base = dict(
        project="demo", days=7, home=None, store=None, json=False,
        now=NOW.isoformat(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def run_stats(**kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = memidx.cmd_stats(args(**kw))
    return rc, buf.getvalue()


def run_stats_json(**kw):
    rc, out = run_stats(json=True, **kw)
    return rc, json.loads(out)


class StatsTestBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="memcontinuum-stats-")
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)
        self.home = Path(self.td) / "home"
        self.home.mkdir()

    def write_log(self, lines):
        (self.home / "hook.log").write_text("\n".join(lines) + "\n")

    def git_store(self, name="store", commit_dates=()):
        """A real git repo with one commit per date in `commit_dates`
        (datetime, backdated via GIT_AUTHOR_DATE/GIT_COMMITTER_DATE so `git
        log --since` sees a real historical date, not "now")."""
        store = Path(self.td) / name
        store.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=store, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=store, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=store, check=True)
        for i, d in enumerate(commit_dates):
            iso = d.strftime("%Y-%m-%dT%H:%M:%S")
            env = dict(os.environ)
            env["GIT_AUTHOR_DATE"] = iso
            env["GIT_COMMITTER_DATE"] = iso
            subprocess.run(
                ["git", "commit", "-q", "--allow-empty", "-m", f"c{i}"],
                cwd=store, check=True, env=env,
            )
        if not commit_dates:
            subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=store, check=True)
        return store


class TestStatsHealthyCase(StatsTestBase):
    def test_missing_hook_log_prints_message_and_exits_0(self):
        rc, out = run_stats(home=str(self.home))
        self.assertEqual(rc, 0)
        self.assertIn(f"no hook.log at {self.home / 'hook.log'}", out)

    def test_home_defaults_to_env_var(self):
        env_home = Path(self.td) / "env-home"
        env_home.mkdir()
        old = os.environ.get("MEMCONTINUUM_HOME")
        os.environ["MEMCONTINUUM_HOME"] = str(env_home)
        try:
            rc, out = run_stats(home=None)
        finally:
            if old is None:
                os.environ.pop("MEMCONTINUUM_HOME", None)
            else:
                os.environ["MEMCONTINUUM_HOME"] = old
        self.assertEqual(rc, 0)
        self.assertIn(str(env_home / "hook.log"), out)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permission bits")
    def test_exists_but_unreadable_prints_tolerant_message_not_internal_error(self):
        """Fix round 1 (review finding, IMPORTANT): _scan_hook_log's OSError
        branch used to return a 5-tuple (a duplicated unparseable_lines)
        while the normal path returns 4 and cmd_stats always unpacks 4 --
        an existing-but-unreadable hook.log (chmod 000; permissions, a
        mid-rotation window, anything read_text can raise OSError for)
        crashed with "too many values to unpack" INSIDE the try/except
        meant to make this fail open, surfacing as "stats: internal error
        (...)" instead of a real message. Reproduced exactly as the
        reviewer described: chmod 000 an existing hook.log."""
        log_path = self.home / "hook.log"
        log_path.write_text(f"{ts(1)} userprompt outcome=injected session=s1 project=demo\n")
        log_path.chmod(0o000)
        self.addCleanup(log_path.chmod, 0o644)
        rc, out = run_stats(home=str(self.home))
        self.assertEqual(rc, 0)
        self.assertNotIn("internal error", out)
        self.assertIn("hook.log exists but is unreadable at", out)
        self.assertIn(str(log_path), out)

    def test_healthy_case_no_flags(self):
        lines = [
            f"{ts(1)} sessionstart outcome=init session=s1 source=startup project=demo",
            f"{ts(1)} userprompt outcome=injected session=s1 project=demo",
            f"{ts(1)} userprompt outcome=lookback-injected turn=4 since=4 count=1 session=s1 project=demo",
            f"{ts(1)} ledger outcome=appended kind=code session=s1 file=/x.py project=demo",
            f"{ts(1)} ledger outcome=appended kind=store session=s1 file=/y.md project=demo",
            f"{ts(1)} outcome=matched elapsed=0s project=demo file=/z.py",
        ]
        self.write_log(lines)
        store = self.git_store(commit_dates=[NOW - timedelta(hours=2)])
        rc, out = run_stats_json(home=str(self.home), store=str(store))
        self.assertEqual(rc, 0)
        self.assertEqual(out["flags"], [])
        self.assertEqual(out["sessions_seen"], 1)
        self.assertEqual(out["user_prompts"], 2)
        self.assertEqual(out["nudges"]["coverage_injected"], 1)
        self.assertEqual(out["nudges"]["lookback_injected"], 1)
        self.assertEqual(out["nudges"]["total"], 2)
        self.assertEqual(out["pre_edit"]["matched"], 1)
        self.assertEqual(out["ledger_appends"]["code"], 1)
        self.assertEqual(out["ledger_appends"]["store"], 1)
        self.assertEqual(out["store_commits"], 1)

    def test_pre_edit_no_match_and_other_classified_separately(self):
        """The named fields (matched/no_match/other/total) are VIEWS over
        the dynamic per-outcome `outcomes` dict (fix round 1) -- assert
        both: the named subset the rest of this file relies on, and that
        the two distinct "other" outcomes (index-missing, no-file-path)
        are still individually visible in `outcomes`, not folded into one
        opaque "other" count with the literal strings lost."""
        lines = [
            f"{ts(1)} outcome=matched elapsed=0s project=demo file=/a.py",
            f"{ts(1)} outcome=no-match elapsed=0s project=demo file=/b.py",
            f"{ts(1)} outcome=index-missing elapsed=0s project=demo db=/x.sqlite",
            f"{ts(1)} outcome=no-file-path elapsed=0s project=demo file=",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        pe = out["pre_edit"]
        self.assertEqual(
            {k: v for k, v in pe.items() if k != "outcomes"},
            {"matched": 1, "no_match": 1, "other": 2, "total": 4},
        )
        self.assertEqual(
            pe["outcomes"],
            {"matched": 1, "no-match": 1, "index-missing": 1, "no-file-path": 1},
        )

    def test_memlib_raw_outcome_lines_not_misclassified_as_pre_edit(self):
        """The false-positive advisor caught: memlib.sh's own raw
        lock-timeout/lock-open-failed/state-dir-failed lines (mc_log,
        no hook-type keyword) start with `outcome=` exactly like
        pre-edit-chain.sh's finish() -- but never carry `elapsed=`. If the
        classifier only checked for a bare `outcome=` prefix, these would
        count as pre-edit lookups (`other`) and could suppress the
        INC-0103 FLAG -- the exact false negative this metric exists to
        prevent."""
        lines = [
            f"{ts(1)} outcome=lock-timeout file=/x.json.lock project=demo",
            f"{ts(1)} outcome=lock-open-failed file=/x.json.lock project=demo",
            f"{ts(1)} outcome=state-dir-failed dir=/x project=demo",
        ] + [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["pre_edit"]["total"], 0)
        self.assertEqual(out["user_prompts"], 10)
        self.assertIn("FLAG: read side silent (INC-0103 class)", out["flags"])

    def test_new_file_nudge_outcomes(self):
        """The four named fields are a view over `outcomes` (fix round 1);
        a fifth outcome (existing-or-symlink) -- one of newfile-nudge.sh's
        other nine possible outcomes, none of which had a named field --
        must still be visible in `outcomes`, never silently dropped."""
        lines = [
            f"{ts(1)} newfile-nudge outcome=nudged project=demo file=/a.swift",
            f"{ts(1)} newfile-nudge outcome=not-indexed-extension project=demo file=/b.md",
            f"{ts(1)} newfile-nudge outcome=language-available-not-wired project=demo file=/c.py",
            f"{ts(1)} newfile-nudge outcome=never-extension project=demo file=/d.txt",
            f"{ts(1)} newfile-nudge outcome=existing-or-symlink project=demo file=/e.swift",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        nf = out["newfile_nudge"]
        self.assertEqual(
            {k: v for k, v in nf.items() if k != "outcomes"},
            {
                "nudged": 1, "not_indexed_extension": 1,
                "language_available_not_wired": 1, "never_extension": 1,
            },
        )
        self.assertEqual(nf["outcomes"].get("existing-or-symlink"), 1)
        self.assertEqual(sum(nf["outcomes"].values()), 5)

    def test_never_raises_on_malformed_or_unrelated_lines(self):
        lines = [
            "payload_keys=session_id,agent_id project=demo",
            f"{NOW.strftime('%Y-%m-%dT%H:%M:%S')} outcome=watchdog-killed hook=userprompt-remind.sh",
            "not a log line at all !!! {{{",
            "",
            f"{ts(1)} userprompt outcome=injected session=s1 project=demo",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(rc, 0)
        self.assertEqual(out["nudges"]["coverage_injected"], 1)

    def test_unparseable_lines_counted_as_self_liveness_signal(self):
        """A reviewer finding: lines whose leading token isn't an
        ISO-with-offset timestamp are silently SKIPPED from every other
        count by design (payload_keys=..., watchdog-killed, tracebacks) --
        but that same silence would also hide a genuinely broken host (e.g.
        `date -Iseconds` unsupported, every real hook.log write falling
        back to a bare `date` format) as an indistinguishable all-zero
        report. unparseable_lines must count them (never silently) so an
        operator can tell "nothing happened" apart from "this metric can't
        read this log". The three non-ISO lines from the malformed-lines
        fixture above are exactly this."""
        lines = [
            "payload_keys=session_id,agent_id project=demo",
            f"{NOW.strftime('%Y-%m-%dT%H:%M:%S')} outcome=watchdog-killed hook=userprompt-remind.sh",
            "not a log line at all !!! {{{",
            f"{ts(1)} userprompt outcome=injected session=s1 project=demo",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["unparseable_lines"], 3)
        rc, text = run_stats(home=str(self.home))
        self.assertIn("unparseable lines skipped", text)
        self.assertIn("3", text.split("unparseable lines skipped")[1].splitlines()[0])

    def test_no_unparseable_lines_omits_the_line_in_text_output(self):
        self.write_log([f"{ts(1)} userprompt outcome=injected session=s1 project=demo"])
        rc, text = run_stats(home=str(self.home))
        self.assertNotIn("unparseable lines skipped", text)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["unparseable_lines"], 0)

    def test_store_missing_repo_is_unmeasured_not_a_crash(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(3)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home), store=str(Path(self.td) / "no-such-store"))
        self.assertEqual(rc, 0)
        self.assertIsNone(out["store_commits"])
        self.assertEqual(out["flags"], [], "no --store measurement means no FLAG, never a guessed zero")


class TestStatsFlags(StatsTestBase):
    def test_write_side_silent_flag(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(4)]
        self.write_log(lines)
        store = self.git_store(commit_dates=[NOW - timedelta(days=400)])  # outside window
        rc, out = run_stats_json(home=str(self.home), store=str(store))
        self.assertEqual(out["store_commits"], 0)
        self.assertEqual(out["nudges"]["total"], 4)
        self.assertIn("FLAG: write side silent — 4 nudges, 0 store writes in 7d (INC-0105 class)", out["flags"])

    def test_write_side_flag_needs_at_least_three_nudges(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(2)]
        self.write_log(lines)
        store = self.git_store(commit_dates=[NOW - timedelta(days=400)])
        rc, out = run_stats_json(home=str(self.home), store=str(store))
        self.assertEqual(out["flags"], [])

    def test_write_side_flag_absent_without_store(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(5)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertIsNone(out["store_commits"])
        self.assertEqual(out["flags"], [])

    def test_read_side_silent_flag(self):
        lines = [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["pre_edit"]["total"], 0)
        self.assertIn("FLAG: read side silent (INC-0103 class)", out["flags"])

    def test_read_side_flag_needs_at_least_ten_prompts(self):
        lines = [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(9)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["flags"], [])

    def test_read_side_flag_absent_when_pre_edit_matches_exist(self):
        lines = [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(10)]
        lines.append(f"{ts(1)} outcome=matched elapsed=0s project=demo file=/z.py")
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["flags"], [])

    def test_both_flags_can_fire_together(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        store = self.git_store(commit_dates=[NOW - timedelta(days=400)])
        rc, out = run_stats_json(home=str(self.home), store=str(store))
        self.assertEqual(len(out["flags"]), 2)
        self.assertTrue(any("INC-0105" in f for f in out["flags"]))
        self.assertTrue(any("INC-0103" in f for f in out["flags"]))


class TestStatsWindowEdges(StatsTestBase):
    def test_line_inside_window_counted(self):
        self.write_log([f"{ts(6 * 24 + 23)} userprompt outcome=injected session=s1 project=demo"])
        rc, out = run_stats_json(home=str(self.home), days=7)
        self.assertEqual(out["nudges"]["coverage_injected"], 1)

    def test_line_older_than_window_ignored(self):
        self.write_log([f"{ts(7 * 24 + 1)} userprompt outcome=injected session=s1 project=demo"])
        rc, out = run_stats_json(home=str(self.home), days=7)
        self.assertEqual(out["nudges"]["coverage_injected"], 0)
        self.assertEqual(out["user_prompts"], 0)

    def test_line_at_now_included(self):
        self.write_log([f"{NOW.isoformat()} userprompt outcome=injected session=s1 project=demo"])
        rc, out = run_stats_json(home=str(self.home), days=7)
        self.assertEqual(out["nudges"]["coverage_injected"], 1)

    def test_future_line_beyond_now_excluded(self):
        future = (NOW + timedelta(hours=1)).isoformat()
        self.write_log([f"{future} userprompt outcome=injected session=s1 project=demo"])
        rc, out = run_stats_json(home=str(self.home), days=7)
        self.assertEqual(out["nudges"]["coverage_injected"], 0)

    def test_days_override_widens_window(self):
        self.write_log([f"{ts(20 * 24)} userprompt outcome=injected session=s1 project=demo"])
        rc, out = run_stats_json(home=str(self.home), days=7)
        self.assertEqual(out["nudges"]["coverage_injected"], 0)
        rc, out = run_stats_json(home=str(self.home), days=30)
        self.assertEqual(out["nudges"]["coverage_injected"], 1)


class TestStatsUnknownProject(StatsTestBase):
    def test_legacy_no_project_lines_bucketed_under_unknown_not_dropped(self):
        lines = [
            f"{ts(1)} userprompt outcome=injected session=s1",  # no project=
            f"{ts(1)} userprompt outcome=injected session=s2",  # no project=
            f"{ts(1)} userprompt outcome=injected session=s3 project=demo",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home), project="demo")
        self.assertEqual(out["nudges"]["coverage_injected"], 1, "unknown lines must not count toward demo")
        self.assertEqual(out["unknown_lines"], 2)
        self.assertIn("demo", out["projects_seen"])
        self.assertIn("(unknown)", out["projects_seen"])

        rc, out2 = run_stats_json(home=str(self.home), project="(unknown)")
        self.assertEqual(out2["nudges"]["coverage_injected"], 2, "requesting (unknown) surfaces the legacy lines")

    def test_wrong_project_name_reports_zero_not_someone_elses_data(self):
        self.write_log([f"{ts(1)} userprompt outcome=injected session=s1 project=shotporter"])
        rc, out = run_stats_json(home=str(self.home), project="demo")
        self.assertEqual(out["nudges"]["coverage_injected"], 0)
        self.assertIn("shotporter", out["projects_seen"])


class TestStatsTextOutput(StatsTestBase):
    def test_plain_text_contains_key_lines(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(3)]
        self.write_log(lines)
        rc, out = run_stats(home=str(self.home))
        self.assertEqual(rc, 0)
        self.assertIn("MemContinuum liveness stats", out)
        self.assertIn("project=demo", out)
        self.assertIn("nudges → store writes: 3 → ?", out)
        self.assertIn("(unknown) lines skipped", out)

    def test_exit_code_always_zero_even_on_flags(self):
        lines = [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats(home=str(self.home))
        self.assertEqual(rc, 0)
        self.assertIn("FLAG: read side silent", out)


if __name__ == "__main__":
    unittest.main()
