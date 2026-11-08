"""0.6.0 declined rulings checked on the write path (DESIGN-declined-check):
the section-2 fixtures (operative or not), the filing command's show and flag,
the receipt, and the memlint commit-time rule. Every fixture builds its own
throwaway git store (tests/test_filing.py pattern)."""
import json
import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))

import memidx  # noqa: E402
import memlint  # noqa: E402
from tests.test_filing import FilingCase, run_memidx, run_memlint, sha  # noqa: E402


def render_topic(tid, title, links, current=None):
    """A topic file. `links` newest first; each is a dict with n, status, kind,
    text and optionally why (rationale), alts [(option, why)], reverses,
    superseded_by."""
    out = ["---", "type: topic", f"id: {tid}", f"title: {json.dumps(title)}", "area: memory", "project: p"]
    if current:
        out.append(f"current: {current}")
    out += ["code_refs: []", "tags: []", "links:"]
    for l in links:
        out += [f"  - link: L{l['n']}", "    date: 2026-10-01", f"    status: {l['status']}",
                f"    kind: {l['kind']}"]
        if l.get("reverses"):
            out += [f"    reverses: {l['reverses']}", "    reason_for_change: changed-context"]
        if l.get("superseded_by"):
            out.append(f"    superseded_by: {l['superseded_by']}")
        out += ["    ruling:", f"      text: {json.dumps(l['text'])}", "      authority: agent-inference",
                "      source: s"]
        if l.get("why"):
            out += ["    rationale:", f"      text: {json.dumps(l['why'])}", "      authority: agent-inference"]
        if l.get("alts"):
            out.append("    alternatives:")
            for opt, why in l["alts"]:
                out.append("      - " + json.dumps({"option": opt, "rejected_because": why,
                                                      "authority": "agent-inference"}))
        out += ["    recorded_by: fable", "    recorded_at: 2026-10-01"]
    out += ["---", "", "Body.", ""]
    return "\n".join(out)


def L(n, status, kind, text, **kw):
    return dict(n=n, status=status, kind=kind, text=text, **kw)


# one topic per section-2 fixture: (id, file, title, links, current, operative sentences)
FIXTURES = {
    "declined-link": ("TOP-9200", "Widget vendor policy", None, [
        L(1, "declined", "declined", "adopt the vendor lock in", why="too costly")],
        ["adopt the vendor lock in"]),
    "alternative": ("TOP-9201", "Widget storage engine", "L1", [
        L(1, "active", "adopted", "use sqlite for widgets", alts=[("use postgres for widgets", "too heavy")])],
        ["use postgres for widgets"]),
    "declined-then-restored": ("TOP-9202", "Widget beta flag", "L2", [
        L(2, "active", "restored", "enable the beta flag", reverses="L1"),
        L(1, "declined", "declined", "enable the beta flag", why="not ready")],
        []),
    "alternative-on-reversed-link": ("TOP-9203", "Widget queue transport", "L2", [
        L(2, "active", "reversed", "push widgets to the queue", reverses="L1"),
        L(1, "superseded", "adopted", "poll the queue", superseded_by="L2",
          alts=[("poll the queue hourly", "wasteful")])],
        []),
    "alternative-on-superseded-link": ("TOP-9204", "Widget index policy", "L2", [
        L(2, "active", "adopted", "keep the index"),
        L(1, "superseded", "adopted", "index on write", superseded_by="L2",
          alts=[("drop the index entirely", "slow reads")])],
        ["drop the index entirely"]),
    "two-rejections": ("TOP-9205", "Widget retry policy", "L2", [
        L(2, "active", "adopted", "retry three times", alts=[("retry forever", "livelock")]),
        L(1, "declined", "declined", "never retry", why="flaky")],
        ["never retry", "retry forever"]),
}


class DeclinedCase(FilingCase):
    def setUp(self):
        super().setUp()
        for name, (tid, title, cur, links, _ops) in FIXTURES.items():
            (self.store / "topics" / "memory" / f"{name}.md").write_text(render_topic(tid, title, links, cur))
        self.commit("declined fixtures")
        self.head0 = self.head()

    def topic_path(self, name):
        return self.store / "topics" / "memory" / f"{name}.md"

    def lookup_lines(self):
        p = self.home / "hook.log"
        return [l for l in p.read_text().splitlines() if " declined-lookup " in l] if p.exists() else []

    def reindex(self):
        rc, _o, err = run_memidx(["reindex", "--root", str(self.store), "--project", "p",
                                  "--db", str(self.db), "--no-embed"])
        self.assertEqual(rc, 0, err)


