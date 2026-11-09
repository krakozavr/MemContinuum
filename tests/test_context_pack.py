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
import os
import unittest
from pathlib import Path
from unittest import mock

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
        self.assertEqual(set(j1), {"sha256", "bytes", "blind", "text", "topics", "omitted", "standing_omitted", "state"})
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


def link_block(n, date, text, authority="owner-ratified", status="superseded"):
    return (f"  - link: L{n}\n    date: {date}\n    status: {status}\n    kind: adopted\n"
            f"    ruling: {{text: {json.dumps(text, ensure_ascii=False)}, authority: {authority}, source: s}}\n"
            "    recorded_by: agent\n    recorded_at: 2026-01-01\n")


def multi_link_topic(tid, title, code_refs, current_text, old_texts, authority="owner-ratified"):
    """Old links L1..Ln superseded, then an active current link."""
    links = "".join(link_block(i + 1, f"2025-01-{i + 1:02d}", t, authority) for i, t in enumerate(old_texts))
    links += link_block(len(old_texts) + 1, "2026-01-01", current_text, authority, status="active")
    return (f"---\ntype: topic\nid: {tid}\ntitle: {title}\narea: testing\n"
            f"current: L{len(old_texts) + 1}\ncode_refs: [{', '.join(code_refs)}]\nlinks:\n{links}---\n\nBody.\n")


