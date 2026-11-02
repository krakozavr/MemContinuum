"""`memidx.py context-pack` (TOP-0135 L1/L2): decision records for an agent
brief -- standing digest, bound topics, cap by whole topics, pack hash, blind
mode, read-only. Fixtures follow tests/test_memidx.py: a tmp store, reindexed
into a tmp sqlite db, driven through memidx.main."""
import contextlib
import hashlib
import io
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402


def topic_text(tid, title, code_refs, text, authority="owner-ratified", standing=False):
    refs = "[" + ", ".join(code_refs) + "]"
    return (
        "---\ntype: topic\n"
        f"id: {tid}\ntitle: {title}\narea: testing\ncurrent: L1\n"
        f"standing: {'[L1]' if standing else '[]'}\n"
        f"code_refs: {refs}\n"
        "links:\n  - link: L1\n    date: 2026-01-01\n    status: active\n    kind: adopted\n"
        f"    ruling: {{text: {json.dumps(text, ensure_ascii=False)}, authority: {authority}, source: s}}\n"
        "    recorded_by: agent\n    recorded_at: 2026-01-01\n"
        "---\n\nBody.\n"
    )


class PackCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.td = Path(self._td.name)
        self.root = self.td / "store"
        (self.root / "topics").mkdir(parents=True)
        self.db = self.td / "idx.sqlite"

    def add(self, name, text):
        (self.root / "topics" / f"{name}.md").write_text(text, encoding="utf-8")

    def reindex(self):
        with contextlib.redirect_stdout(io.StringIO()):
            rc = memidx.main(["reindex", "--root", str(self.root), "--db", str(self.db), "--no-embed"])
        self.assertEqual(rc, 0)

    def run_cmd(self, *argv, with_root=True):
        base = ["--db", str(self.db)] + (["--root", str(self.root)] if with_root else [])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = memidx.main([argv[0], *base, *argv[1:]])
        return rc, out.getvalue(), err.getvalue()

    def pack(self, *args, **kw):
        rc, out, err = self.run_cmd("context-pack", *args, **kw)
        self.assertEqual(rc, 0, err)
        return out

    def pack_json(self, *args, **kw):
        return json.loads(self.pack(*args, "--json", **kw))

    @staticmethod
    def text_part(out):
        """The pack text above the final `pack sha256=` line."""
        head, _, last = out.rstrip("\n").rpartition("\n")
        assert last.startswith("pack sha256="), last
        return head + "\n"