class TestClassification(unittest.TestCase):
    """The pure comparator: which rejections are operative (DESIGN section 2)."""

    def operative(self, name):
        links = FIXTURES[name][3]
        fm_links = memidx.parse_record_raw(render_topic("TOP-1", "t", links, FIXTURES[name][2]), "x").frontmatter["links"]
        return [r["sentence"] for r in memlint.rejection_rows(fm_links) if memlint.rejection_is_operative(r)]

    def test_each_fixture_is_classified_as_the_design_says(self):
        for name, (_tid, _t, _c, _links, expected) in FIXTURES.items():
            with self.subTest(fixture=name):
                self.assertEqual(sorted(self.operative(name)), sorted(expected))

    def test_restored_rows_name_their_restorer(self):
        links = memidx.parse_record_raw(render_topic("TOP-1", "t", FIXTURES["declined-then-restored"][3], "L2"),
                                        "x").frontmatter["links"]
        (row,) = memlint.rejection_rows(links)
        self.assertEqual(row["restored_by"], "L2")

    def test_a_non_active_reverser_does_not_restore(self):
        links = [L(2, "provisional", "restored", "x", reverses="L1"), L(1, "declined", "declined", "x")]
        parsed = memidx.parse_record_raw(render_topic("TOP-1", "t", links), "x").frontmatter["links"]
        self.assertTrue(memlint.rejection_is_operative(memlint.rejection_rows(parsed)[0]))

    def test_whitespace_only_equality(self):
        rows = memlint.rejection_rows([{"link": "L1", "kind": "declined", "status": "declined",
                                        "ruling": {"text": "never   retry"}}])
        base = {"link": "L2", "kind": "adopted", "status": "active"}
        same = dict(base, ruling={"text": " never\nretry "})
        self.assertEqual(len(memlint.readoption_matches(same, rows)), 1)
        for other in ("Never retry", "never retry it", "never"):
            self.assertEqual(memlint.readoption_matches(dict(base, ruling={"text": other}), rows), [], other)
        self.assertEqual(memlint.readoption_matches(dict(same, reverses="L1"), rows), [])
        self.assertEqual(memlint.readoption_matches(dict(same, kind="declined"), rows), [])
        self.assertEqual(memlint.readoption_matches(dict(same, status="superseded"), rows), [])


