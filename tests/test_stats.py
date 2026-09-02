"""Tests for `memidx.py stats` -- the liveness metric (backlog SS2,
INC-0103/INC-0105): reads hook.log and reports, per project, whether the
read side (real pre-edit lookups) and the write side (nudges vs. this
project's own store-kind ledger appends) are alive within a trailing
window.

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
        (datetime, backdated/forward-dated via GIT_AUTHOR_DATE/
        GIT_COMMITTER_DATE so `git log --since/--until` sees a real
        historical or future date, not "now")."""
        store = Path(self.td) / name
        store.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=store, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.local"], cwd=store, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=store, check=True)
        for i, d in enumerate(commit_dates):
            # Explicit UTC offset (isoformat(), not a naive strftime with
            # no zone) -- git interprets a zone-less GIT_AUTHOR_DATE as
            # LOCAL system time, not UTC. On a host whose local zone isn't
            # UTC (e.g. America/New_York, -04:00) a naive "wall clock"
            # string silently shifted every backdated/forward-dated commit
            # by the local offset -- invisible in round 1's wide (30-day)
            # windows, but a real bug once round 2 added a tight
            # `--until=now` bound (round-2 fix-round self-catch, surfaced
            # by test_healthy_case_no_flags going from a passing to a
            # failing store_commits assertion after that bound landed).
            iso = d.isoformat()
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
        """Fix round 1 (review finding, IMPORTANT; historical -- at the
        time, _scan_hook_log returned a 4-tuple): the OSError branch used
        to return a 5-tuple (a duplicated unparseable_lines) while the
        normal path returned 4 and cmd_stats always unpacked 4 -- round 2
        later added a genuine 5th value (untimestamped_lines) to both
        branches, kept in sync on purpose. At the time of THIS bug, an
        existing-but-unreadable hook.log (chmod 000; permissions, a
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
        self.assertEqual(out["non_user_prompt_lines"], 0)
        self.assertEqual(out["nudges"]["coverage_injected"], 1)
        self.assertEqual(out["nudges"]["lookback_injected"], 1)
        self.assertEqual(out["nudges"]["total"], 2)
        self.assertEqual(out["pre_edit"]["matched"], 1)
        self.assertEqual(out["pre_edit"]["lookups"], 1)
        self.assertEqual(out["ledger_appends"]["code"], 1)
        self.assertEqual(out["ledger_appends"]["store"], 1)
        self.assertEqual(out["store_commits"], 1)

    def test_pre_edit_no_match_and_other_classified_separately(self):
        """The named fields (matched/no_match/other/total/lookups) are
        VIEWS over the dynamic per-outcome `outcomes` dict (fix round 1) --
        assert both: the named subset the rest of this file relies on, and
        that the two distinct "other" outcomes (index-missing,
        no-file-path) are still individually visible in `outcomes`, not
        folded into one opaque "other" count with the literal strings
        lost. `lookups` (round 2, Codex item 7) counts only matched+
        no-match -- the two "other" outcomes contribute to `other`/`total`
        but NOT to `lookups`."""
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
            {"matched": 1, "no_match": 1, "other": 2, "total": 4, "lookups": 2},
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
        prevent.

        Round-3 addition: these lines (`file=/x.json.lock project=demo`)
        also pin the round-3 fix -- they classify as kind "other" (no
        hook-type keyword, no `elapsed=`), which the round-2 kind=='ledger'
        -only rescue did NOT cover, so their trailing project=demo (mc_log's
        own unconditional append, AFTER file=) used to be silently
        swallowed into the file value and the lines landed in
        "(unknown)" instead of "demo"."""
        lines = [
            f"{ts(1)} outcome=lock-timeout file=/x.json.lock project=demo",
            f"{ts(1)} outcome=lock-open-failed file=/x.json.lock project=demo",
            f"{ts(1)} outcome=state-dir-failed dir=/x project=demo",
        ] + [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["pre_edit"]["total"], 0)
        self.assertEqual(out["pre_edit"]["lookups"], 0)
        self.assertEqual(out["user_prompts"], 10)
        self.assertIn("FLAG: read side silent (INC-0103 class)", out["flags"])
        self.assertNotIn("(unknown)", out["projects_seen"])
        self.assertIn("demo", out["projects_seen"])

    def test_trailing_project_rescued_for_every_kind_not_just_ledger(self):
        """Round 3 (review ruling): the rule is STRUCTURAL, not per-kind --
        mc_log always appends project= as the line's last token, whatever
        kind of line it's logging for. A synthetic kind='other' line whose
        file value itself embeds a FAKE ' project=other' token in the
        middle, but whose line genuinely ENDS with a real ' project=demo'
        token, must attribute to demo -- the fake, non-last occurrence
        must never win."""
        lines = [
            f"{ts(1)} outcome=some-other-outcome file=/tmp/x project=other/mid.txt project=demo",
        ]
        self.write_log(lines)
        rc, demo = run_stats_json(home=str(self.home), project="demo")
        self.assertIn("demo", demo["projects_seen"])
        self.assertNotIn("(unknown)", demo["projects_seen"])
        rc, unknown = run_stats_json(home=str(self.home), project="(unknown)")
        self.assertEqual(unknown["unknown_lines"], 0, "the line must be attributed to demo, not (unknown)")

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

    def test_unparseable_vs_untimestamped_lines_counted_separately(self):
        """Round 2 refinement (reviewer finding): `payload_keys=...` is a
        KNOWN, by-design timestamp-less shape (userprompt-remind.sh's
        payload-shape capture) -- it must NOT tick the `unparseable_lines`
        self-liveness signal (that would make it fire on every single
        healthy run, burying the one number that is supposed to mean
        "something NEW is wrong"). It is counted separately as
        `untimestamped_lines`. A naive/non-ISO timestamp-shaped line and a
        genuinely garbled line both still count as `unparseable_lines`."""
        lines = [
            "payload_keys=session_id,agent_id project=demo",
            "payload_keys=cwd,hook_event_name project=demo",
            f"{NOW.strftime('%Y-%m-%dT%H:%M:%S')} outcome=watchdog-killed hook=userprompt-remind.sh",
            "not a log line at all !!! {{{",
            f"{ts(1)} userprompt outcome=injected session=s1 project=demo",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["untimestamped_lines"], 2)
        self.assertEqual(out["unparseable_lines"], 2)
        rc, text = run_stats(home=str(self.home))
        self.assertIn("unparseable lines skipped", text)
        self.assertIn("untimestamped lines skipped", text)

    def test_no_unparseable_lines_omits_the_line_in_text_output(self):
        self.write_log([f"{ts(1)} userprompt outcome=injected session=s1 project=demo"])
        rc, text = run_stats(home=str(self.home))
        self.assertNotIn("unparseable lines skipped", text)
        self.assertNotIn("untimestamped lines skipped", text)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["unparseable_lines"], 0)
        self.assertEqual(out["untimestamped_lines"], 0)

    def test_bsd_date_fallback_format_parsed_as_local_naive(self):
        """Round 2 review finding: macOS/BSD `date` (no `-Iseconds`
        support -- this repo's documented bash-3.2 port target) prints
        `Tue Sep  2 02:10:00 EDT 2026`, not an ISO offset. Without parsing
        this shape, every real hook.log line on such a host would be
        unparseable -- an INC-0103-class silence of the metric itself on
        exactly the platform this repo was ported to support."""
        local_ts = (NOW - timedelta(hours=1)).astimezone().strftime("%a %b %e %H:%M:%S %Z %Y")
        lines = [f"{local_ts} userprompt outcome=injected session=s1 project=demo"]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["nudges"]["coverage_injected"], 1)
        self.assertEqual(out["unparseable_lines"], 0)

    def test_file_field_containing_project_equals_does_not_hijack_attribution_pre_edit_shape(self):
        """Round 2 Codex gate finding: `file=` is genuinely the LAST field
        on a pre-edit-chain.sh/newfile-nudge.sh line (their own
        independent loggers, never through mc_log) -- `... project=P
        file=F`, project= BEFORE file=. Its value is an arbitrary
        filesystem path that can itself contain `project=`-shaped text.
        Reproduced exactly: a path containing the literal substring
        `project=other`, textually AFTER the real project= -- the OLD
        generic last-match-wins scan would have let it win. The line's
        REAL project= (parsed only from the prefix before the first
        ` file=`) must win instead."""
        lines = [
            f"{ts(1)} outcome=matched elapsed=0s project=demo file=/tmp/a project=other/x.py",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home), project="demo")
        self.assertEqual(out["pre_edit"]["matched"], 1, "the real project=demo must win")
        rc, other = run_stats_json(home=str(self.home), project="other")
        self.assertEqual(other["pre_edit"]["matched"], 0, "the embedded file-path text must not become a project")

    def test_file_field_containing_project_equals_does_not_hijack_attribution_ledger_shape(self):
        """The mirror-image shape: ledger-post-edit.sh's line (via
        mc_log) places `project=` AFTER `file=` -- mc_log's own
        unconditional suffix. Cutting the scan at the first ` file=` (the
        pre-edit-chain rule) would silently swallow that real trailing
        project= as part of the file value instead -- this is the
        regression the kind-aware fix exists to prevent (caught by
        test_healthy_case_no_flags while fixing the OTHER shape). A file
        value containing an embedded, EARLIER fake `project=other` token
        must still lose to mc_log's real trailing one."""
        lines = [
            f"{ts(1)} ledger outcome=appended kind=code elapsed=0s session=s1 "
            f"file=/tmp/a project=other/x.py project=demo",
        ]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home), project="demo")
        self.assertEqual(out["ledger_appends"]["code"], 1, "the real trailing project=demo must win")
        rc, other = run_stats_json(home=str(self.home), project="other")
        self.assertEqual(other["ledger_appends"]["code"], 0, "the embedded file-path text must not become a project")

    def test_store_missing_repo_is_unmeasured_but_ledger_flag_still_evaluated(self):
        """The write-side FLAG no longer needs --store at all (round 2,
        ruling 1) -- it is driven entirely by this project's own
        store-kind ledger appends. A missing/bad --store path leaves
        store_commits unmeasured (None) but must NOT suppress the FLAG:
        3 nudges and 0 ledger store appends here, with no ledger lines at
        all, must still fire."""
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(3)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home), store=str(Path(self.td) / "no-such-store"))
        self.assertEqual(rc, 0)
        self.assertIsNone(out["store_commits"])
        self.assertIn(
            "FLAG: write side silent — 3 nudges, 0 store-kind ledger appends in 7d (INC-0105 class)",
            out["flags"],
        )