class TestContextPackFixRound1(PackCase):
    def test_current_ruling_over_the_per_topic_budget_round_trips_exactly(self):
        long_text = "BEGIN " + "clause; " * 400 + "END-OF-RULING"
        self.assertGreater(len(long_text), 2500)
        self.add("a", multi_link_topic("TOP-8001", "Long", ["src/a.py"], long_text, ["old one " * 30]))
        self.reindex()
        out = self.pack("src/a.py")
        self.assertIn(f'"{long_text}"', out)
        self.assertIn("[... 1 older link(s) omitted]", out)
        self.assertEqual(self.pack_json("src/a.py")["omitted"], [])

    def test_very_long_current_chain_with_one_older_link_still_names_the_dropped_history(self):
        long_text = "BEGIN " + "clause; " * 700 + "END-OF-RULING"
        self.assertGreater(len(long_text), 5500)
        self.add("a", multi_link_topic("TOP-8010", "Huge", ["src/a.py"], long_text, ["OLDER-HISTORY " * 20]))
        self.reindex()
        j = self.pack_json("src/a.py")
        self.assertIn(f'"{long_text}"', j["text"])
        self.assertIn("[... 1 older link(s) omitted]", j["text"])
        self.assertNotIn("OLDER-HISTORY", j["text"])
        self.assertEqual(j["omitted"], [])
        # current link fits but current + marker does not: the whole topic goes, and is named
        marker_free = j["bytes"] - len("  [... 1 older link(s) omitted]\n")
        k = self.pack_json("src/a.py", "--max-bytes", str(marker_free + 5))
        self.assertEqual(k["omitted"], ["TOP-8010"])
        self.assertNotIn("END-OF-RULING", k["text"])
        self.assertLessEqual(k["bytes"], marker_free + 5)

    def test_blind_obeys_max_bytes(self):
        for bad in ("1", "0", "-5"):
            rc, out, err = self.run_cmd("context-pack", "--blind", "--max-bytes", bad)
            self.assertEqual(rc, 2, bad)
            self.assertEqual(out, "")
        need = len(memidx.CONTEXT_PACK_BLIND_TEXT.encode("utf-8"))
        rc, out, _ = self.run_cmd("context-pack", "--blind", "--max-bytes", str(need - 1))
        self.assertEqual(rc, 2)
        rc, out, _ = self.run_cmd("context-pack", "--blind", "--max-bytes", str(need), "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["bytes"], need)

    def test_current_ruling_that_does_not_fit_the_pack_omits_the_whole_topic(self):
        long_text = "BEGIN " + "clause; " * 400 + "END-OF-RULING"
        self.add("a", multi_link_topic("TOP-8002", "Long", ["src/a.py"], long_text, []))
        self.add("b", topic_text("TOP-8003", "Short", ["src/a.py"], "short ruling", authority="agent-inference"))
        self.reindex()
        j = self.pack_json("src/a.py", "--max-bytes", "2600")
        self.assertIn("TOP-8002", j["omitted"])
        self.assertNotIn("BEGIN", j["text"])
        self.assertLessEqual(j["bytes"], 2600)
        self.assertNotIn("TOP-8003 Short", j["text"])   # tail: dropped after the topic that did not fit

    def test_older_links_shed_before_the_current_link_when_tight(self):
        olds = [f"OLDLINK{i} " + "x" * 300 for i in range(5)]
        self.add("a", multi_link_topic("TOP-8004", "Hist", ["src/a.py"], "CURRENT-RULING", olds))
        self.reindex()
        full = self.pack_json("src/a.py")
        self.assertIn("OLDLINK4", full["text"])
        j = self.pack_json("src/a.py", "--max-bytes", str(full["bytes"] - 700))
        self.assertEqual(j["omitted"], [])
        self.assertIn("CURRENT-RULING", j["text"])
        self.assertIn("older link(s) omitted", j["text"])
        self.assertLessEqual(j["bytes"], full["bytes"] - 700)

    def test_max_bytes_is_a_hard_cap_and_usage_error_when_fixed_part_does_not_fit(self):
        self.add("a", topic_text("TOP-8101", "Alpha", ["src/a.py"], "alpha", standing=True))
        self.reindex()
        for bad in ("0", "-5"):
            rc, out, err = self.run_cmd("context-pack", "src/a.py", "--max-bytes", bad)
            self.assertEqual(rc, 2, bad)
            self.assertEqual(out, "")
        rc, out, err = self.run_cmd("context-pack", "src/a.py", "--max-bytes", "100", "--json")
        self.assertEqual(rc, 2)
        self.assertIn("max-bytes 100 too small: fixed part needs", json.loads(out)["error"])
        needed = int(re.search(r"fixed part needs (\d+) bytes", err).group(1))
        rc, out, _ = self.run_cmd("context-pack", "src/a.py", "--max-bytes", str(needed), "--json")
        self.assertEqual(rc, 0)
        j = json.loads(out)
        self.assertEqual(j["bytes"], needed)
        self.assertEqual(j["omitted"], ["TOP-8101"])
        rc, _, err = self.run_cmd("context-pack", "src/a.py", "--max-bytes", str(needed - 1))
        self.assertEqual(rc, 2)

    def test_long_omission_list_counts_against_the_cap(self):
        for i in range(30):
            self.add(f"t{i}", topic_text(f"TOP-82{i:02d}", f"T{i}", ["src/a.py"], "r" * 200,
                                         authority="agent-inference"))
        self.reindex()
        rc, _, err = self.run_cmd("context-pack", "src/a.py", "--max-bytes", "1")
        needed = int(re.search(r"needs (\d+) bytes", err).group(1))   # includes all 30 ids
        self.assertGreater(needed, 30 * 10)
        for cap in (needed, needed + 150, needed + 900):
            j = self.pack_json("src/a.py", "--max-bytes", str(cap))
            self.assertLessEqual(j["bytes"], cap)

    def test_upgrade_required_but_readable_index_still_emits(self):
        self.add("a", topic_text("TOP-8301", "Alpha", ["src/a.py"], "alpha ruling", standing=True))
        self.reindex()
        conn = sqlite3.connect(str(self.db))
        conn.execute("INSERT OR REPLACE INTO db_meta (key, value) VALUES ('index_generation', '1')")
        conn.commit(); conn.close()
        j = self.pack_json("src/a.py")
        self.assertEqual(j["state"], "upgrade-required")
        self.assertIn("Index state: upgrade-required", j["text"])
        self.assertIn("alpha ruling", j["text"])

    def test_unreadable_legacy_schema_exits_1_with_reason(self):
        self.add("a", topic_text("TOP-8302", "Alpha", ["src/a.py"], "alpha", standing=True))
        self.reindex()
        conn = sqlite3.connect(str(self.db))
        conn.execute("ALTER TABLE records DROP COLUMN standing")
        conn.commit(); conn.close()
        rc, out, err = self.run_cmd("context-pack", "src/a.py")
        self.assertEqual(rc, 1)
        self.assertIn("upgrade-required: index unreadable, run reindex", err)

    def test_store_root_from_env_or_usage_error(self):
        self.add("a", topic_text("TOP-8401", "Alpha", ["src/a.py"], "alpha"))
        self.reindex()
        with mock.patch.dict(os.environ, {"MEMCONTINUUM_ROOT": "/some/store"}):
            rc, out, _ = self.run_cmd("context-pack", "src/a.py", with_root=False)
            self.assertEqual(rc, 0)
            self.assertIn("(/some/store)", out)
        env = {k: v for k, v in os.environ.items() if k != "MEMCONTINUUM_ROOT"}
        with mock.patch.dict(os.environ, env, clear=True):
            rc, out, err = self.run_cmd("context-pack", "src/a.py", with_root=False)
            self.assertEqual(rc, 2)
            self.assertEqual(out, "")
            self.assertIn("store root unknown: pass --root or set MEMCONTINUUM_ROOT", err)
            rc, out, _ = self.run_cmd("context-pack", "--blind", with_root=False)   # blind names no store
            self.assertEqual(rc, 0)
        self.assertNotIn("not configured", out)

    def test_standing_overflow_is_reported_not_claimed_as_delivered(self):
        for i in range(memidx.STANDING_CAP_LINKS + 1):
            self.add(f"s{i}", topic_text(f"TOP-85{i:02d}", f"S{i}", [f"src/s{i}.py"], "s", standing=True))
        self.reindex()
        j = self.pack_json("src/none.py")
        self.assertIn("not included -- the digest exceeds the store-wide cap", j["text"])
        self.assertEqual(len(j["standing_omitted"]), memidx.STANDING_CAP_LINKS + 1)
        self.assertEqual(j["topics"], [])

    def test_named_topics_rank_first_and_are_shown_once_in_the_named_block(self):
        pad = " filler" * 150
        self.add("own", topic_text("TOP-8601", "OwnerBound", ["src/a.py"], "OWNERBOUND" + pad,
                                   authority="owner-verbatim"))
        self.add("inf", topic_text("TOP-8602", "InfNamed", ["src/zzz.py"], "INFNAMED" + pad,
                                   authority="agent-inference"))
        self.add("both", topic_text("TOP-8603", "BothNamed", ["src/a.py"], "BOTH" + pad,
                                    authority="owner-ratified"))
        self.reindex()
        full = self.pack_json("src/a.py", "--topic", "TOP-8602", "--topic", "TOP-8603")
        t = full["text"]
        self.assertEqual(t.count("TOP-8603 BothNamed"), 1)
        self.assertLess(t.index("Named explicitly:"), t.index("TOP-8603 BothNamed"))
        # named order: authority then id -> ratified TOP-8603 before inference TOP-8602
        self.assertLess(t.index("TOP-8603 BothNamed"), t.index("TOP-8602 InfNamed"))
        j = self.pack_json("src/a.py", "--topic", "TOP-8602", "--max-bytes", str(full["bytes"] - 2200))
        self.assertIn("INFNAMED", j["text"])
        self.assertIn("TOP-8601", j["omitted"])   # owner-verbatim, but only bound
        self.assertLessEqual(j["bytes"], full["bytes"] - 2200)

    def test_ordering_ties_authority_then_file_count_then_id(self):
        self.add("a", topic_text("TOP-8703", "Rat1", ["src/a.py"], "r", authority="owner-ratified"))
        self.add("b", topic_text("TOP-8702", "Rat2", ["src/a.py", "src/b.py"], "r", authority="owner-ratified"))
        self.add("c", topic_text("TOP-8701", "Rat3", ["src/a.py"], "r", authority="owner-ratified"))
        self.add("d", topic_text("TOP-8700", "Inf", ["src/a.py", "src/b.py"], "r", authority="agent-inference"))
        self.add("e", topic_text("TOP-8704", "Own", ["src/a.py"], "r", authority="owner-verbatim"))
        self.reindex()
        t = self.pack("src/a.py", "src/b.py")
        order = [m for m in re.findall(r"^(TOP-87\d\d) ", t, re.M)]
        self.assertEqual(order, ["TOP-8704", "TOP-8702", "TOP-8701", "TOP-8703", "TOP-8700"])

    def test_concept_governed_topic_is_bound_to_the_concept_file(self):
        self.add("g", topic_text("TOP-8801", "Governing", [], "governed ruling"))
        (self.root / "concepts").mkdir()
        (self.root / "concepts" / "c.md").write_text(
            "---\ntype: concept\nid: CON-8801\ntitle: Svc\nowner_boundary: edge\n"
            "implemented_by: [src/svc.py]\ngoverned_by: [TOP-8801]\n---\nBody.\n")
        self.reindex()
        j = self.pack_json("src/svc.py")
        self.assertIn("TOP-8801 Governing", j["text"])
        self.assertIn("bound to: src/svc.py", j["text"])
        self.assertEqual([t["via"] for t in j["topics"] if t["id"] == "TOP-8801"], [["src/svc.py"]])

    def test_query_never_enters_the_vector_path(self):
        self.add("a", topic_text("TOP-8901", "Zebra", ["src/a.py"], "zebra crossing"))
        self.reindex()
        boom = AssertionError("vector/embedding path entered")
        with mock.patch.object(memidx, "vector_ranked", side_effect=boom), \
             mock.patch.object(memidx, "load_embedding_model", side_effect=boom), \
             mock.patch.object(memidx, "try_compute_embeddings", side_effect=boom):
            j = self.pack_json("--query", "zebra")
        self.assertIn("TOP-8901 Zebra", j["text"])

    def test_blind_never_opens_an_index(self):
        self.add("a", topic_text("TOP-8902", "Alpha", ["src/a.py"], "alpha"))
        self.reindex()
        boom = AssertionError("index opened in blind mode")
        with mock.patch.object(memidx, "open_db_readonly", side_effect=boom), \
             mock.patch.object(memidx, "open_db_noncreating", side_effect=boom), \
             mock.patch.object(memidx, "open_db", side_effect=boom), \
             mock.patch.object(memidx, "decision_index_state_readonly", side_effect=boom), \
             mock.patch.object(memidx, "decision_index_state", side_effect=boom), \
             mock.patch("sqlite3.connect", side_effect=boom):
            out = self.pack("src/a.py", "--blind")
        self.assertIn("Blind review:", out)


GOLDEN = TOOLS_DIR / "tests" / "goldens" / "for_path_concept"


def inventory_row(rid, impl_path, pad=""):
    return {"id": rid, "job": f"job of {rid}" + pad, "policy": "allowed_variants",
            "implementation": f"{impl_path}#f", "contract": "c", "why_single": "w",
            "related_paths": [], "variants": [{"symbol": f"{impl_path}#g", "contract": "c", "reason": "r"}],
            "guards": [], "coverage_limit": "l", "evidence": "e", "test": "tests.test_c7_inventory.TestPositiveControls"}


class TestContextPackInventory(PackCase):
    """C7: the pack appends the reuse-inventory rows of the code repository it
    is given (--code-root), inside the byte cap and the hash."""

    def code_repo(self, rows):
        repo = self.td / "code"
        (repo / "docs").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "C7-INVENTORY.json").write_text(json.dumps(
            {"schema_version": 1, "scan_roots": ["."], "exclusions": ["tests"], "rows": rows}), encoding="utf-8")
        return repo

    def setUp(self):
        super().setUp()
        self.add("a", topic_text("TOP-9501", "Alpha", ["src/a.py"], "alpha ruling"))
        self.reindex()
        # --code-root also adds anchor lines, so "no inventory" baselines use a root without one.
        self.bare = self.td / "bare"
        self.bare.mkdir()

    def plain(self, *args):
        return self.pack_json(*args, "--code-root", str(self.bare))

    def test_matching_rows_are_listed_under_the_label_inside_the_hash(self):
        repo = self.code_repo([inventory_row("row-a", "src/a.py"), inventory_row("row-b", "src/b.py"),
                               inventory_row("row-other", "src/other.py")])
        out = self.pack("src/a.py", "src/b.py", "--code-root", str(repo))
        text = self.text_part(out)
        self.assertIn("Implementation policy (repository inventory)", text)
        self.assertIn("[row-a] job of row-a", text)
        self.assertIn("[row-b] job of row-b", text)
        self.assertNotIn("row-other", text)
        self.assertLess(text.index("TOP-9501"), text.index("Implementation policy"))
        sha, nbytes, line = memidx.pack_hash_line(text)
        self.assertEqual(out.rstrip("\n").rpartition("\n")[2], line)
        j = self.pack_json("src/a.py", "src/b.py", "--code-root", str(repo))
        self.assertEqual(j["inventory"], ["row-a", "row-b"])
        self.assertEqual(j["inventory_omitted"], [])
        self.assertEqual(j["sha256"], sha)

    def test_without_a_code_root_or_an_inventory_the_pack_is_unchanged(self):
        repo = self.code_repo([inventory_row("row-a", "src/a.py")])
        self.assertNotIn("Implementation policy", self.pack("src/a.py"))      # no --code-root: never injected
        j = self.plain("src/a.py")                                            # this repo carries none
        self.assertNotIn("inventory", j)
        self.assertNotIn("Implementation policy", j["text"])
        self.assertEqual(self.pack("src/zzz.py", "--code-root", str(repo)).count("Implementation policy"), 0)

    def test_an_unusable_inventory_is_ignored_not_fatal(self):
        repo = self.td / "code"
        (repo / "docs").mkdir(parents=True)
        (repo / "docs" / "C7-INVENTORY.json").write_text("{not json", encoding="utf-8")
        base = self.plain("src/a.py")
        j = self.pack_json("src/a.py", "--code-root", str(repo))
        self.assertEqual(j["text"].replace(str(repo), ""), base["text"].replace(str(self.bare), ""))
        self.assertNotIn("inventory", j)

    def test_cap_keeps_whole_rows_and_names_the_omitted_ids_inside_the_pack(self):
        pad = " word" * 120
        rows = [inventory_row("row-a", "src/a.py", pad), inventory_row("row-b", "src/a.py", pad),
                inventory_row("row-c", "src/a.py", pad)]
        repo = self.code_repo(rows)
        full = self.pack_json("src/a.py", "--code-root", str(repo))
        self.assertEqual(full["inventory_omitted"], [])
        one_row = len(memidx.mc_reuse.render_row(rows[0]).encode("utf-8"))
        cap = full["bytes"] - one_row - 20
        j = self.pack_json("src/a.py", "--code-root", str(repo), "--max-bytes", str(cap))
        self.assertLessEqual(j["bytes"], cap)
        self.assertEqual(j["inventory"] + j["inventory_omitted"], ["row-a", "row-b", "row-c"])
        self.assertTrue(j["inventory_omitted"])
        self.assertEqual(j["inventory"], ["row-a", "row-b", "row-c"][:len(j["inventory"])])   # head rows, in order
        m = re.search(r"\[(\d+) inventory row\(s\) omitted by the size cap: ([^\]]+)\]", j["text"])
        self.assertIsNotNone(m)
        self.assertEqual(m.group(2).split(", "), j["inventory_omitted"])
        for rid in j["inventory"]:
            self.assertIn(f"[{rid}] job of {rid}" + pad, j["text"])           # never cut mid-row
        for rid in j["inventory_omitted"]:
            self.assertNotIn(f"[{rid}] job of", j["text"])
        self.assertEqual(j["sha256"], memidx.pack_hash_line(j["text"])[0])

    def test_when_not_even_the_omission_line_fits_the_ids_go_to_the_envelope_only(self):
        pad = " word" * 120
        repo = self.code_repo([inventory_row("row-a", "src/a.py", pad)])
        plain = self.plain("src/a.py")
        j = self.pack_json("src/a.py", "--code-root", str(repo), "--max-bytes", str(plain["bytes"]))
        self.assertLessEqual(j["bytes"], plain["bytes"])
        self.assertEqual(j["inventory"], [])
        self.assertEqual(j["inventory_omitted"], ["row-a"])
        self.assertNotIn("inventory row(s) omitted", j["text"])
        self.assertEqual(j["text"], plain["text"])

    def test_decision_records_are_never_displaced_by_the_inventory(self):
        pad = " word" * 400
        repo = self.code_repo([inventory_row("row-a", "src/a.py", pad)])
        plain = self.plain("src/a.py")
        j = self.pack_json("src/a.py", "--code-root", str(repo), "--max-bytes", str(plain["bytes"] + 50))
        self.assertIn("alpha ruling", j["text"])
        self.assertEqual(j["omitted"], plain["omitted"])

    def test_the_engines_own_inventory_is_selected_for_its_own_files(self):
        out = self.pack("hooks/mc_nudge.py", "--code-root", str(TOOLS_DIR))
        self.assertIn("[coverage-fact-sentence]", out)
        self.assertIn("[coverage-question-sentence]", out)
        self.assertNotIn("[join-semicolon-list]", out)