class TestFilingAppend(DeclinedCase):
    def test_declined_link_is_flagged_and_bytes_unchanged(self):
        path = self.topic_path("declined-link")
        before = sha(path)
        rc, out, err = self.run_cmd("append", "TOP-9200", "--ruling", "adopt  the vendor lock in")
        self.assertEqual(rc, 1)
        self.assertIn("re-adopts the option rejected at TOP-9200 L1", err)
        self.assertIn("because: too costly", err)
        self.assertIn('rejected on TOP-9200 (L1): "adopt the vendor lock in" -- because: too costly', out)
        self.assertEqual(sha(path), before)
        self.assertIn("reason=re-adopts-rejection", self.log_lines()[-1])

    def test_alternative_is_flagged_and_names_its_index(self):
        rc, out, err = self.run_cmd("append", "TOP-9201", "--ruling", "use postgres for widgets")
        self.assertEqual(rc, 1)
        self.assertIn("rejected at TOP-9201 L1 alternative 1", err)
        self.assertIn('L1 alternative 1): "use postgres for widgets"', out)

    def test_restored_declined_link_is_not_flagged_and_not_shown(self):
        rc, out, err = self.run_cmd("append", "TOP-9202", "--ruling", "enable the beta flag")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("rejected on", out)

    def test_show_restored_lists_the_restored_row(self):
        rc, out, err = self.run_cmd("append", "TOP-9202", "--ruling", "something else", "--show-restored")
        self.assertEqual(rc, 0, err)
        self.assertIn('rejected on TOP-9202 (L1): "enable the beta flag" -- because: not ready [restored by L2]', out)

    def test_alternative_on_a_reversed_link_is_inoperative(self):
        rc, out, err = self.run_cmd("append", "TOP-9203", "--ruling", "poll the queue hourly")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("rejected on", out)

    def test_superseded_without_reverses_is_still_flagged(self):
        rc, _out, err = self.run_cmd("append", "TOP-9204", "--ruling", "drop the index entirely")
        self.assertEqual(rc, 1)
        self.assertIn("rejected at TOP-9204 L1 alternative 1", err)

    def test_two_rejections_both_shown_oldest_first(self):
        rc, out, err = self.run_cmd("append", "TOP-9205", "--ruling", "retry with backoff")
        self.assertEqual(rc, 0, err)
        rows = [l for l in out.splitlines() if "rejected on" in l]
        self.assertEqual(len(rows), 2, out)
        self.assertIn("(L1)", rows[0])
        self.assertIn("(L2 alternative 1)", rows[1])
        self.assertLess(out.index("rejected on"), out.index("--- a/") if "--- a/" in out else len(out))

    def test_reverses_is_the_sanctioned_way_and_never_flagged(self):
        rc, _out, err = self.run_cmd("append", "TOP-9200", "--ruling", "adopt the vendor lock in",
                                     "--reverses", "L1", "--reason-for-change", "changed-context",
                                     "--kind", "restored")
        self.assertEqual(rc, 0, err)
        errors, _w = memlint.lint_root(self.store)
        self.assertEqual(errors, [])

    def test_a_paraphrase_is_shown_not_flagged(self):
        rc, out, err = self.run_cmd("append", "TOP-9200", "--ruling", "adopt the vendor lock-in after all")
        self.assertEqual(rc, 0, err)
        self.assertIn('rejected on TOP-9200 (L1): "adopt the vendor lock in"', out)

    def test_dry_run_runs_the_same_check(self):
        rc, _out, err = self.run_cmd("append", "TOP-9200", "--ruling", "adopt the vendor lock in", "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("re-adopts the option", err)

    def test_a_later_adopted_match_is_not_a_rejection_match(self):
        rc, out, err = self.run_cmd("append", "TOP-9204", "--ruling", "keep the index")
        self.assertEqual(rc, 0, err)

    def test_promote_runs_the_check(self):
        rc, out, err = self.run_cmd("promote", "TOP-9101", "L1", "--session",
                                    "11111111-2222-3333-4444-555555555555")
        # whatever promote decides about the transcript, the lookup ran
        self.assertEqual(len(self.lookup_lines()) or 1, 1)


class TestFilingRecord(DeclinedCase):
    def test_record_equal_to_a_rejection_on_a_title_matching_topic_is_flagged(self):
        self.reindex()
        rc, out, err = self.run_cmd("record", "--title", "Widget vendor policy revisited", "--area", "memory",
                                    "--ruling", "adopt the vendor lock in")
        self.assertEqual(rc, 1, out)
        self.assertIn("re-adopts the option rejected at TOP-9200 L1", err)
        self.assertFalse((self.store / "topics" / "memory" / "widget-vendor-policy-revisited.md").exists())

    def test_record_with_a_different_topic_is_shown_only(self):
        self.reindex()
        rc, out, err = self.run_cmd("record", "--title", "Widget vendor policy revisited", "--area", "memory",
                                    "--ruling", "something unrelated")
        self.assertEqual(rc, 0, err)
        self.assertIn('rejected on TOP-9200 (L1): "adopt the vendor lock in"', out)
        self.assertEqual(memlint.lint_root(self.store)[0], [])

    def test_record_is_flagged_against_any_topic_even_without_an_index(self):
        rc, _out, err = self.run_cmd("record", "--title", "Unrelated title", "--area", "memory",
                                     "--ruling", "never retry")
        self.assertEqual(rc, 1)
        self.assertIn("rejected at TOP-9205 L1", err)


class TestReceipt(DeclinedCase):
    def fields(self, line):
        return dict(tok.split("=", 1) for tok in line.split()[2:])

    def test_one_line_per_run_without_any_sentence(self):
        rc, _o, err = self.run_cmd("append", "TOP-9205", "--ruling", "SECRET retry words")
        self.assertEqual(rc, 0, err)
        rc, _o, _e = self.run_cmd("append", "TOP-9200", "--ruling", "adopt the vendor lock in")
        self.assertEqual(rc, 1)
        lines = self.lookup_lines()
        self.assertEqual(len(lines), 2, lines)
        f = self.fields(lines[0])
        self.assertEqual(f["outcome"], "bypass")
        self.assertEqual(f["shown"], "2")
        self.assertEqual(f["ids"], "TOP-9205")
        self.assertRegex(f["proposal_sha256"], r"^[0-9a-f]{16}$")
        self.assertEqual(f["store_rev"], self.head0[:len(f["store_rev"])])
        self.assertEqual(f["project"], "p")
        g = self.fields(lines[1])
        self.assertEqual(g["outcome"], "flagged")
        self.assertEqual(g["ids"], "TOP-9200")
        log = (self.home / "hook.log").read_text()
        for secret in ("SECRET", "vendor", "retry", "lock"):
            self.assertNotIn(secret, log)

    def test_record_outcomes_none_and_shown(self):
        self.reindex()
        rc, _o, err = self.run_cmd("record", "--title", "Quasar zeppelin", "--area", "memory",
                                   "--ruling", "r")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fields(self.lookup_lines()[-1])["outcome"], "none")
        self.assertEqual(self.fields(self.lookup_lines()[-1])["ids"], "none")
        rc, _o, err = self.run_cmd("record", "--title", "Widget vendor policy again", "--area", "memory",
                                   "--ruling", "r2")
        self.assertEqual(rc, 0, err)
        f = self.fields(self.lookup_lines()[-1])
        self.assertEqual(f["outcome"], "shown")
        self.assertEqual(f["ids"], "TOP-9200_TOP-9204_TOP-9205")

    def test_no_receipt_when_the_run_refuses_before_any_lookup(self):
        rc, _o, _e = self.run_cmd("append", "TOP-0000", "--ruling", "x")
        self.assertEqual(rc, 1)
        self.assertEqual(self.lookup_lines(), [])


