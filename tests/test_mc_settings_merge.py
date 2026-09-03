"""Direct (no-subprocess) unit tests for scripts/mc_settings_merge.py -- the
ONE settings.json/settings.local.json hook-entry merge implementation shared
by scripts/repo-init.sh and memcontinuum-setup.sh (fix-round-4 F8). Pure
python, no venv/subprocess needed -- these exercise the library function
directly, the same way tests/test_memidx.py exercises memidx.py.

scripts/repo-init.sh and memcontinuum-setup.sh each have their own
subprocess-level tests (tests/test_repo_init.py, tests/test_setup.py) proving
their CALLERS behave correctly end to end; this file proves the shared
implementation itself: atomicity, mode preservation, backup, malformed-input
refusal, and the two identity rules (project-aware basenames, bare needle).
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR / "scripts"))

from mc_settings_merge import (  # noqa: E402
    MergeRefused,
    basenames_identity,
    merge_settings,
    needle_identity,
)


class MergeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mc-settings-merge-test-")
        self.path = str(Path(self.tmp) / "settings.local.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, data):
        Path(self.path).write_text(json.dumps(data), encoding="utf-8")

    def read(self):
        return json.loads(Path(self.path).read_text(encoding="utf-8"))


class TestAtomicWriteAndBackup(MergeCase):
    def test_no_tmp_left_behind_and_backup_written(self):
        self.write({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "echo foreign"}]}
        ]}})
        ok = merge_settings(
            self.path, ["SessionStart"], needle_identity("nonexistent-needle"),
            add={"SessionStart": [{"hooks": [{"type": "command", "command": "echo new"}]}]},
        )
        self.assertTrue(ok)
        self.assertFalse(Path(self.path + ".tmp-memcontinuum").exists())
        self.assertTrue(Path(self.path + ".bak-memcontinuum").is_file())

    def test_mode_preserved_no_truncation_window(self):
        """F8: repo-init.sh's old merge used a bare open(path, "w") truncate
        -- an interrupt mid-dump left an invalid file. The shared helper
        writes a same-directory tmp file and os.replace()s it in, so the
        settings file itself is never observed half-written; this also
        proves the ORIGINAL file mode survives the rewrite (a 0600 settings
        file must not come back 0644 under a default umask)."""
        self.write({"hooks": {}})
        os.chmod(self.path, 0o600)
        merge_settings(
            self.path, ["SessionStart"], needle_identity("x"),
            add={"SessionStart": [{"hooks": [{"type": "command", "command": "echo new"}]}]},
        )
        mode = os.stat(self.path).st_mode & 0o7777
        self.assertEqual(mode, 0o600)
        # the backup preserves the ORIGINAL (pre-write) mode too (copy2).
        backup_mode = os.stat(self.path + ".bak-memcontinuum").st_mode & 0o7777
        self.assertEqual(backup_mode, 0o600)

    def test_no_backup_when_file_did_not_exist(self):
        merge_settings(
            self.path, ["SessionStart"], needle_identity("x"),
            add={"SessionStart": [{"hooks": [{"type": "command", "command": "echo new"}]}]},
        )
        self.assertFalse(Path(self.path + ".bak-memcontinuum").exists())
        self.assertTrue(Path(self.path).is_file())


class TestIdempotentRerunAndForeignSurvival(MergeCase):
    def test_rerunning_never_duplicates_and_foreign_entries_survive(self):
        self.write({
            "permissions": {"allow": ["Bash(ls:*)"]},
            "hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": "echo foreign"}]}
            ]},
        })
        is_ours = needle_identity("our-detector.sh")
        add = {"SessionStart": [{"hooks": [{"type": "command", "command": "bash our-detector.sh"}]}]}
        for _ in range(3):
            ok = merge_settings(self.path, ["SessionStart"], is_ours, add=add)
            self.assertTrue(ok)
        data = self.read()
        cmds = [
            i["command"]
            for g in data["hooks"]["SessionStart"]
            for i in g.get("hooks", [])
        ]
        self.assertEqual(cmds.count("bash our-detector.sh"), 1, cmds)
        self.assertIn("echo foreign", cmds)
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls:*)"]})

    def test_group_dropped_when_entirely_ours_foreign_group_survives(self):
        self.write({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "bash our-detector.sh"}]},
            {"hooks": [{"type": "command", "command": "echo foreign"}]},
        ]}})
        merge_settings(self.path, ["SessionStart"], needle_identity("our-detector.sh"), add={})
        data = self.read()
        groups = data["hooks"]["SessionStart"]
        self.assertEqual(len(groups), 1, groups)
        self.assertEqual(groups[0]["hooks"][0]["command"], "echo foreign")

    def test_event_dropped_entirely_when_it_becomes_empty(self):
        self.write({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "bash our-detector.sh"}]},
        ]}})
        merge_settings(self.path, ["SessionStart"], needle_identity("our-detector.sh"), add={})
        data = self.read()
        self.assertNotIn("SessionStart", data.get("hooks", {}))


class TestMalformedInputRefused(MergeCase):
    def test_invalid_json_refused_file_untouched(self):
        Path(self.path).write_text("{not json", encoding="utf-8")
        with self.assertRaises(MergeRefused):
            merge_settings(self.path, ["SessionStart"], needle_identity("x"), add={})
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), "{not json")

    def test_non_object_top_level_refused(self):
        self.write(["not", "an", "object"])
        with self.assertRaises(MergeRefused):
            merge_settings(self.path, ["SessionStart"], needle_identity("x"), add={})

    def test_non_object_hooks_refused(self):
        self.write({"hooks": "oops"})
        with self.assertRaises(MergeRefused):
            merge_settings(self.path, ["SessionStart"], needle_identity("x"), add={})

    def test_non_list_event_refused(self):
        self.write({"hooks": {"SessionStart": "oops"}})
        with self.assertRaises(MergeRefused):
            merge_settings(self.path, ["SessionStart"], needle_identity("x"), add={})

    def test_bad_add_shape_raises_value_error_not_merge_refused(self):
        self.write({"hooks": {}})
        with self.assertRaises(ValueError):
            merge_settings(self.path, ["SessionStart"], needle_identity("x"), add={"SessionStart": "oops"})


class TestDryRunWritesNothing(MergeCase):
    def test_dry_run(self):
        self.write({"hooks": {}})
        before = Path(self.path).read_text(encoding="utf-8")
        merge_settings(
            self.path, ["SessionStart"], needle_identity("x"),
            add={"SessionStart": [{"hooks": [{"type": "command", "command": "echo new"}]}]},
            dry_run=True,
        )
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"), before)
        self.assertFalse(Path(self.path + ".bak-memcontinuum").exists())


class TestBasenamesIdentityProjectScoping(unittest.TestCase):
    """Mirrors scripts/repo-init.sh's is_ours() rule exactly (F1): a
    markerless command naming one of our scripts is ours (legacy, pre-
    identity wiring); a marked one must match --project."""

    def setUp(self):
        self.is_ours = basenames_identity(["newfile-nudge.sh"], project="alpha")

    def test_foreign_script_never_matches(self):
        self.assertFalse(self.is_ours("bash unrelated.sh"))

    def test_markerless_legacy_entry_matches(self):
        self.assertTrue(self.is_ours("MEMCONTINUUM_CODE_ROOT=/x bash hooks/newfile-nudge.sh"))

    def test_marked_own_project_matches(self):
        self.assertTrue(self.is_ours(
            "MEMCONTINUUM_CODE_ROOT=/x MEMCONTINUUM_PROJECT=alpha MEMCONTINUUM_PYTHON=/py bash hooks/newfile-nudge.sh"
        ))

    def test_marked_other_project_does_not_match(self):
        self.assertFalse(self.is_ours(
            "MEMCONTINUUM_CODE_ROOT=/x MEMCONTINUUM_PROJECT=beta MEMCONTINUUM_PYTHON=/py bash hooks/newfile-nudge.sh"
        ))

    def test_marked_project_at_end_of_command_matches(self):
        self.assertTrue(self.is_ours("bash hooks/newfile-nudge.sh MEMCONTINUUM_PROJECT=alpha"))

    def test_no_project_scoping_when_project_is_none(self):
        is_ours = basenames_identity(["newfile-nudge.sh"], project=None)
        self.assertTrue(is_ours(
            "MEMCONTINUUM_PROJECT=beta bash hooks/newfile-nudge.sh"
        ))

    def test_r7_code_root_path_containing_needle_does_not_collide(self):
        """R7 regression, round 4 gate (Codex, probed): the project match
        used to be a bare substring check -- a --code-root path containing
        the literal text 'MEMCONTINUUM_PROJECT=alpha' anywhere (not at an
        env-assignment position at all) satisfied it, so alpha's sweep
        wrongly claimed an entry that actually belongs to beta and deleted
        it. Beta's entry must survive alpha's sweep."""
        beta_cmd = (
            "MEMCONTINUUM_CODE_ROOT=/home/x/MEMCONTINUUM_PROJECT=alpha "
            "MEMCONTINUUM_PROJECT=beta MEMCONTINUUM_PYTHON=/py bash hooks/newfile-nudge.sh"
        )
        self.assertFalse(self.is_ours(beta_cmd), "alpha's sweep must not claim beta's entry")
        is_ours_beta = basenames_identity(["newfile-nudge.sh"], project="beta")
        self.assertTrue(is_ours_beta(beta_cmd), "beta's own sweep must still claim its own entry")

    def test_regate2_markerless_entry_with_needle_in_path_is_still_ours(self):
        """Regate round 2 (Codex): the 'carries ANY marker?' pre-check was
        still a bare substring -- a legacy MARKERLESS entry whose
        --code-root path merely contains 'MEMCONTINUUM_PROJECT=...' (at a
        non-assignment position) read as marked-for-someone-else, survived
        the sweep, and got DUPLICATED when the refreshed group was
        appended. It has no real marker, so it is ours (legacy) and must
        be swept."""
        legacy_cmd = (
            "MEMCONTINUUM_CODE_ROOT=/home/x/MEMCONTINUUM_PROJECT=alpha "
            "bash hooks/newfile-nudge.sh"
        )
        self.assertTrue(
            self.is_ours(legacy_cmd),
            "a markerless legacy entry must be swept even when a path argument "
            "contains the marker text at a non-assignment position",
        )


class TestNeedleIdentity(unittest.TestCase):
    def test_bare_substring_match(self):
        is_ours = needle_identity("memcontinuum-detect.sh")
        self.assertTrue(is_ours("bash '/some/path/memcontinuum-detect.sh'"))
        self.assertFalse(is_ours("bash '/some/path/other.sh'"))


class TestF6SettingsTimeoutSurvivesReRender(unittest.TestCase):
    """F6 (external-review fix round): the new `"timeout": 5` key rendered
    into pre-edit-chain.sh's PreToolUse entries (templates/
    code-root-filter-pair.json.tmpl) must survive a re-render through the
    shared merge implementation -- a stale, timeout-less entry from an
    older install gets swept and replaced, not merged/kept alongside the
    new one."""

    def test_a_rerun_replaces_old_entries_with_the_timeout_carrying_ones(self):
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / "settings.json")
            old_group = {"matcher": "Edit|Write", "hooks": [{
                "type": "command", "if": "Edit(/repo/**)",
                "command": "MEMCONTINUUM_PROJECT=p bash hooks/pre-edit-chain.sh",
            }]}
            Path(path).write_text(json.dumps({"hooks": {"PreToolUse": [old_group]}}))
            new_group = {"matcher": "Edit|Write", "hooks": [{
                "type": "command", "if": "Edit(/repo/**)",
                "command": "MEMCONTINUUM_PROJECT=p bash hooks/pre-edit-chain.sh", "timeout": 5,
            }]}
            merge_settings(
                path, ["PreToolUse"], needle_identity("MEMCONTINUUM_PROJECT="),
                add={"PreToolUse": [new_group]},
            )
            data = json.loads(Path(path).read_text())
            entries = data["hooks"]["PreToolUse"][0]["hooks"]
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