class TestContextPack(PackCase):
    def test_two_files_bound_to_one_topic_give_one_entry_listing_both(self):
        self.add("a", topic_text("TOP-9101", "Alpha", ["src/a.py", "src/b.py"], "alpha ruling"))
        self.reindex()
        out = self.pack("src/a.py", "src/b.py")
        self.assertEqual(out.count("TOP-9101 Alpha"), 1)
        self.assertIn("bound to: src/a.py, src/b.py", out)
        j = self.pack_json("src/a.py", "src/b.py")
        self.assertEqual([t for t in j["topics"] if t["id"] == "TOP-9101"][0]["via"],
                         ["src/a.py", "src/b.py"])

    def test_owner_topic_survives_cap_ahead_of_inference_and_omissions_are_named(self):
        pad = " filler" * 150
        self.add("own", topic_text("TOP-9200", "Owner", ["src/x.py"], "OWNERRULE" + pad,
                                   authority="owner-verbatim"))
        for i in range(1, 7):
            self.add(f"inf{i}", topic_text(f"TOP-920{i}", f"Inf{i}", ["src/x.py"],
                                           f"INFRULE{i}" + pad, authority="agent-inference"))
        self.reindex()
        full = self.pack_json("src/x.py")
        self.assertEqual(full["omitted"], [])
        cap = full["bytes"] - 2 * 1100
        j = self.pack_json("src/x.py", "--max-bytes", str(cap))
        self.assertLessEqual(j["bytes"], cap)
        self.assertIn("OWNERRULE", j["text"])
        self.assertTrue(j["omitted"])
        self.assertNotIn("TOP-9200", j["omitted"])
        m = re.search(r"\[(\d+) topic\(s\) omitted by the size cap: ([^\]]+)\]", j["text"])
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).split(", "), j["omitted"])
        for i in range(1, 7):
            tid = f"TOP-920{i}"
            if tid in j["omitted"]:
                self.assertNotIn(f"INFRULE{i}", j["text"])
            else:  # never cut mid-record
                self.assertIn(f"INFRULE{i}" + pad, j["text"])

    def test_standing_block_is_byte_identical_to_standing(self):
        self.add("s", topic_text("TOP-9301", "Stand", ["src/s.py"], "standing ruling", standing=True))
        self.add("t", topic_text("TOP-9302", "Other", ["src/t.py"], "other ruling"))
        self.reindex()
        rc, standing_out, _ = self.run_cmd("standing")
        self.assertEqual(rc, 0)
        self.assertIn(standing_out.rstrip("\n"), self.pack("src/t.py"))

    def test_query_block_is_labelled_fts_only_and_dropped_first(self):
        self.add("a", topic_text("TOP-9401", "Bound", ["src/a.py"], "bound ruling"))
        self.add("q", topic_text("TOP-9402", "Zebra", ["src/z.py"], "zebra crossing rule"))
        self.reindex()
        without = self.pack_json("src/a.py", "--query", "nosuchwordxyz")   # heading, no hits
        withq = self.pack_json("src/a.py", "--query", "zebra")
        self.assertIn("Nearest by search -- may be unrelated", withq["text"])
        self.assertIn("TOP-9402 Zebra", withq["text"])
        self.assertEqual([t["via"] for t in withq["topics"] if t["id"] == "TOP-9402"], [["search"]])
        capped = self.pack_json("src/a.py", "--query", "zebra", "--max-bytes", str(without["bytes"] + 60))
        self.assertEqual(capped["omitted"], ["TOP-9402"])
        self.assertIn("TOP-9401 Bound", capped["text"])

    def test_read_only_index_and_store_untouched(self):
        self.add("a", topic_text("TOP-9501", "Alpha", ["src/a.py"], "ruling", standing=True))
        self.reindex()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"],
                       cwd=self.root, check=True)
        sha = hashlib.sha256(self.db.read_bytes()).hexdigest()
        mtime = self.db.stat().st_mtime_ns
        self.pack("src/a.py", "--topic", "TOP-9501", "--query", "ruling")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), sha)
        self.assertEqual(self.db.stat().st_mtime_ns, mtime)
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.root,
                            capture_output=True, text=True, check=True)
        self.assertEqual(st.stdout, "")

    def test_stale_index_still_emits_with_state_line(self):
        self.add("a", topic_text("TOP-9601", "Alpha", ["src/a.py"], "ruling"))
        self.reindex()
        self.add("b", topic_text("TOP-9602", "New", ["src/n.py"], "new"))
        out = self.pack("src/a.py")
        self.assertIn("Index state: stale", out)
        self.assertIn("TOP-9601 Alpha", out)
        self.assertEqual(self.pack_json("src/a.py")["state"], "stale")

    def test_missing_and_uninitialized_index_exit_1(self):
        rc, out, err = self.run_cmd("context-pack", "src/a.py")
        self.assertEqual(rc, 1)
        self.assertIn("missing", err)
        conn = memidx.open_db(self.db, project=memidx.DEFAULT_PROJECT)
        conn.close()
        rc, out, err = self.run_cmd("context-pack", "src/a.py")
        self.assertEqual(rc, 1)
        self.assertIn("uninitialized", err)

    def test_file_with_no_bound_topic_gives_standing_only_and_says_so(self):
        self.add("s", topic_text("TOP-9701", "Stand", ["src/s.py"], "standing ruling", standing=True))
        self.reindex()
        out = self.pack("src/unbound.py")
        self.assertIn("TOP-9701 L1 (owner-ratified): standing ruling", out)
        self.assertIn("none of these files has a bound topic", out)
        self.assertNotIn("record file:", out)

    def test_ruling_text_round_trips_verbatim(self):
        ruling = 'Never "delete" without a check: re-verify -- always; naïve Привет 日本'
        self.add("a", topic_text("TOP-9801", "Verbatim", ["src/a.py"], ruling))
        self.reindex()
        out = self.pack("src/a.py")
        self.assertIn(f'"{ruling}"', out)

    def test_header_is_authority_aware_and_carries_no_record_ids(self):
        self.add("a", topic_text("TOP-9901", "Alpha", ["src/a.py"], "r"))
        self.reindex()
        head = self.pack("src/a.py").split("Index state:")[0]
        for needle in ("owner-verbatim and owner-ratified rulings bind",
                       "reviewer-finding and code-derived records are reported facts",
                       "agent-inference records are labelled inference"):
            self.assertIn(needle, head)
        self.assertNotRegex(head, r"TOP-\d")
        self.assertNotIn("Treat them as settled", head)

    def test_named_topic_and_missing_named_topic(self):
        self.add("a", topic_text("TOP-9911", "Alpha", ["src/a.py"], "alpha"))
        self.reindex()
        out = self.pack("--topic", "TOP-9911", "--topic", "TOP-0000")
        self.assertIn("Named explicitly:", out)
        self.assertIn("TOP-9911 Alpha", out)
        self.assertIn("TOP-0000: not found in the index", out)


class TestContextPackHash(PackCase):
    def setUp(self):
        super().setUp()
        self.add("a", topic_text("TOP-9951", "Alpha", ["src/a.py"], "alpha ruling", standing=True))
        self.reindex()

    def test_two_runs_identical_and_hash_matches_printed_bytes(self):
        a = self.pack("src/a.py")
        b = self.pack("src/a.py")
        self.assertEqual(a, b)
        text = self.text_part(a)
        last = a.rstrip("\n").rsplit("\n", 1)[1]
        self.assertEqual(
            last, f"pack sha256={hashlib.sha256(text.encode()).hexdigest()} bytes={len(text.encode())}")
        j1, j2 = self.pack_json("src/a.py"), self.pack_json("src/a.py")
        self.assertEqual(j1, j2)
        self.assertEqual(j1["text"], text)
        self.assertEqual(j1["sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(j1["bytes"], len(text.encode()))
        self.assertEqual(set(j1), {"sha256", "bytes", "blind", "text", "topics", "omitted", "state"})
        self.assertFalse(j1["blind"])

    def test_blind_opens_no_index_and_carries_nothing_from_the_store(self):
        self.db.unlink()   # no index at all: blind must still exit 0
        out = self.pack("src/a.py", "--topic", "TOP-9951", "--blind")
        lines = out.rstrip("\n").split("\n")
        self.assertEqual(lines[0], "Blind review: no decision records were supplied "
                                   "(recorded blind-review mode).")
        self.assertEqual(len(lines), 2)
        text = self.text_part(out)
        self.assertEqual(lines[1], f"pack sha256={hashlib.sha256(text.encode()).hexdigest()} "
                                   f"bytes={len(text.encode())}")
        self.assertNotIn("TOP-", out)
        j = self.pack_json("src/a.py", "--blind")
        self.assertTrue(j["blind"])
        self.assertEqual((j["topics"], j["omitted"]), ([], []))
        self.assertEqual(j["text"], text)


if __name__ == "__main__":
    unittest.main()