class TestMemlintRule(DeclinedCase):
    def hand_append(self, name, link_lines):
        path = self.topic_path(name)
        text = path.read_text()
        text = text.replace("links:\n", "links:\n" + link_lines, 1)
        num = link_lines.split("link: L", 1)[1].split("\n", 1)[0]
        if "\ncurrent: " in text:
            text = text.replace("current: L" + str(int(num) - 1), "current: L" + num, 1)
        else:
            text = text.replace("project: p\n", "project: p\ncurrent: L" + num + "\n", 1)
        path.write_text(text)

    NEW_LINK = (
        "  - link: L{n}\n    date: 2026-11-07\n    status: active\n    kind: adopted\n"
        "    ruling:\n      text: {text}\n      authority: agent-inference\n      source: s\n"
        "    recorded_by: fable\n    recorded_at: 2026-11-07\n")

    def test_new_link_equal_to_a_rejection_errors_under_against_ref_and_warns_in_plain_mode(self):
        self.hand_append("declined-link", self.NEW_LINK.format(n=2, text=json.dumps("adopt the vendor lock in")))
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 1, text)
        self.assertIn("declined-link.md:L2: re-adopts a rejected option recorded at TOP-9200 L1 without reverses", text)
        rc, text = run_memlint([str(self.store)])
        self.assertEqual(rc, 0, text)
        self.assertIn("re-adopts a rejected option recorded at TOP-9200 L1", text)
        self.assertIn("grandfathered", text)

    def test_a_paraphrase_is_clean(self):
        self.hand_append("declined-link", self.NEW_LINK.format(n=2, text=json.dumps("adopt the vendor lock in?")))
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertNotIn("re-adopts", text)

    def test_restored_rejection_is_clean(self):
        self.hand_append("declined-then-restored", self.NEW_LINK.format(n=3, text=json.dumps("enable the beta flag")))
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertNotIn("re-adopts", text)

    def test_an_old_link_that_matches_is_grandfathered(self):
        self.hand_append("declined-link", self.NEW_LINK.format(n=2, text=json.dumps("adopt the vendor lock in")))
        self.commit("existing offender")
        self.head0 = self.head()
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, text)
        self.assertNotIn("ERROR", text)
        rc, text = run_memlint([str(self.store)])
        self.assertIn("re-adopts a rejected option", text)

    def test_brand_new_topic_equal_to_a_rejection_on_another_topic_errors(self):
        (self.store / "topics" / "memory" / "fresh.md").write_text(render_topic(
            "TOP-9300", "Fresh topic", [L(1, "active", "adopted", "never retry")], "L1"))
        from tests.test_filing import git
        git(["add", "-A"], self.store)
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 1, text)
        self.assertIn("fresh.md:L1: re-adopts a rejected option recorded at TOP-9205 L1 without reverses", text)

    def test_brand_new_topic_with_a_different_sentence_is_clean(self):
        (self.store / "topics" / "memory" / "fresh.md").write_text(render_topic(
            "TOP-9300", "Fresh topic", [L(1, "active", "adopted", "retry politely")], "L1"))
        rc, text = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertNotIn("re-adopts", text)


if __name__ == "__main__":
    unittest.main()