class TestStatsFlags(StatsTestBase):
    def test_write_side_silent_flag_driven_by_ledger_not_store_commits(self):
        """Round 2, ruling 1 (Grok gate): the write-side FLAG is keyed on
        `ledger_appends.store`, never on git `store_commits` -- even with
        --store passed and a real (irrelevant, outside-window) store
        history, an empty ledger for THIS project still fires."""
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(4)]
        self.write_log(lines)
        store = self.git_store(commit_dates=[NOW - timedelta(days=400)])  # outside window
        rc, out = run_stats_json(home=str(self.home), store=str(store))
        self.assertEqual(out["store_commits"], 0)
        self.assertEqual(out["ledger_appends"]["store"], 0)
        self.assertEqual(out["nudges"]["total"], 4)
        self.assertIn(
            "FLAG: write side silent — 4 nudges, 0 store-kind ledger appends in 7d (INC-0105 class)",
            out["flags"],
        )

    def test_inc_0105_exact_shape_unrelated_store_commit_never_vetoes(self):
        """The literal INC-0105 shape (12 nudges, 0 store-kind ledger
        appends that day) PLUS an unrelated store commit elsewhere in the
        window (the real incident: a store relocation commit the day
        before) -- ruling 1's whole point. The old git-gated formula would
        have read `store_commits >= 1` and suppressed the FLAG; the
        ledger-gated formula must fire regardless, with the commit still
        reported as (irrelevant) corroboration."""
        lines = [f"{ts(2)} userprompt outcome=injected session=s1 project=demo" for _ in range(12)]
        self.write_log(lines)
        store = self.git_store(commit_dates=[NOW - timedelta(days=1)])  # unrelated, inside window
        rc, out = run_stats_json(home=str(self.home), store=str(store))
        self.assertEqual(out["store_commits"], 1, "the unrelated commit is still reported, as corroboration")
        self.assertEqual(out["ledger_appends"]["store"], 0)
        self.assertEqual(out["nudges"]["total"], 12)
        self.assertIn(
            "FLAG: write side silent — 12 nudges, 0 store-kind ledger appends in 7d (INC-0105 class)",
            out["flags"],
        )

    def test_future_dated_store_commit_excluded_by_until_bound(self):
        """Round 2 Codex gate item 11: `--until=<now>` bounds the git
        measurement on BOTH ends -- a future-dated commit (clock skew, a
        rebase, deliberate backdating) must not count as "in the window"
        with no upper bound."""
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(3)]
        self.write_log(lines)
        store = self.git_store(commit_dates=[NOW + timedelta(days=2)])  # future
        rc, out = run_stats_json(home=str(self.home), store=str(store))
        self.assertEqual(out["store_commits"], 0, "a future-dated commit must not be counted")

    def test_write_side_flag_boundary_three_fires(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(3)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertTrue(any("INC-0105" in f for f in out["flags"]))

    def test_write_side_flag_boundary_two_does_not_fire(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(2)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertFalse(any("INC-0105" in f for f in out["flags"]))

    def test_write_side_flag_silent_when_ledger_store_appends_present(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(5)]
        lines.append(f"{ts(1)} ledger outcome=appended kind=store session=s1 file=/y.md project=demo")
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertFalse(any("INC-0105" in f for f in out["flags"]))

    def test_read_side_silent_flag(self):
        lines = [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["pre_edit"]["lookups"], 0)
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

    def test_read_side_flag_fires_when_only_failed_lookups_exist(self):
        """Round 2 Codex gate item 7 (BLOCKING finding): ten prompts plus
        five FAILED pre-edit attempts (index-missing -- the db was never
        built) and zero real (matched/no-match) lookups must still FLAG.
        The old formula used `pre_edit_total` (which includes `other`) and
        would have read this as "read side has activity" -- the exact
        false negative that would have hidden a dead read side behind a
        pile of failed attempts."""
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(10)]
        lines += [f"{ts(1)} outcome=index-missing elapsed=0s project=demo db=/x.sqlite" for _ in range(5)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["pre_edit"]["total"], 5)
        self.assertEqual(out["pre_edit"]["lookups"], 0)
        self.assertIn("FLAG: read side silent (INC-0103 class)", out["flags"])

    def test_read_side_flag_fires_with_query_failed_outcome(self):
        """Round-3 addendum: `query-failed` (pre-edit-chain.sh's new
        outcome for "every for-path candidate itself errored, nothing was
        genuinely queried") is exactly the same class as index-missing --
        FAILED, not a real lookup. Ten prompts plus five query-failed and
        zero real lookups must still FLAG."""
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(10)]
        lines += [f"{ts(1)} outcome=query-failed elapsed=0s project=demo file=/x.py" for _ in range(5)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["pre_edit"]["total"], 5)
        self.assertEqual(out["pre_edit"]["lookups"], 0)
        self.assertIn("query-failed", out["pre_edit"]["outcomes"])
        self.assertIn("FLAG: read side silent (INC-0103 class)", out["flags"])

    def test_read_side_flag_absent_with_ten_empty_payload_lines(self):
        """Round-3 addendum (ruling C): `empty-payload` means the hook
        received no payload at all -- nothing was processed, so it must
        not satisfy the ">=10 prompts" busy signal any more than
        duplicate-delivery/agent-source do."""
        lines = [f"{ts(1)} userprompt outcome=empty-payload session= project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["user_prompts"], 0)
        self.assertEqual(out["non_user_prompt_lines"], 10)
        self.assertEqual(out["flags"], [])

    def test_read_side_flag_absent_with_no_session_id_and_no_state_lines(self):
        """Round-3 addendum: `no-session-id` (payload present, no
        session_id -- can't even say WHICH session) and `no-state` (a
        session_id with no prior SessionStart -- no turn tracking, no
        evidence considered) are the same "nothing was actually
        processed" class as empty-payload."""
        lines = [f"{ts(1)} userprompt outcome=no-session-id project=demo" for _ in range(5)]
        lines += [f"{ts(1)} userprompt outcome=no-state session=s1 project=demo" for _ in range(5)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["user_prompts"], 0)
        self.assertEqual(out["non_user_prompt_lines"], 10)
        self.assertEqual(out["flags"], [])

    def test_read_side_flag_still_fires_with_mktemp_and_decision_failed_lines(self):
        """Round-3 addendum design decision (documented, not literally
        asked for by the ruling, but the natural boundary line): unlike
        the malformed-delivery outcomes above, `mktemp-failed` and
        `decision-failed` are only reachable AFTER the hook has already
        confirmed a real session_id, existing state, and a non-agent
        turn -- a downstream infra failure on top of a CONFIRMED real
        prompt, not evidence the prompt wasn't real. These must still
        count toward user_prompts (excluding them would under-count
        genuine engagement and could itself hide a real INC-0103-class
        silence)."""
        lines = [f"{ts(1)} userprompt outcome=mktemp-failed session=s1 project=demo" for _ in range(5)]
        lines += [f"{ts(1)} userprompt outcome=decision-failed session=s1 project=demo" for _ in range(5)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["user_prompts"], 10)
        self.assertEqual(out["non_user_prompt_lines"], 0)
        self.assertIn("FLAG: read side silent (INC-0103 class)", out["flags"])

    def test_read_side_flag_absent_with_ten_duplicate_delivery_lines(self):
        """Round 2 Codex gate item 8: ten `duplicate-delivery` lines are
        ten hook INVOCATIONS but zero live user turns -- they must not
        satisfy the ">=10 prompts" busy signal on their own. (No pre-edit
        lookups exist either, so under the OLD user_prompts semantics
        this would have wrongly FLAGged a project that never had ten real
        prompts at all.)"""
        lines = [f"{ts(1)} userprompt outcome=duplicate-delivery session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["user_prompts"], 0)
        self.assertEqual(out["non_user_prompt_lines"], 10)
        self.assertEqual(out["flags"], [])

    def test_read_side_flag_absent_with_ten_agent_source_lines(self):
        lines = [f"{ts(1)} userprompt outcome=agent-source session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["user_prompts"], 0)
        self.assertEqual(out["flags"], [])

    def test_non_user_prompt_lines_excluded_but_real_prompts_still_flag(self):
        """A mix: 10 real (injected) prompts plus 5 duplicate-delivery --
        user_prompts must count only the 10 real ones (duplicates are
        NOT added on top), and with zero pre-edit lookups the read-side
        FLAG must still fire on the real count alone."""
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(10)]
        lines += [f"{ts(1)} userprompt outcome=duplicate-delivery session=s1 project=demo" for _ in range(5)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home))
        self.assertEqual(out["user_prompts"], 10)
        self.assertEqual(out["non_user_prompt_lines"], 5)
        self.assertIn("FLAG: read side silent (INC-0103 class)", out["flags"])

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

    def test_unknown_project_never_flags_even_when_thresholds_met(self):
        """Round 2, ruling 2 (Grok gate BLOCKING finding): >=10 userprompt
        lines with no project= (a real, common shape -- every pre-fix
        write-side hook invocation before this branch) plus >=3 nudges and
        zero everything-else must NOT raise either FLAG on the
        "(unknown)" bucket -- that bucket's attribution is incomplete by
        construction (every un-projected logger, forever), so both
        conditions would ALWAYS read as silent there, on every
        deployment's cold-start window. That is not a real signal."""
        lines = [f"{ts(1)} userprompt outcome=injected session=s1" for _ in range(12)]
        self.write_log(lines)
        rc, out = run_stats_json(home=str(self.home), project="(unknown)")
        self.assertEqual(out["user_prompts"], 12)
        self.assertEqual(out["pre_edit"]["lookups"], 0)
        self.assertEqual(out["nudges"]["total"], 12)
        self.assertEqual(out["ledger_appends"]["store"], 0)
        self.assertEqual(out["flags"], [], "neither FLAG may ever fire for the (unknown) bucket")

    def test_live_mix_named_project_honest_vs_unknown_bucket(self):
        """Round 2, ruling 2's required test: the exact live-log shape
        (pre-edit-chain.sh already stamped project= before this branch;
        userprompt-remind.sh/ledger-post-edit.sh did not yet) -- pre-edit
        lookups WITH project=X, userprompt lines WITHOUT any project= at
        all. X must show its true, honest numbers (0 prompts -- none of
        the userprompt traffic is attributable to it, this is not
        silently invented) and must NOT flag (0 prompts < 10, trivially).
        "(unknown)" must show the real prompt volume AND must not
        INC-0103-FLAG despite 0 pre-edit lookups landing there."""
        lines = [f"{ts(1)} outcome=matched elapsed=0s project=X file=/a.py"]
        lines += [f"{ts(1)} userprompt outcome=no-evidence session=s{i}" for i in range(15)]
        self.write_log(lines)

        rc, x = run_stats_json(home=str(self.home), project="X")
        self.assertEqual(x["user_prompts"], 0, "X's own prompt count is honestly zero, not borrowed from (unknown)")
        self.assertEqual(x["pre_edit"]["lookups"], 1)
        self.assertEqual(x["flags"], [])

        rc, unknown = run_stats_json(home=str(self.home), project="(unknown)")
        self.assertEqual(unknown["user_prompts"], 15)
        self.assertEqual(unknown["pre_edit"]["lookups"], 0)
        self.assertEqual(unknown["flags"], [], "the (unknown) bucket must never INC-0103-FLAG")


class TestStatsTextOutput(StatsTestBase):
    def test_plain_text_contains_key_lines(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(3)]
        lines.append(f"{ts(1)} userprompt outcome=injected session=s2")  # legacy, no project=
        self.write_log(lines)
        rc, out = run_stats(home=str(self.home))
        self.assertEqual(rc, 0)
        self.assertIn("MemContinuum liveness stats", out)
        self.assertIn("project=demo", out)
        self.assertIn(
            "nudges → store-kind ledger appends (this project, drives the FLAG below): 3 → 0", out
        )
        self.assertIn('note: 1 legacy lines without project= are bucketed under "(unknown)"', out)

    def test_no_note_line_when_no_unknown_lines(self):
        lines = [f"{ts(1)} userprompt outcome=injected session=s1 project=demo" for _ in range(3)]
        self.write_log(lines)
        rc, out = run_stats(home=str(self.home))
        self.assertNotIn("note:", out)

    def test_exit_code_always_zero_even_on_flags(self):
        lines = [f"{ts(1)} userprompt outcome=no-evidence session=s1 project=demo" for _ in range(10)]
        self.write_log(lines)
        rc, out = run_stats(home=str(self.home))
        self.assertEqual(rc, 0)
        self.assertIn("FLAG: read side silent", out)


if __name__ == "__main__":
    unittest.main()