class TestForPathConceptSnapshot(PackCase):
    """`for-path` output on a concept-governed path is byte-identical to the
    answer recorded before the shared resolver replaced its own composition."""

    def test_concept_governed_answer_is_unchanged(self):
        self.add("d", topic_text("TOP-9801", "Direct", ["src/svc.py"], "direct ruling", authority="owner-verbatim"))
        self.add("g", topic_text("TOP-9802", "Governing", [], "governed ruling"))
        (self.root / "concepts").mkdir()
        (self.root / "concepts" / "c.md").write_text(
            "---\ntype: concept\nid: CON-9801\ntitle: Service concept\n"
            "owner_boundary: the service boundary\nimplemented_by: [src/svc.py]\n"
            "governed_by: [TOP-9802]\n---\nBody.\n")
        self.reindex()
        root = str(self.root.resolve())
        for argv, golden in ((["for-path", "src/svc.py"], "expected.txt"),
                             (["for-path", "src/svc.py", "--json"], "expected.json"),
                             (["for-path", "src/svc.py", "--json", "--with-chain-text"],
                              "expected_chain_text.json")):
            rc, out, err = self.run_cmd(*argv, with_root=False)
            self.assertEqual(rc, 0, err)
            self.assertEqual(out.replace(root, "@ROOT@"), (GOLDEN / golden).read_text(), golden)


if __name__ == "__main__":
    unittest.main()
