"""0.6.0 hygiene PR -- the frontmatter guard gaps (TOP-0137 L26): `code_refs:`
membership under the guard through the frozen `ref_marks:` rows, `bind`
writing its row and restoring an unbound ref, `unbind`, `type` frozen, and the
anchor rule's "unbound" clause. Every fixture builds its own throwaway store."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(TOOLS_DIR / "tests"))

import memidx  # noqa: E402
import memlint  # noqa: E402
from test_binding import (  # noqa: E402
    PY_SRC, anchor_block, good_row, make_code_root, ref_mark_text, topic_text as binding_topic,
)
from test_standing_marks import (  # noqa: E402
    MARK_FLAGS, MARK_OWNER, MARK_SID, MARK_YES, ErrCase, GuardCase, TmpCase, commit_all, git,
    git_store, lint_text, link_yaml, run_memidx, run_memlint, topic_text, write_mark_transcript,
)

STAMP = "2026-11-03T09:12:41-05:00"
LATER = "2026-11-04T09:12:41-05:00"


def rmark(ref, action="add", recorded_at=STAMP, why="because", authority="agent-inference",
          source="a test", nl="\n"):
    return (
        f"  - ref: \"{ref}\"{nl}"
        f"    action: {action}{nl}"
        f"    date: {recorded_at[:10]}{nl}"
        f"    recorded_at: \"{recorded_at}\"{nl}"
        f"    recorded_by: tester{nl}"
        f"    authority: {authority}{nl}"
        f"    source: \"{source}\"{nl}"
        f"    why: \"{why}\"{nl}"
    )


def refs_fm(refs, *marks):
    """`code_refs:` then `ref_marks:` (newest row first) front matter text."""
    text = "code_refs:\n" + "".join(f"  - \"{r}\"\n" for r in refs) if refs else "code_refs: []\n"
    if marks:
        text += "ref_marks:\n" + "".join(marks)
    return text


def ttext(refs, *marks, **kw):
    kw.setdefault("standing", "standing: [L2]\n")
    return topic_text(extra_fm=refs_fm(refs, *marks), **kw)


# ---------------------------------------------------------------------------
# the guard: code_refs membership needs its rows
# ---------------------------------------------------------------------------

class TestRefPairing(GuardCase):
    def test_unchanged_membership_needs_no_rows(self):
        base = ttext(["src/a.py"])
        self.store(base)
        self.assertClean(base)

    def test_reorder_only_needs_no_row(self):
        self.store(ttext(["src/a.py", "src/b.py"]))
        self.assertClean(ttext(["src/b.py", "src/a.py"]))

    def test_add_without_a_row_is_refused(self):
        self.store(ttext(["src/a.py"]))
        self.assertRefused(ttext(["src/a.py", "src/b.py"]),
                           "code_refs changed for src/b.py (add) with no matching ref_marks row")

    def test_the_first_change_on_a_topic_with_no_ref_marks_yet_needs_a_row_and_passes_with_one(self):
        self.store(ttext(["src/a.py"]))                       # grandfathered: no ref_marks block
        self.assertClean(ttext(["src/a.py", "src/b.py"], rmark("src/b.py")))

    def test_remove_needs_a_row(self):
        self.store(ttext(["src/a.py", "src/b.py"]))
        self.assertRefused(ttext(["src/a.py"]),
                           "code_refs changed for src/b.py (remove) with no matching ref_marks row")
        self.assertClean(ttext(["src/a.py"], rmark("src/b.py", "remove")))

    def test_prefix_and_glob_refs_are_audited_too(self):
        self.store(ttext(["src/a.py", "src/lib/", "src/*.py"]))
        self.assertRefused(ttext(["src/a.py"]), "src/lib/ (remove)", "src/*.py (remove)")
        self.assertClean(ttext(["src/a.py"], rmark("src/lib/", "remove", LATER),
                               rmark("src/*.py", "remove")))
        self.assertClean(ttext(["src/a.py", "src/lib/", "src/*.py", "docs/"],
                               rmark("docs/", "add")))

    def test_two_refs_in_one_commit_need_two_rows(self):
        self.store(ttext(["src/a.py"]))
        both = ttext(["src/a.py", "src/b.py", "src/c.py"], rmark("src/c.py"), rmark("src/b.py"))
        self.assertClean(both)
        self.assertRefused(ttext(["src/a.py", "src/b.py", "src/c.py"], rmark("src/b.py")),
                           "code_refs changed for src/c.py (add)")

    def test_a_row_for_a_change_that_did_not_happen_is_an_orphan(self):
        self.store(ttext(["src/a.py"]))
        self.assertRefused(ttext(["src/a.py"], rmark("src/a.py")), "orphan reference mark (src/a.py, add)")
        self.assertRefused(ttext(["src/a.py", "src/b.py"], rmark("src/b.py"), rmark("src/zzz.py", "remove")),
                           "orphan reference mark (src/zzz.py, remove)")

    def test_the_same_change_twice_in_one_commit_is_refused(self):
        self.store(ttext(["src/a.py"]))
        self.assertRefused(ttext(["src/a.py", "src/b.py"], rmark("src/b.py", recorded_at=LATER),
                                 rmark("src/b.py")),
                           "duplicate reference mark for (src/b.py, add) in one commit")

    def test_remove_and_add_of_the_same_ref_in_one_commit_changes_nothing_so_both_rows_are_orphans(self):
        self.store(ttext(["src/a.py"]))
        out = self.assertRefused(ttext(["src/a.py"], rmark("src/a.py", "remove", LATER), rmark("src/a.py")),
                                 "orphan reference mark (src/a.py, remove)")
        self.assertIn("orphan reference mark (src/a.py, add)", out)


class TestRefMarksFrozen(GuardCase):
    BASE = ttext(["src/a.py", "src/b.py"], rmark("src/b.py"))

    def test_untouched_rows_pass_and_a_new_row_prepends(self):
        self.store(self.BASE)
        self.assertClean(self.BASE)
        self.assertClean(ttext(["src/a.py"], rmark("src/b.py", "remove", LATER), rmark("src/b.py")))

    def test_a_changed_field_is_refused(self):
        self.store(self.BASE)
        self.assertRefused(ttext(["src/a.py", "src/b.py"], rmark("src/b.py", why="EDITED")),
                           "reference mark (src/b.py, add,", "row changed after being recorded")

    def test_a_reformatted_row_is_refused_by_its_bytes(self):
        self.store(self.BASE)
        reformatted = self.BASE.replace('    why: "because"\n', "    why: because\n")
        self.assertNotEqual(reformatted, self.BASE)
        self.assertRefused(reformatted, "row reformatted, not appended")

    def test_a_deleted_row_is_refused(self):
        self.store(self.BASE)
        self.assertRefused(ttext(["src/a.py", "src/b.py"]), "row removed after being recorded")

    def test_a_row_appended_below_the_recorded_one_is_refused(self):
        self.store(self.BASE)
        self.assertRefused(ttext(["src/a.py", "src/b.py", "src/c.py"], rmark("src/b.py"), rmark("src/c.py")),
                           "marks not prepended")

    def test_untouched_blocks_stay_byte_identical_when_a_row_is_added(self):
        marks = ("standing_marks:\n  - link: L2\n    action: add\n    date: 2026-11-01\n"
                 "    recorded_at: \"2026-11-01T08:00:00-05:00\"\n    recorded_by: t\n"
                 "    authority: owner-ratified\n    source: \"s\"\n    why: \"w\"\n")
        base = topic_text(standing="standing: [L2]\n", marks=marks, extra_fm=refs_fm(["src/a.py"]))
        self.store(base)
        new = topic_text(standing="standing: [L2]\n", marks=marks,
                         extra_fm=refs_fm(["src/a.py", "src/b.py"], rmark("src/b.py")))
        self.assertClean(new)
        old_fm = memidx.parse_record_raw(base, "x").frontmatter
        new_fm = memidx.parse_record_raw(new, "x").frontmatter
        self.assertEqual(old_fm["links"], new_fm["links"])
        self.assertEqual(old_fm["standing_marks"], new_fm["standing_marks"])
        self.assertEqual(memlint._link_raw_spans(base, old_fm["links"]),
                         memlint._link_raw_spans(new, new_fm["links"]))
        self.assertEqual(memlint._link_raw_spans(base, old_fm["standing_marks"], key="standing_marks",
                                                 ident=memlint._mark_span_ident),
                         memlint._link_raw_spans(new, new_fm["standing_marks"], key="standing_marks",
                                                 ident=memlint._mark_span_ident))

    def test_a_crlf_file_is_judged_the_same(self):
        self.store(self.BASE.replace("\n", "\r\n"))
        new = ttext(["src/a.py"], rmark("src/b.py", "remove", LATER), rmark("src/b.py")).replace("\n", "\r\n")
        self.assertClean(new)
        self.assertRefused(new.replace('why: "because"\r\n', 'why: "because"\n'), "row reformatted")


class TestRefMarksOnAddedAndRepairedRecords(GuardCase):
    def staged_new(self, text):
        """Stage a brand-new topic (the worktree diff cannot see an untracked file)."""
        if self.root is None:
            self.store(ttext(["src/a.py"]))
        (self.root / "topics" / "bar.md").write_text(text)
        git(["add", "-A"], self.root)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        git(["reset", "-q"], self.root)
        (self.root / "topics" / "bar.md").unlink()
        return rc, out

    def test_a_brand_new_topic_needs_an_add_row_per_code_ref(self):
        rc, out = self.staged_new(ttext(["src/x.py"], tid="TOP-9101", standing=""))
        self.assertEqual(rc, 1, out)
        self.assertIn("code_refs changed for src/x.py (add) with no matching ref_marks row", out)
        rc, out = self.staged_new(ttext(["src/x.py"], rmark("src/x.py"), tid="TOP-9101", standing=""))
        self.assertEqual(rc, 0, out)

    def test_a_bad_row_in_a_staged_new_topic_is_refused(self):
        rc, out = self.staged_new(ttext(["src/x.py"], rmark("src/x.py", action="toggle"), tid="TOP-9101", standing=""))
        self.assertEqual(rc, 1, out)
        self.assertIn("action 'toggle'", out)

    def test_the_repair_branch_still_freezes_the_rows(self):
        root = git_store(self.tmp / "repair")
        self.root = root
        broken = ttext(["src/a.py"], rmark("src/a.py")).replace("tags: []", "tags: not-a-list", 1)
        broken = broken.split("links:")[0] + "links: []\n---\n\nBody.\n"
        (root / "topics" / "foo.md").write_text(broken)
        commit_all(root)
        self.assertFalse(memidx.parse_record_text(broken, "x").valid)
        self.assertRefused(ttext(["src/a.py"], rmark("src/a.py", why="EDITED")), "row changed after being recorded")
        self.assertClean(ttext(["src/a.py"], rmark("src/a.py")))


# ---------------------------------------------------------------------------
# type frozen
# ---------------------------------------------------------------------------

class TestTypeFrozen(GuardCase):
    # gate round 12 R12-1: the freeze protects DELIVERED records only. A REF the index QUARANTINED
    # (`memidx.record_is_quarantined`, the reindex predicate) delivered nothing, so any repair of it
    # is a record entering delivery and is free; a stored REF (strict or a tolerated fallback note) is
    # frozen to `infer_type`.
    def _assert_quarantined(self, text, want=True):
        res = memidx.parse_record_text(text, "x")
        self.assertEqual(memidx.record_is_quarantined(res), want, res)

    def test_a_type_change_on_a_recorded_topic_is_refused(self):
        base = ttext(["src/a.py"])
        self.store(base)
        self.assertClean(base)
        self.assertRefused(base.replace("type: topic\n", "type: investigation\n"),
                           "type changed 'topic' -> 'investigation'")

    def test_dropping_the_type_under_topics_infers_topic_and_is_clean(self):
        # gate round 11 R11-1: `type removed` is gone; a typeless NEW under topics/ infers `topic` (delivery unchanged)
        base = ttext(["src/a.py"])
        self.store(base)
        self.assertClean(base.replace("type: topic\n", ""))

    def test_repairing_a_fallback_parsed_record_is_not_a_type_change(self):
        # REF: `title: bad: title` + `type: topic # comment` only parses through the lenient
        # fallback (type recovered as the string 'topic # comment'); quoting the title is a repair
        broken = ttext(["src/a.py"]).replace("type: topic\n", "type: topic # comment\n", 1)
        lines = broken.split("\n")
        for i, ln in enumerate(lines):
            if ln.startswith("title:"):
                lines[i] = "title: bad: title"
        broken = "\n".join(lines)
        res = memidx.parse_record_text(broken, "x")
        self.assertTrue(res.fallback, res)
        self.store(broken)
        self.assertClean(broken.replace("title: bad: title", 'title: "bad: title"'))

    @staticmethod
    def _bad_title(text, type_line=None):
        lines = text.split("\n")
        for i, ln in enumerate(lines):
            if ln.startswith("title:"):
                lines[i] = "title: bad: title"
            if type_line and ln == "type: topic":
                lines[i] = type_line
        return "\n".join(lines)

    def test_a_schema_invalid_but_strict_ref_is_quarantined_so_its_repair_may_declare_any_type(self):
        # REF: valid YAML, `type: topic`, `tags: not-a-list` -> valid=False, fallback=False: the index quarantined it
        # (round 12 R12-1: was "keeps its type frozen")
        broken = ttext(["src/a.py"]).replace("tags: []\n", "tags: not-a-list\n", 1)
        res = memidx.parse_record_text(broken, "x")
        self.assertFalse(res.valid, res)
        self.assertFalse(res.fallback, res)
        self._assert_quarantined(broken)
        self.store(broken)
        repaired = broken.replace("tags: not-a-list\n", "tags: []\n", 1)
        self.assertClean(repaired)
        self.assertClean(repaired.replace("type: topic\n", "type: incident\n", 1))

    def test_a_quarantined_fallback_ref_with_a_clean_known_type_may_declare_another_type(self):
        # round 12 R12-1 (was "keeps it frozen"): the TOP- id makes the fallback REF canonical, so it is quarantined
        broken = self._bad_title(ttext(["src/a.py"]))
        self.assertTrue(memidx.parse_record_text(broken, "x").fallback)
        self._assert_quarantined(broken)
        self.store(broken)
        fixed = broken.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed)
        self.assertClean(fixed.replace("type: topic\n", "type: incident\n", 1))

    def test_a_quarantined_fallback_ref_with_a_garbage_recovered_type_may_declare_any_type(self):
        # round 12 R12-1 (was "frozen through the id prefix and must declare topic"; the "must declare" hint is gone)
        broken = self._bad_title(ttext(["src/a.py"]), type_line="type: banana # note")
        res = memidx.parse_record_text(broken, "x")
        self.assertTrue(res.fallback, res)
        self._assert_quarantined(broken)
        self.store(broken)
        fixed = broken.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace("type: banana # note", "type: topic"))
        self.assertClean(fixed.replace("type: banana # note", "type: incident"))

    def test_a_new_side_quoted_hash_value_is_not_stripped_into_the_old_type(self):
        # gate round 3 R3-1: a STORED strict REF (`topic`); NEW is fallback-parsed with
        # `type: "topic # actual value"` -- no strip may turn that into `topic`
        self.store(ttext(["src/a.py"]))
        # no canonical marker left (no id, links, or bare `type: topic`): a tolerated fallback note
        new = '---\ntype: "topic # actual value"\ntitle: bad: title\n---\n\nBody.\n'
        res = memidx.parse_record_text(new, "x")
        self.assertTrue(res.fallback and res.valid, res)
        self.assertRefused(new, "type changed 'topic' -> 'topic # actual value'")

    def test_a_quarantined_ref_exempts_a_new_side_quoted_hash_value(self):
        # round 12 R12-1 (was the same fixture over a fallback REF frozen to `topic`): the REF was quarantined
        broken = self._bad_title(ttext(["src/a.py"]))
        self._assert_quarantined(broken)
        self.store(broken)
        new = '---\ntype: "topic # actual value"\ntitle: bad: title\n---\n\nBody.\n'
        self.assertClean(new)

    # gate round 4 R4-1 / round 6 R6-1 / round 8 R8-1: the frozen REF value is the engine's kind reading; the NEW decoded type must equal it
    # (gate round 8 R8-1: the REF carries a TOP- id, so the engine's kind reading is `topic` through the id prefix)
    ODD_REF = '---\nid: TOP-0001\ntype: "topic # actual value"\ntitle: bad: title\n---\n\nBody.\n'

    def _odd_ref(self):
        res = memidx.parse_record_text(self.ODD_REF, "x")
        self.assertTrue(res.fallback, res)   # (the id makes the record schema-invalid; the type freeze does not care)
        self.assertEqual(res.frontmatter.get("type"), "topic # actual value")
        self.store(self.ODD_REF)

    def test_a_quarantined_odd_type_ref_is_clean_on_a_title_only_repair(self):
        # round 12 R12-1 (was refused with the normalise message): the TOP- id makes the REF canonical -> quarantined
        self._odd_ref()
        self.assertClean(self.ODD_REF.replace("title: bad: title", 'title: "bad: title"'))

    def test_a_quarantined_odd_type_ref_body_only_edit_is_refused_as_still_malformed_not_as_a_type_change(self):
        # round 12 R12-1 (was refused with the normalise message): the type freeze is silent; the edit is refused
        # only because the record is still malformed
        self._odd_ref()
        out = self.assertRefused(self.ODD_REF.replace("Body.", "Body, edited."), "malformed YAML frontmatter")
        self.assertNotIn("type changed", out)

    def test_an_odd_ref_type_may_become_its_bare_token(self):
        self._odd_ref()
        fixed = self.ODD_REF.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace('type: "topic # actual value"', "type: topic"))

    def test_a_quarantined_odd_ref_type_changed_to_another_type_is_clean(self):
        # round 12 R12-1 (was refused 'topic' -> 'incident'): the record enters delivery as `incident`
        self._odd_ref()
        fixed = self.ODD_REF.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace('type: "topic # actual value"', "type: incident"))

    # gate round 5 R5-1 / round 6 R6-1: a raw fallback spelling is never compared with a decoded
    # strict value; the NEW record's decoded type must equal the engine's kind reading (no source-line rule)
    def _fallback_ref(self, type_line, valid=True, rid="TOP-0001"):
        ref = f"---\nid: {rid}\n{type_line}\ntitle: bad: title\n---\n\nBody.\n"
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(res.fallback, res)
        self._assert_quarantined(ref)   # the TOP-/INC- id makes it canonical, hence quarantined (round 12 R12-1)
        self.store(ref)
        return ref

    def _title_repaired(self, ref):
        return ref.replace("title: bad: title", 'title: "bad: title"')

    def test_a_quarantined_single_quoted_odd_type_with_a_doubled_quote_is_clean(self):
        # round 12 R12-1 (was refused with the normalise message)
        ref = self._fallback_ref("type: 'topic # it''s x'")
        fixed = self._title_repaired(ref)
        self.assertEqual(memidx.parse_record_text(fixed, "x").frontmatter.get("type"), "topic # it's x")
        self.assertClean(fixed)

    def test_a_quarantined_double_quoted_odd_type_is_clean(self):
        # round 12 R12-1 (was refused with the normalise message)
        ref = self._fallback_ref('type: "topic # x"')
        self.assertClean(self._title_repaired(ref))

    def test_a_quarantined_double_quoted_odd_type_with_an_escape_is_clean(self):
        # round 12 R12-1 (was refused with the normalise message)
        ref = self._fallback_ref('type: "topic # \\u0078"')
        self.assertClean(self._title_repaired(ref))

    def test_a_quarantined_odd_type_line_with_changed_spacing_is_clean(self):
        # round 12 R12-1 (was refused with the normalise message)
        ref = self._fallback_ref("type: 'topic # it''s x'")
        self.assertClean(self._title_repaired(ref).replace("type: 'topic", "type:   'topic"))

    def test_a_quarantined_odd_single_quoted_ref_may_become_bare_topic_or_incident(self):
        # round 12 R12-1 (was: topic clean, incident refused)
        ref = self._fallback_ref("type: 'topic # it''s x'")
        fixed = self._title_repaired(ref)
        self.assertClean(fixed.replace("type: 'topic # it''s x'", "type: topic"))
        self.assertClean(fixed.replace("type: 'topic # it''s x'", "type: incident"))

    def test_a_quarantined_odd_type_line_respelled_to_a_different_literal_is_clean(self):
        # round 12 R12-1 (was refused 'topic' -> 'topic # other')
        ref = self._fallback_ref("type: 'topic # it''s x'")
        self.assertClean(self._title_repaired(ref).replace("it''s x", "other"))

    # gate round 5 R5-2: a quoted type followed by a comment is frozen too
    def test_a_quarantined_quoted_type_followed_by_a_comment_may_declare_any_type(self):
        # round 12 R12-1 (was frozen to 'incident' through the INC- id)
        ref = self._fallback_ref('type: "incident" # note', rid="INC-0001")
        self.assertEqual(memidx.parse_record_text(ref, "x").frontmatter.get("type"), 'incident" # note')
        fixed = self._title_repaired(ref)
        self.assertClean(fixed)
        self.assertClean(fixed.replace('type: "incident" # note', "type: concept"))
        self.assertClean(fixed.replace('type: "incident" # note', "type: incident"))

    # gate round 8 R8-1: the frozen kind of a fallback REF is the engine's own `_record_kind` reading
    def test_record_kind_table_for_a_fallback_ref(self):
        # `_record_kind` still serves the deletion messages (check_append_only); the type freeze no longer uses it
        table = [
            ({"type": "incident"}, "incident"),                          # explicit canonical type wins
            ({"type": "topic", "id": "INC-0001"}, "topic"),
            ({"type": 'topic"# note', "id": "TOP-0001"}, "topic"),       # garbled type -> TOP- id
            ({"type": "to\\u0070ic", "id": "TOP-0001"}, "topic"),
            ({"type": "topic # c", "id": "INC-0001"}, "incident"),       # garbled type -> INC- id
            ({"type": "banana", "id": "INV-0001"}, "investigation"),
            ({"type": "banana", "id": "CON-0001"}, "concept"),
            ({"type": "banana", "links": [{"link": "x"}]}, "topic"),     # no id, links -> topic
            ({"type": "banana"}, None),                                  # garbled type, no id, no links
            ({"type": "banana", "id": "ZZZ-1"}, None),
            ({}, None),
        ]
        for fm, want in table:
            with self.subTest(fm=fm):
                self.assertEqual(memlint._record_kind(fm), want)
        # round 12 R12-1: the freeze reads the index's decision instead: a quarantined fallback REF is exempt, a
        # tolerated fallback note keeps its stored type, a strict record its inferred one
        res = memidx.parse_record_text(self.LINKS_REF, "x")
        self.assertTrue(res.fallback, res)
        self.assertNotIn("links", res.frontmatter)
        self._assert_quarantined(self.LINKS_REF)
        self.assertIsNone(self._frozen(res))
        memo = memidx.parse_record_text(self.MEMO_REF, "x")
        self.assertTrue(memo.fallback, memo)
        self.assertIsNone(self._frozen(memo))   # `memo` is not canonical: exempt (round 12 amendment)
        strict = memidx.parse_record_text(ttext(["src/a.py"]), "x")
        self.assertFalse(strict.fallback)
        self.assertEqual(self._frozen(strict), "topic")

    # gate round 9 R9-3 (Grok MAJOR), as amended in round 12: a garbled type with a `links:` list is quarantined
    # by the index, so it is exempt; a `type: memo` note with no links and no id is a stored note, also exempt
    LINKS_REF = ("---\ntype: topic # comment\ntitle: bad: title\nlinks:\n  - link: L1\n    date: 2026-01-01\n"
                 "    decision: x\n---\n\nBody.\n")
    MEMO_REF = "---\ntype: memo\ntitle: bad: title\n---\n\nBody.\n"

    def test_a_quarantined_links_fallback_ref_may_declare_any_type(self):
        # round 12 R12-1 (was: frozen `topic`, "must declare it"): `type: topic # comment` is canonical to the
        # raw scan, so the index quarantined the REF
        self._assert_quarantined(self.LINKS_REF)
        self.store(self.LINKS_REF)
        fixed = self.LINKS_REF.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace("type: topic # comment", "type: incident"))
        self.assertClean(fixed)
        self.assertClean(fixed.replace("type: topic # comment", "type: topic"))

    def test_a_memo_note_with_no_links_and_no_id_has_a_non_canonical_stored_type_that_is_exempt_and_its_repair_is_free(self):
        # round 12 amendment: the index stored `memo`, which no delivery route reads (for-path, digest and search
        # filter on the canonical types), so nothing is frozen and any repair, including entering as a topic, is free
        self._assert_quarantined(self.MEMO_REF, want=False)
        self.store(self.MEMO_REF)
        fixed = self.MEMO_REF.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed)
        self.assertClean(fixed.replace("type: memo", "type: investigation"))
        self.assertClean(fixed.replace("type: memo", "type: topic"))

    # gate round 10 R10-1 (Sol MAJOR): a STRICT REF with no `type:` is not exempt -- its kind is frozen too
    def _typeless(self, text=None):
        text = text or ttext(["src/a.py"])
        out = text.replace("type: topic\n", "", 1)
        self.assertNotEqual(out, text)
        res = memidx.parse_record_text(out, "x")
        self.assertFalse(res.fallback, res)
        self.assertIsNone(res.frontmatter.get("type"))
        return out

    def test_a_strict_typeless_topic_may_not_gain_type_incident(self):
        ref = self._typeless()
        self.store(ref)
        self.assertRefused(ref.replace("title:", "type: incident\ntitle:", 1), "type changed 'topic' -> 'incident'")

    def test_a_strict_typeless_topic_may_gain_type_topic(self):
        ref = self._typeless()
        self.store(ref)
        self.assertClean(ref.replace("title:", "type: topic\ntitle:", 1))

    def test_a_strict_typeless_topic_may_stay_typeless(self):
        ref = self._typeless()
        self.store(ref)
        self.assertClean(ref)

    def test_a_strict_typeless_record_with_no_id_and_no_links_under_topics_is_frozen_as_topic(self):
        ref = "---\ntitle: a note\n---\n\nBody.\n"
        self.assertFalse(memidx.parse_record_text(ref, "x").fallback)
        self.store(ref)
        # gate round 11 R11-1: under topics/ the index delivers it as `topic` (directory inference), so it is frozen
        self.assertRefused(ref.replace("title:", "type: incident\ntitle:", 1), "type changed 'topic' -> 'incident'")
        self.assertClean(ref)

    @staticmethod
    def _frozen(result):
        root = Path("/store")
        return memlint._frozen_type(root, root / "topics" / "foo.md", result)

    def test_the_unified_rule_table(self):
        # round 12 R12-1: the frozen value is infer_type's answer for a stored record, None for a quarantined one
        self.assertEqual(self._frozen(memidx.parse_record_text(ttext(["src/a.py"]), "x")), "topic")
        self.assertEqual(self._frozen(memidx.parse_record_text(self._typeless(), "x")), "topic")
        self.assertIsNone(self._frozen(memidx.parse_record_text(self._bad_title(ttext(["src/a.py"])), "x")))
        self.assertIsNone(self._frozen(memidx.parse_record_text(self.LINKS_REF, "x")))
        self.assertIsNone(self._frozen(memidx.parse_record_text(self.MEMO_REF, "x")))
        inbox = Path("/store") / "inbox" / "n.md"
        self.assertIsNone(memlint._frozen_type(Path("/store"), inbox, memidx.parse_record_text(ttext(["src/a.py"]), "x")))   # `inbox` is not canonical

    # gate round 10 R10-2 (Sol MAJOR, Grok MINOR): the fallback key scan sees quoted and indented keys; it
    # over-includes (a `links:` in a scalar, a nested one), and such a REF is quarantined, so any repair is free
    LINKS_BLOCK = "  - link: L1\n    date: 2026-01-01\n    decision: x\n"

    def _links_guard(self, ref):
        # round 12 R12-1 (was "must declare topic"): every one of these REFs carries
        # `type: topic # c`, canonical to the raw scan, so the index quarantined it and any repair is free
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(res.fallback, res)
        self._assert_quarantined(ref)
        self.store(ref)
        fixed = ref.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace("type: topic # c", "type: incident"))
        self.assertClean(fixed.replace("type: topic # c", "type: topic"))

    def test_a_quarantined_ref_with_a_double_quoted_links_key_may_declare_any_type(self):
        self._links_guard('---\ntype: topic # c\ntitle: bad: title\n"links":\n' + self.LINKS_BLOCK + "---\n\nBody.\n")

    def test_a_quarantined_ref_with_a_single_quoted_links_key_may_declare_any_type(self):
        self._links_guard("---\ntype: topic # c\ntitle: bad: title\n'links':\n" + self.LINKS_BLOCK + "---\n\nBody.\n")

    def test_a_quarantined_uniformly_indented_ref_may_declare_any_type(self):
        ref = ("---\n  type: topic # c\n  title: bad: title\n  links:\n"
               + "".join("  " + ln + "\n" for ln in self.LINKS_BLOCK.splitlines()) + "---\n\nBody.\n")
        self._links_guard(ref)

    def test_a_quarantined_ref_with_a_nested_links_key_may_declare_any_type(self):
        self._links_guard("---\ntype: topic # c\ntitle: bad: title\nmeta:\n  links: x\n---\n\nBody.\n")

    def test_a_memo_with_a_links_line_inside_a_multi_line_scalar_is_quarantined_so_any_repair_is_free(self):
        # round 12 R12-1 (was: frozen `topic` by the fail-closed key scan): the raw scan's column-zero `links:` makes
        # the REF canonical, so the index quarantined it and the repair is a record entering delivery
        memo = '---\ntype: memo\ntitle: "unclosed\nlinks: []\n---\n\nBody.\n'
        res = memidx.parse_record_text(memo, "x")
        self.assertTrue(res.fallback, res)
        self._assert_quarantined(memo)
        self.store(memo)
        fixed = memo.replace('title: "unclosed', 'title: "closed"')
        self.assertClean(fixed)
        self.assertClean(fixed.replace("type: memo", "type: topic"))

    # gate round 11 R11-3 (Sol MINOR): the quotes around a scanned key must MATCH
    def test_mismatched_quotes_around_a_key_do_not_make_the_ref_canonical(self):
        # round 12 R12-1: only a bare column-zero `links:` makes the memo canonical
        # (quarantined, any repair free); every other spelling is a stored `memo` note, frozen as such
        for key, quarantined in (("links'", False), ('"links"', False), ("'links'", False), ('"links', False), ("links", True)):
            with self.subTest(key=key):
                ref = f"---\ntype: memo\ntitle: bad: title\n{key}: x\n---\n\nBody.\n"
                res = memidx.parse_record_text(ref, "x")
                self.assertTrue(res.fallback, res)
                self.assertEqual(memidx.record_is_quarantined(res), quarantined, res)

    def test_a_memo_with_a_links_quote_key_is_exempt_so_its_title_repair_is_clean(self):
        memo = "---\ntype: memo\ntitle: bad: title\nlinks': x\n---\n\nBody.\n"
        self.store(memo)
        self.assertClean(memo.replace("title: bad: title", 'title: "bad: title"'))

    # gate round 8 R8-1 (Sol MAJOR/MINOR, Grok): spellings of a canonical type that the fallback
    # garbles all freeze through the id prefix (or the links list), never through a string reading.
    # (A cleanly quoted `type: "topic"` is recovered as the token `topic`, which freezes it directly; the id
    # prefix is what freezes the escaped, tagged and block-scalar spellings the fallback cannot decode.)
    def _kind_refused_and_declared(self, type_line, kind="topic", rid="TOP-0001"):
        # round 12 R12-1 (was: other type refused, kind clean): the REF is quarantined, so both are clean
        ref = self._fallback_ref(type_line, rid=rid)
        fixed = self._title_repaired(ref)
        other = "investigation" if kind != "investigation" else "concept"
        self.assertClean(fixed.replace(type_line, f"type: {other}"))
        self.assertClean(fixed.replace(type_line, f"type: {kind}"))

    def test_a_quarantined_double_quoted_canonical_type_may_declare_any_type(self):
        self._kind_refused_and_declared('type: "topic"')

    def test_a_quarantined_escaped_canonical_type_may_declare_any_type(self):
        # Sol's round 8 MAJOR: "to\u0070ic" and "\x74opic" decode to `topic`
        self._kind_refused_and_declared('type: "to\\u0070ic"')
        self._kind_refused_and_declared('type: "\\x74opic"')

    def test_a_quarantined_tag_prefixed_canonical_type_may_declare_any_type(self):
        self._kind_refused_and_declared('type: !!str "topic"')
        self._kind_refused_and_declared("type: !!str 'topic'")

    def test_a_quarantined_block_scalar_canonical_type_may_declare_any_type(self):
        self._kind_refused_and_declared("type: >-\n  topic")
        self._kind_refused_and_declared("type: |\n  topic")

    def test_a_quarantined_unquoted_quote_comment_type_is_clean_on_a_title_repair(self):
        # round 12 R12-1 (was refused with the normalise message): the TOP- id makes the REF canonical
        ref = self._fallback_ref('type: topic"# note')
        fixed = self._title_repaired(ref)
        self.assertClean(fixed)
        self.assertClean(fixed.replace('type: topic"# note', "type: topic"))

    def test_a_quarantined_garbled_type_with_an_inc_id_may_declare_any_type(self):
        # round 12 R12-1 (was frozen `incident`, "must declare type: incident"); a typeless repair is clean too
        ref = self._fallback_ref("type: banana # note", rid="INC-0001")
        fixed = self._title_repaired(ref)
        self.assertClean(fixed.replace("type: banana # note", "type: topic"))
        self.assertClean(fixed.replace("type: banana # note", ""))
        self.assertClean(fixed.replace("type: banana # note", "type: incident"))

    def test_a_garbled_type_note_with_no_id_and_no_links_has_a_non_canonical_stored_type_that_is_exempt_and_its_repair_is_free(self):
        # round 12 amendment (was "frozen as its stored type"): `banana # note` is not canonical, so it is exempt
        ref = "---\ntype: banana # note\ntitle: bad: title\n---\n\nBody.\n"
        self._assert_quarantined(ref, want=False)
        self.store(ref)
        fixed = self._title_repaired(ref)
        self.assertClean(fixed)
        self.assertClean(fixed.replace("type: banana # note", "type: investigation"))
        self.assertClean(ref)

    def test_a_quarantined_no_space_hash_after_a_quoted_canonical_type_may_declare_any_type(self):
        # round 12 R12-1 (was frozen 'topic'): PyYAML reads `"topic"# note` as `topic`; the REF was quarantined
        ref = self._fallback_ref('type: "topic"# note')
        self.assertEqual(memidx.parse_record_text(ref, "x").frontmatter.get("type"), 'topic"# note')
        fixed = self._title_repaired(ref)
        self.assertEqual(memidx.parse_record_text(fixed, "x").frontmatter.get("type"), "topic")
        self.assertClean(fixed)
        self.assertClean(fixed.replace('type: "topic"# note', "type: investigation"))

    def test_a_no_space_hash_repair_keeping_type_topic_is_clean(self):
        ref = self._fallback_ref('type: "topic"# note')
        self.assertClean(self._title_repaired(ref).replace('type: "topic"# note', "type: topic"))

    # gate round 6 R6-1: the effective key is the YAML-decoded one, never a scanned source line
    def test_a_duplicate_type_key_that_wins_last_is_clean_for_a_quarantined_ref(self):
        # round 12 R12-1 (was refused 'topic' -> 'incident'): the REF was quarantined; the record enters as `incident`
        ref = self._fallback_ref("type: topic", valid=False)
        fixed = self._title_repaired(ref).replace("type: topic\n", "type: topic\ntype: incident\n", 1)
        self.assertEqual(memidx.parse_record_text(fixed, "x").frontmatter.get("type"), "incident")
        self.assertClean(fixed)

    def test_a_block_scalar_decoy_type_line_ref_is_quarantined_so_a_real_type_change_is_free(self):
        # round 12 R12-1 (was refused 'topic' -> 'incident'): the column-zero `type: topic` makes the REF canonical
        ref = "---\nnote: |\n  type: topic\ntype: topic\ntitle: bad: title\n---\n\nBody.\n"
        self.assertTrue(memidx.parse_record_text(ref, "x").fallback)
        self._assert_quarantined(ref)
        self.store(ref)
        fixed = self._title_repaired(ref)
        self.assertClean(fixed)
        self.assertClean(fixed.replace("\ntype: topic\ntitle", "\ntype: incident\ntitle"))

    def test_a_quarantined_ref_with_a_nested_meta_type_may_declare_any_type(self):
        # round 12 R12-1 (was frozen `topic`, "must declare"): the TOP- id makes the REF canonical -> quarantined
        ref = self._fallback_ref("meta:\n  type: unrelated\ntype: 'topic # it''s x'")
        fixed = self._title_repaired(ref)
        self.assertClean(fixed)
        self.assertClean(fixed.replace("type: 'topic # it''s x'", "type: topic"))
        self.assertClean(fixed.replace("type: 'topic # it''s x'", "type: incident"))

    def test_an_empty_new_mapping_under_topics_is_not_a_type_change(self):
        # gate round 3 R3-2: the type may not disappear by emptying the frontmatter
        base = ttext(["src/a.py"])
        self.store(base)
        empty = "---\n{}\n---\n\nBody.\n"
        self.assertEqual(memidx.parse_record_text(empty, "x").frontmatter, {})
        # gate round 11 R11-1: no `type removed` case; `{}` under topics/ infers `topic`, the links guard refuses the loss
        out = self.assertRefused(empty, "link removed after being recorded")
        self.assertNotIn("type changed", out)

    def test_an_empty_new_mapping_under_topics_keeps_a_fallback_refs_topic(self):
        broken = self._bad_title(ttext(["src/a.py"]))
        self.assertTrue(memidx.parse_record_text(broken, "x").fallback)
        self.store(broken)
        # gate round 11 R11-1: `{}` under topics/ infers `topic` = the frozen kind; the repair branch has no
        # recorded links to protect, so the type guard is clean
        self.assertClean("---\n{}\n---\n\nBody.\n")

    def test_a_new_side_with_other_keys_but_no_type_is_not_a_type_change(self):
        self.store(ttext(["src/a.py"]))
        out = self.assertRefused("---\ntitle: x\n---\n\nBody.\n", "link removed after being recorded")
        self.assertNotIn("type changed", out)   # gate round 11 R11-1: infers `topic`

    def test_a_strict_type_change_is_still_refused_with_a_comment_free_ref(self):
        base = ttext(["src/a.py"])
        self.assertTrue(memidx.parse_record_text(base, "x").valid)
        self.store(base)
        self.assertRefused(base.replace("type: topic\n", "type: decision\n"), "type changed")

    def test_the_same_type_value_with_different_quoting_is_clean(self):
        base = ttext(["src/a.py"])
        self.store(base)
        self.assertClean(base.replace("type: topic\n", 'type: "topic"\n'))

    def test_an_added_file_is_free_to_have_any_type(self):
        self.store(ttext(["src/a.py"]))
        rc, out = self.guard("---\ntype: incident\nid: INC-9001\ntitle: x\n---\n\nBody.\n", rel="inc.md")
        self.assertEqual(rc, 0, out)

    def test_a_non_topic_record_is_not_guarded(self):
        # amended in gate round 10 R10-1: a strict REF with no `type` is no longer exempt when it has a
        # kind (here the INC- id); it may stay typeless or declare `incident`, nothing else
        note = "---\nid: INC-9001\ntitle: x\n---\n\nBody.\n"
        self.store(note)
        # amended in gate round 11 R11-1: the record sits under topics/, so the index delivers it as `topic`
        # (directory inference) and that is what is frozen; declaring `incident` is a delivery change
        self.assertRefused(note.replace("id: INC-9001\n", "id: INC-9001\ntype: investigation\n"),
                           "type changed 'topic' -> 'investigation'")
        self.assertRefused(note.replace("id: INC-9001\n", "id: INC-9001\ntype: incident\n"),
                           "type changed 'topic' -> 'incident'")
        self.assertClean(note.replace("id: INC-9001\n", "id: INC-9001\ntype: topic\n"))
        self.assertClean(note)

    def _non_topic_ref(self, kind):
        base = ttext(["src/a.py"]).replace("type: topic\n", f"type: {kind}\n", 1)
        broken = base.replace("tags: []\n", "tags: not-a-list\n", 1)
        res = memidx.parse_record_text(broken, "x")
        self.assertFalse(res.valid, res)
        self.assertFalse(res.fallback, res)
        self.store(broken)
        return broken

    def test_a_quarantined_incident_ref_may_declare_another_type_on_repair(self):
        # round 12 R12-1 (was refused 'incident' -> 'investigation'): schema-invalid strict -> quarantined
        broken = self._non_topic_ref("incident")
        repaired = broken.replace("tags: not-a-list\n", "tags: []\n", 1)
        self.assertClean(repaired)
        self.assertClean(repaired.replace("type: incident\n", "type: investigation\n", 1))

    def test_a_quarantined_concept_ref_may_declare_another_type_on_repair(self):
        # round 12 R12-1 (was refused 'concept' -> 'investigation')
        broken = self._non_topic_ref("concept")
        repaired = broken.replace("tags: not-a-list\n", "tags: []\n", 1)
        self.assertClean(repaired)
        self.assertClean(repaired.replace("type: concept\n", "type: investigation\n", 1))

    def test_a_typeless_note_under_topics_is_frozen_as_topic(self):
        # amended in gate round 11 R11-1 (was: free to gain a type): directory inference delivers it as `topic`
        note = "---\ntitle: just a note\n---\n\nBody.\n"
        self.store(note)
        self.assertRefused(note.replace("title: just a note\n", "title: just a note\ntype: incident\n"),
                           "type changed 'topic' -> 'incident'")

    def test_a_modified_non_topic_record_reads_and_parses_its_new_blob_once(self):
        # gate round 4 R4-2
        note = "---\ntitle: x\ntype: incident\nid: INC-9001\n---\n\nBody.\n"
        self.store(note)
        (self.root / "topics" / "foo.md").write_text(note.replace("Body.", "Body, edited."))
        # (the staged run also parses the index blob once for the rejections lint, a separate
        # pre-existing feature; the worktree run isolates the append-only comparison)
        seen = []
        real = memlint._parse_git_blob

        def spy(blob, label):
            seen.append(label)
            return real(blob, label)

        with mock.patch.object(memlint, "_parse_git_blob", spy):
            memlint.check_append_only(self.root, "HEAD", False)
        # every call counts whatever its label type; the NEW-side comparison parse is the one
        # whose label is the store-relative path string, the REF parse says "(at HEAD)"
        ref_side = [l for l in seen if "(at " in str(l)]
        new_side = [l for l in seen if "(at " not in str(l)]
        self.assertEqual(len(ref_side), 1, seen)
        self.assertEqual(len([l for l in new_side if isinstance(l, str)]), 1, seen)
        self.assertLessEqual(len(new_side), 2, seen)  # + the pre-existing rejections scan (a Path label)


class TestNonScalarTypeUnderAgainstRef(GuardCase):
    """TOP-0137 L29 (gate round 1, R1-1): a list `type` is a shape error; the append-only
    guard must judge it (added, repaired, deleted) and never raise on the unhashable value."""

    LIST_TYPE = "type: [topic, weird]\n"

    def _listy(self, **kw):
        return ttext([], **kw).replace("type: topic\n", self.LIST_TYPE, 1)

    def test_added_record_with_a_list_type_is_a_shape_error_not_a_traceback(self):
        root = self.store(ttext([]))
        (root / "topics" / "new.md").write_text(self._listy(tid="TOP-9102"))
        git(["add", "-A"], root)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(root)])
        self.assertNotIn("Traceback", out)
        self.assertNotIn("unhashable", out)
        self.assertEqual(rc, 1, out)
        self.assertIn("type must be a scalar string", out)

    def test_repairing_a_list_type_to_a_scalar_enters_delivery_and_is_clean(self):
        self.store(self._listy())
        self.assertClean(ttext([]))

    # R1-2: a REF valid under the OLD rules (type: topic, an ignored non-string metadata.type) was
    # delivered as `topic`; the string rule now quarantines it, which must not release its freeze.
    @staticmethod
    def _bad_metadata(text):
        return text.replace("tags: []\n", "tags: []\nmetadata:\n  type: [x]\n", 1)

    def test_a_ref_quarantined_only_by_the_new_metadata_type_rule_keeps_its_type_frozen(self):
        ref = self._bad_metadata(ttext([]))
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(memidx.record_is_quarantined(res), res)
        self.store(ref)
        self.assertRefused(ttext([]).replace("type: topic\n", "type: incident\n", 1),
                           "type changed 'topic' -> 'incident'")

    def test_repairing_that_metadata_alone_is_clean(self):
        self.store(self._bad_metadata(ttext([])))
        self.assertClean(ttext([]))

    def test_a_list_type_at_ref_is_quarantined_at_ref_so_its_repair_may_declare_any_type(self):
        self.store(self._listy())
        self.assertClean(ttext([]).replace("type: topic\n", "type: incident\n", 1))

    def test_deleting_a_non_topic_record_that_carried_a_list_type_delivered_nothing_and_is_clean(self):
        # an incident-like record (INC- id, no links): quarantined at REF, deletable like any non-topic
        text = "---\nid: INC-9103\ntype: [incident, weird]\ntitle: Odd\n---\n\nBody.\n"
        root = self.store(text)
        (root / "topics" / "foo.md").unlink()
        rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
        self.assertNotIn("Traceback", out)
        self.assertEqual(rc, 0, out)

    def test_deleting_a_topic_like_record_with_a_list_type_is_refused_by_the_topic_rule_without_a_traceback(self):
        root = self.store(self._listy())
        (root / "topics" / "foo.md").unlink()
        rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
        self.assertNotIn("Traceback", out)
        self.assertEqual(rc, 1, out)
        self.assertIn("file deleted or renamed", out)


# ---------------------------------------------------------------------------
# gate round 11 R11-2: rows new since REF meet the complete anchor validator
# ---------------------------------------------------------------------------

class TestNewAnchorRowsMeetTheFullValidator(GuardCase):
    def test_an_added_topic_with_a_prefix_anchor_is_refused(self):
        self.store(ttext(["src/a.py"]))
        text = binding_topic([good_row(ref="src/")], code_refs=("src/",),
                             marks_text="ref_marks:\n" + rmark("src/"))
        (self.root / "topics" / "bar.md").write_text(text)
        git(["add", "-A"], self.root)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        self.assertEqual(rc, 1, out)
        self.assertIn(memlint._ANCHOR_PREFIX_GLOB_MESSAGE, out)

    def test_an_added_topic_with_a_bad_digest_anchor_is_refused_on_the_worktree_and_staged_paths(self):
        self.store(ttext(["src/a.py"]))
        text = binding_topic([good_row(digest='"abc"')],
                             marks_text="ref_marks:\n" + rmark("src/foo.py"))
        (self.root / "topics" / "bar.md").write_text(text)
        git(["add", "-A"], self.root)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        self.assertEqual(rc, 1, out)
        self.assertIn("digest must be 64 lowercase hex characters", out)

    def test_a_modified_topic_whose_new_row_has_a_bad_digest_is_refused(self):
        refs = ("src/foo.py", "src/bar.py")
        self.store(binding_topic([good_row()], code_refs=refs))
        self.assertRefused(binding_topic([good_row(ref="src/bar.py", digest='"abc"'), good_row()], code_refs=refs),
                           "digest must be 64 lowercase hex characters")
        self.assertRefused(binding_topic([good_row(ref="src/bar.py", link="L9"), good_row()], code_refs=refs),
                           "link 'L9' is not a link in this topic")
        self.assertRefused(binding_topic([good_row(ref="src/nowhere.py"), good_row()], code_refs=refs),
                           "ref is not present verbatim in this topic's code_refs")
        self.assertClean(binding_topic([good_row(ref="src/bar.py"), good_row()], code_refs=refs))

    def test_a_grandfathered_row_is_freeze_only_even_when_it_would_fail_plain_lint(self):
        refs = ("src/foo.py",)
        old = binding_topic([good_row(digest='"abc"')], code_refs=refs)
        self.store(old)
        self.assertClean(old.replace("Body.", "Body, edited."))


# ---------------------------------------------------------------------------
# gate round 11 R11-1: the frozen type is what the index delivers (`infer_type`)
# ---------------------------------------------------------------------------

class TestDeliveredTypeFrozen(GuardCase):
    def _typeless(self, tid="TOP-9100"):
        text = ttext(["src/a.py"], tid=tid)
        out = text.replace("type: topic\n", "", 1)
        res = memidx.parse_record_text(out, "x")
        self.assertFalse(res.fallback, res)
        self.assertIsNone(res.frontmatter.get("type"))
        return out

    def test_metadata_type_on_a_typeless_topic_is_refused(self):
        # Sol R11 #1: `metadata.type` is what infer_type reads next
        ref = self._typeless()
        self.store(ref)
        self.assertRefused(ref.replace("tags: []\n", "tags: []\nmetadata:\n  type: incident\n", 1),
                           "type changed 'topic' -> 'incident'")

    def test_a_metadata_type_change_on_a_typeless_record_is_refused(self):
        ref = self._typeless().replace("tags: []\n", "tags: []\nmetadata:\n  type: concept\n", 1)
        self.store(ref)
        self.assertEqual(memidx.infer_type(Path("/s"), Path("/s/topics/foo.md"),
                                           memidx.parse_record_text(ref, "x").frontmatter), "concept")
        self.assertRefused(ref.replace("type: concept", "type: investigation"),
                           "type changed 'concept' -> 'investigation'")

    def test_a_metadata_type_change_on_a_record_with_an_explicit_type_is_not_a_delivery_change(self):
        # the explicit `type:` wins in infer_type, so the delivered type is the same and the edit is clean
        ref = ttext(["src/a.py"]).replace("tags: []\n", "tags: []\nmetadata:\n  type: concept\n", 1)
        self.store(ref)
        self.assertClean(ref.replace("type: concept", "type: investigation"))

    def test_a_typeless_inc_record_under_topics_may_not_declare_incident(self):
        # Sol R11 #2: the index delivered it as `topic` (directory), declaring `incident` drops it from for-path
        ref = self._typeless(tid="INC-9100")
        self.store(ref)
        self.assertRefused(ref.replace("title:", "type: incident\ntitle:", 1), "type changed 'topic' -> 'incident'")
        self.assertClean(ref.replace("title:", "type: topic\ntitle:", 1))
        self.assertClean(ref)

    def test_type_false_to_the_string_false_is_refused(self):
        # Sol R11 #3: YAML false is not a type (infer_type falls through to the directory); "False" is one
        ref = ttext(["src/a.py"]).replace("type: topic\n", "type: false\n", 1)
        self.store(ref)
        self.assertRefused(ref.replace("type: false\n", 'type: "False"\n', 1), "type changed 'topic' -> 'False'")
        self.assertClean(ref)

    def test_a_typeless_inc_record_under_incidents_may_declare_incident(self):
        text = self._typeless(tid="INC-9100")
        root = git_store(self.tmp / "inc")
        self.root = root
        (root / "incidents").mkdir()
        (root / "incidents" / "x.md").write_text(text)
        commit_all(root)
        (root / "incidents" / "x.md").write_text(text.replace("title:", "type: incident\ntitle:", 1))
        rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
        self.assertEqual(rc, 0, out)
        (root / "incidents" / "x.md").write_text(text.replace("title:", "type: topic\ntitle:", 1))
        rc, out = run_memlint(["--against-ref", "HEAD", str(root)])
        self.assertEqual(rc, 1, out)
        self.assertIn("type changed 'incident' -> 'topic'", out)

    INDENTED_INC = ("---\n  type: incident\n  id: INC-0001\n  title: bad: title\n  links:\n"
                    "    - link: L1\n      date: 2026-01-01\n      decision: x\n---\n\nBody.\n")

    def test_a_quarantined_uniformly_indented_incident_under_topics_is_repaired_to_incident_or_topic(self):
        # Grok R12 MAJOR 2 (was: frozen `topic`, "must declare type: topic"): the index quarantined it (for-path 0,
        # digest 0), so either repair is a record entering delivery; the misfiling is a human matter
        res = memidx.parse_record_text(self.INDENTED_INC, "x")
        self.assertTrue(res.fallback, res)
        self.assertNotIn("type", res.frontmatter)
        self.assertTrue(memidx.record_is_quarantined(res), res)
        self.store(self.INDENTED_INC)
        fixed = self.INDENTED_INC.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed)
        self.assertClean(fixed.replace("type: incident", "type: topic"))

    # gate round 12 R12-1 / R12-2: fixtures for the "delivered records only" rule
    def _inbox_store(self, text):
        root = git_store(self.tmp / "inbox_store")
        self.root = root
        (root / "inbox").mkdir()
        (root / "inbox" / "n.md").write_text(text)
        commit_all(root)
        return root

    def _inbox_guard(self, text):
        (self.root / "inbox" / "n.md").write_text(text)
        return run_memlint(["--against-ref", "HEAD", str(self.root)])

    def test_a_stored_inbox_memo_is_exempt_and_its_title_repair_is_clean(self):
        # Sol R12 MAJOR (R12-2): the index delivers every file under inbox/ as `inbox` whatever its `type:`; the freeze
        # is `infer_type`, so a title repair (and even a `type:` edit) leaves it `inbox` and is clean. On c58a569 the
        # kind rule froze `topic` for an inbox note with a `links` key and refused the repair.
        ref = "---\ntype: memo\ntitle: bad: title\nmeta:\n  links: x\n---\n\nBody.\n"
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(res.fallback, res)
        self.assertFalse(memidx.record_is_quarantined(res), res)
        self.assertIsNone(memlint._frozen_type(Path("/s"), Path("/s/inbox/n.md"), res))   # `inbox` is not canonical: exempt
        self._inbox_store(ref)
        fixed = ref.replace("title: bad: title", 'title: "bad: title"')
        rc, out = self._inbox_guard(fixed)
        self.assertEqual(rc, 0, out)
        rc, out = self._inbox_guard(fixed.replace("type: memo", "type: incident"))
        self.assertEqual(rc, 0, out)

    def test_a_quarantined_inbox_memo_with_a_links_key_has_a_clean_title_repair(self):
        # Sol R12 MAJOR (R12-2), the exact shape: `type: memo` with a column-zero `links:` is canonical to the raw scan,
        # so the index quarantined it; the repair enters delivery as `inbox`
        ref = "---\ntype: memo\ntitle: bad: title\nlinks:\n  - link: L1\n    date: 2026-01-01\n    decision: x\n---\n\nBody.\n"
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(memidx.record_is_quarantined(res), res)
        self._inbox_store(ref)
        rc, out = self._inbox_guard(ref.replace("title: bad: title", 'title: "bad: title"'))
        self.assertEqual(rc, 0, out)

    def test_a_note_with_a_non_mapping_metadata_is_stored_by_directory_and_stays_frozen(self):
        # R12-1 (infer_type-raises mirror, Grok MINOR 1): the parser DROPS a note's wrongly-shaped `metadata` before
        # the index calls `infer_type`, so the index stored `topic` (directory); a later `metadata.type: incident`
        # is a real delivery change and is refused
        ref = "---\ntitle: a note\nmetadata: not-a-map\n---\n\nBody.\n"
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(res.valid, res)
        self.assertNotIn("metadata", res.frontmatter)
        self.assertEqual(self._frozen_of(res), "topic")
        self.store(ref)
        self.assertRefused(ref.replace("metadata: not-a-map\n", "metadata:\n  type: incident\n"),
                           "type changed 'topic' -> 'incident'")
        self.assertClean(ref.replace("metadata: not-a-map\n", ""))

    def test_a_canonical_record_with_a_non_mapping_metadata_is_quarantined_so_a_metadata_type_repair_is_clean(self):
        # R12-1 (infer_type-raises mirror): the parser keeps a CANONICAL record's bad `metadata` and quarantines it, so
        # the index never calls `infer_type` on it (which would raise) and stored no type: exempt, like any quarantined REF
        ref = self._typeless().replace("tags: []\n", "tags: []\nmetadata: not-a-map\n", 1)
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(memidx.record_is_quarantined(res), res)
        with self.assertRaises(AttributeError):
            memidx.infer_type(Path("/s"), Path("/s/topics/foo.md"), res.frontmatter)
        self.assertIsNone(self._frozen_of(res))
        self.store(ref)
        self.assertClean(ref.replace("metadata: not-a-map\n", "metadata:\n  type: incident\n"))
        self.assertClean(ref.replace("metadata: not-a-map\n", ""))

    # gate round 13 R13-1 (Sol MAJOR): a delivered record may not become quarantined
    SOL_REF = "---\ntitle: a note\nmetadata: not-a-map\ncode_refs:\n  - src/a.py\n---\n\nBody.\n"

    def test_a_delivered_record_may_not_become_quarantined_by_declaring_a_canonical_type(self):
        # Sol R13: REF is stored as `topic` (the parser drops the bad `metadata` of a non-canonical note, for-path
        # sees src/a.py); adding `type: topic` makes NEW canonical-looking, the index quarantines it (for-path 1 -> 0),
        # yet both inferred types are `topic`. Worktree and staged paths agree (GuardCase.guard runs both)
        ref = self.SOL_REF
        self.assertFalse(memidx.record_is_quarantined(memidx.parse_record_text(ref, "x")))
        self.assertEqual(self._frozen_of(memidx.parse_record_text(ref, "x")), "topic")
        self.store(ref)
        new = ref.replace("metadata: not-a-map\n", "type: topic\nmetadata: not-a-map\n")
        self.assertTrue(memidx.record_is_quarantined(memidx.parse_record_text(new, "x")))
        out = self.assertRefused(new, "record leaves delivery: the new side would be quarantined (")
        self.assertNotIn("type changed", out)
        self.assertIn("metadata must be a mapping", out)

    def test_the_same_edit_that_also_fixes_metadata_is_clean(self):
        ref = self.SOL_REF
        self.store(ref)
        new = ref.replace("metadata: not-a-map\n", "type: topic\n")
        self.assertFalse(memidx.record_is_quarantined(memidx.parse_record_text(new, "x")))
        self.assertClean(new)

    def test_a_quarantined_ref_stays_exempt_from_the_new_side_quarantine_rule(self):
        # entering delivery is free; a still-quarantined NEW of a quarantined REF is no new loss
        ref = self.SOL_REF.replace("metadata:", "type: topic\nmetadata:", 1)
        self.assertTrue(memidx.record_is_quarantined(memidx.parse_record_text(ref, "x")))
        self.store(ref)
        out = self.assertRefused(ref.replace("Body.", "Body edited."), "metadata must be a mapping")  # the existing invalid-new refusal only
        self.assertNotIn("record leaves delivery", out)
        self.assertClean(ref.replace("metadata: not-a-map\n", ""))

    def test_a_type_change_is_reported_before_the_quarantine(self):
        ref = ttext(["src/a.py"])
        self.store(ref)
        out = self.assertRefused(ref.replace("type: topic\n", "type: incident\n", 1), "type changed 'topic' -> 'incident'")
        self.assertNotIn("record leaves delivery", out)   # NEW is a valid incident
        both = ref.replace("type: topic\n", "type: incident\nmetadata: not-a-map\n", 1)
        out = self.assertRefused(both, "type changed 'topic' -> 'incident'", "record leaves delivery")
        self.assertLess(out.index("type changed"), out.index("record leaves delivery"))

    @staticmethod
    def _frozen_of(result):
        root = Path("/store")
        return memlint._frozen_type(root, root / "topics" / "foo.md", result)

    GARBLED_WITH_REFS = ("---\n{ident}type: banana # note\ntitle: bad: title\ncode_refs:\n  - src/a.py\n---\n\nBody.\n")

    def test_a_quarantined_garbled_record_with_code_refs_may_enter_delivery_as_a_topic_on_repair(self):
        # Grok R12 MINOR 2 (R12-1): the TOP- id makes the garbled REF canonical, so the index quarantined it (no type,
        # for-path 0); declaring `type: topic` on repair puts its code_refs into for-path. Allowed by the rule: the
        # record ENTERS delivery exactly like an added file (documented in INTERNALS)
        ref = self.GARBLED_WITH_REFS.format(ident="id: TOP-0001\n")
        self.assertTrue(memidx.record_is_quarantined(memidx.parse_record_text(ref, "x")))
        self.store(ref)
        fixed = ref.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace("type: banana # note", "type: topic"))

    def test_a_garbled_note_with_code_refs_and_no_canonical_marker_may_enter_for_path_on_repair(self):
        # Grok R12 MINOR 2, round 12 amendment: not quarantined, but its stored `banana # note` is not canonical, hence
        # delivered by no route (for-path 0); repairing it to `type: topic` puts its code_refs into for-path. Entering
        # delivery is free, like an added file (documented in INTERNALS)
        ref = self.GARBLED_WITH_REFS.format(ident="")
        self.assertFalse(memidx.record_is_quarantined(memidx.parse_record_text(ref, "x")))
        self.store(ref)
        fixed = ref.replace("title: bad: title", 'title: "bad: title"')
        self.assertClean(fixed.replace("type: banana # note", "type: topic"))

    def test_a_fallback_record_that_never_had_a_type_is_clean_when_repaired_without_one(self):
        # Grok R11 MINOR: no `explicit` flag, no `type removed`; a typeless NEW infers by directory
        ref = ("---\nid: TOP-0001\ntitle: bad: title\nlinks:\n  - link: L1\n    date: 2026-01-01\n"
               "    decision: x\n---\n\nBody.\n")
        res = memidx.parse_record_text(ref, "x")
        self.assertTrue(res.fallback, res)
        self.assertNotIn("type", res.frontmatter)
        self.store(ref)
        self.assertClean(ref.replace("title: bad: title", 'title: "bad: title"'))


# ---------------------------------------------------------------------------
# one block-item deletion shared by unbind and mark-standing
# ---------------------------------------------------------------------------

class TestReferenceAuditCoversEveryDeliveredRecord(GuardCase):
    """gate round 14 R14-1 (Sol MAJOR, Grok MAJOR 1): the `code_refs` pairing and the `ref_marks`
    freeze run for every record the index DELIVERED at REF with a canonical type, not only for
    topic-like ones. A typeless `topics/` note, a `links: []` note and a `TOP-` id with no links are
    on `for-path` through `code_refs` (stored type `topic`) but have no link chain to freeze."""
    NOTE = "---\ntitle: a note\ncode_refs:\n  - src/a.py\n---\n\nBody.\n"
    EMPTY_LINKS = "---\ntitle: a note\nlinks: []\ncode_refs:\n  - src/a.py\n---\n\nBody.\n"
    TOP_NO_LINKS = "---\nid: TOP-9200\ntitle: a note\ncode_refs:\n  - src/a.py\n---\n\nBody.\n"

    def _delivered_as_topic_without_a_chain(self, text):
        res = memidx.parse_record_text(text, "x")
        self.assertTrue(res.valid and not res.fallback, res)
        self.assertFalse(memlint._is_topic_like(res), "fixture must be a record the link gate skips")
        self.assertEqual(memlint._frozen_type(Path("/s"), Path("/s/topics/foo.md"), res), "topic")

    def test_the_fixtures_are_delivered_topics_that_the_link_gate_skips(self):
        for text in (self.NOTE, self.EMPTY_LINKS, self.TOP_NO_LINKS):
            self._delivered_as_topic_without_a_chain(text)

    def test_clearing_code_refs_of_a_typeless_note_without_a_row_is_refused(self):
        self.store(self.NOTE)
        self.assertRefused(self.NOTE.replace("code_refs:\n  - src/a.py\n", "code_refs: []\n"),
                           "code_refs changed for src/a.py (remove) with no matching ref_marks row")

    def test_dropping_the_code_refs_key_is_refused(self):
        self.store(self.NOTE)
        self.assertRefused(self.NOTE.replace("code_refs:\n  - src/a.py\n", ""),
                           "code_refs changed for src/a.py (remove)")

    def test_clearing_code_refs_with_a_remove_row_is_clean(self):
        self.store(self.NOTE)
        self.assertClean("---\ntitle: a note\ncode_refs: []\nref_marks:\n" + rmark("src/a.py", "remove")
                         + "---\n\nBody.\n")

    def test_replacing_the_frontmatter_with_an_empty_mapping_is_refused(self):
        self.store(self.NOTE)
        self.assertRefused("---\n{}\n---\n\nBody.\n", "code_refs changed for src/a.py (remove)")

    def test_gaining_a_ref_with_an_add_row_is_clean_and_without_one_refused(self):
        self.store(self.NOTE)
        grown = self.NOTE.replace("  - src/a.py\n", "  - src/a.py\n  - src/b.py\n")
        self.assertRefused(grown, "code_refs changed for src/b.py (add) with no matching ref_marks row")
        self.assertClean(grown.replace("---\n\nBody.", "ref_marks:\n" + rmark("src/b.py") + "---\n\nBody."))

    def test_retargeting_a_ref_needs_a_remove_and_an_add_row(self):
        # Grok variant: src/a.py -> src/z.py
        self.store(self.NOTE)
        moved = self.NOTE.replace("src/a.py", "src/z.py")
        self.assertRefused(moved, "src/a.py (remove)", "src/z.py (add)")
        self.assertClean(moved.replace("---\n\nBody.", "ref_marks:\n" + rmark("src/z.py", "add", LATER)
                                       + rmark("src/a.py", "remove") + "---\n\nBody."))

    def test_a_links_empty_list_note_is_audited(self):
        self.store(self.EMPTY_LINKS)
        self.assertRefused(self.EMPTY_LINKS.replace("code_refs:\n  - src/a.py\n", "code_refs: []\n"),
                           "code_refs changed for src/a.py (remove)")
        self.assertRefused(self.EMPTY_LINKS.replace("src/a.py", "src/z.py"), "src/a.py (remove)")

    def test_a_top_id_without_links_is_audited(self):
        self.store(self.TOP_NO_LINKS)
        self.assertRefused(self.TOP_NO_LINKS.replace("code_refs:\n  - src/a.py\n", "code_refs: []\n"),
                           "code_refs changed for src/a.py (remove)")
        self.assertRefused("---\n{}\n---\n\nBody.\n", "code_refs changed for src/a.py (remove)")
        self.assertRefused(self.TOP_NO_LINKS.replace("src/a.py", "src/z.py"), "src/a.py (remove)")

    def test_a_row_frozen_at_ref_cannot_be_edited_on_a_note_without_links(self):
        ref = ("---\ntitle: a note\ncode_refs:\n  - src/a.py\nref_marks:\n" + rmark("src/a.py")
               + "---\n\nBody.\n")
        self.store(ref)
        self.assertClean(ref)
        self.assertRefused(ref.replace('why: "because"', 'why: "rewritten"'), "reference mark")

    def test_a_note_edited_to_a_fallback_parse_cannot_drop_its_refs_silently(self):
        # the NEW side's lenient parse does not recover `code_refs`: a removal no row can pair
        self.store(self.NOTE)
        self.assertRefused(self.NOTE.replace("title: a note", "title: bad: title"), "code_refs changed for src/a.py (remove)")

    def test_a_body_only_edit_of_a_note_is_clean(self):
        self.store(self.NOTE)
        self.assertClean(self.NOTE.replace("Body.", "Body edited."))

    def test_the_same_audit_runs_on_a_prefixed_store_root_when_staged(self):
        # the store lives in `mem/` of a larger repository: the staged blob is read by its path in the repo
        repo = self.tmp / "prefixed"
        repo.mkdir()
        git(["init", "-q", str(repo)], repo)
        git(["config", "user.email", "t@example.com"], repo)
        git(["config", "user.name", "T"], repo)
        root = repo / "mem"
        (root / "topics").mkdir(parents=True)
        (root / "topics" / "foo.md").write_text(self.NOTE)
        commit_all(repo)
        (root / "topics" / "foo.md").write_text(self.NOTE.replace("code_refs:\n  - src/a.py\n", "code_refs: []\n"))
        rc_wt, out_wt = run_memlint(["--against-ref", "HEAD", str(root)])
        git(["add", "-A"], repo)
        rc_st, out_st = run_memlint(["--against-ref", "HEAD", "--staged", str(root)])
        self.assertEqual((rc_wt, rc_st), (1, 1), f"{out_wt}\n{out_st}")
        self.assertIn("code_refs changed for src/a.py (remove)", out_wt)
        self.assertIn("code_refs changed for src/a.py (remove)", out_st)
        git(["reset", "-q"], repo)
        (root / "topics" / "foo.md").write_text(
            "---\ntitle: a note\ncode_refs: []\nref_marks:\n" + rmark("src/a.py", "remove") + "---\n\nBody.\n")
        git(["add", "-A"], repo)
        rc_ok, out_ok = run_memlint(["--against-ref", "HEAD", "--staged", str(root)])
        self.assertEqual(rc_ok, 0, out_ok)

    def test_a_topic_like_record_keeps_its_link_checks_and_gets_the_reference_audit_once(self):
        ref = ttext(["src/a.py"])
        self.store(ref)
        out = self.assertRefused(ttext([]), "code_refs changed for src/a.py (remove)")
        self.assertEqual(out.count("code_refs changed for src/a.py (remove)"), 1, out)

    def test_anchor_rows_of_a_note_without_links_stay_frozen(self):
        # gate round 14 R14-1: the anchor freeze runs in the early delivered-record block for a record the
        # link gate skips; a topic-like record keeps running it on the link path (so each once)
        a = anchor_block([good_row(ref="src/a.py")])
        ref = "---\ntitle: a note\ncode_refs:\n  - src/a.py\n" + a + "---\n\nBody.\n"
        self.store(ref)
        self.assertClean(ref)
        self.assertRefused(ref.replace("sha256", "sha1"), "anchor")
        self.assertRefused(ref.replace(a, ""), "anchor")


class TestRefMarksRowsValidatedOnEveryRecord(GuardCase):
    """gate round 15 R15-1 (Sol MAJOR, Grok MAJOR 1): `ref_marks:` rows are validated by the one row
    validator for every record that carries the block -- plain lint through the dispatch, the guard on
    the rows new since REF -- not only for topic-like records. The forged row below lacks provenance
    and has an invalid timestamp, yet pairs with the removal of `src/a.py`."""
    SHAPES = {
        "typeless": TestReferenceAuditCoversEveryDeliveredRecord.NOTE,
        "links-empty": TestReferenceAuditCoversEveryDeliveredRecord.EMPTY_LINKS,
        "top-id": TestReferenceAuditCoversEveryDeliveredRecord.TOP_NO_LINKS,
    }
    FORGED = '  - ref: "src/a.py"\n    action: remove\n    recorded_at: x\n'

    def _cleared(self, text, rows):
        return (text.replace("code_refs:\n  - src/a.py\n", "code_refs: []\nref_marks:\n" + rows))

    def test_plain_lint_validates_rows_of_a_record_without_links(self):
        for name, text in self.SHAPES.items():
            with self.subTest(name):
                errors, _ = lint_text(self._cleared(text, self.FORGED))
                self.assertError(errors, "ref_marks[0]", "missing field(s)")
                self.assertError(errors, "recorded_at must be a quoted ISO-8601 timestamp")
                errors, _ = lint_text(self._cleared(text, rmark("src/a.py", "remove")))
                self.assertEqual(errors, [], errors)

    def test_plain_lint_validates_rows_of_an_incident(self):
        # one validator for every record that has the block, not only for notes delivered as topics
        text = "---\ntype: incident\nid: INC-9300\ntitle: x\nref_marks:\n" + self.FORGED + "---\n\nBody.\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "incidents").mkdir()
            (root / "incidents" / "x.md").write_text(text)
            errors, _ = memlint.lint_root(root)
        self.assertError(errors, "ref_marks[0]", "missing field(s)")

    def test_the_guard_validates_new_rows_of_a_record_without_links(self):
        for name, text in self.SHAPES.items():
            with self.subTest(name):
                self.store(text)
                self.assertRefused(self._cleared(text, self.FORGED), "ref_marks[0]", "missing field(s)",
                                   "recorded_at must be a quoted ISO-8601 timestamp")

    def test_the_guard_accepts_a_well_formed_remove_row(self):
        for name, text in self.SHAPES.items():
            with self.subTest(name):
                self.store(text)
                self.assertClean(self._cleared(text, rmark("src/a.py", "remove")))

    def test_the_guard_does_not_rejudge_a_row_recorded_at_ref(self):
        # a malformed row already at REF is frozen by the byte-for-byte freeze, not re-validated
        text = self.SHAPES["typeless"]
        ref = text.replace("code_refs:\n  - src/a.py\n", "code_refs:\n  - src/a.py\nref_marks:\n" + self.FORGED.replace("remove", "add"))
        self.store(ref)
        self.assertClean(ref.replace("Body.", "Body edited."))


class TestConceptRefMarksPlainLint(unittest.TestCase):
    """gate round 16 R16-2 (Sol MINOR, Grok NIT): plain lint judges a concept's `ref_marks:` rows with the
    one row validator, like every other record; a concept without rows is unchanged."""
    FORGED = TestRefMarksRowsValidatedOnEveryRecord.FORGED

    def _lint(self, extra):
        text = "---\ntype: concept\ntitle: x\n" + extra + "---\n\nBody.\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "concepts").mkdir()
            (root / "concepts" / "x.md").write_text(text)
            return memlint.lint_root(root)

    def test_a_concept_with_a_forged_row_is_a_plain_lint_error(self):
        errors, _ = self._lint("ref_marks:\n" + self.FORGED)
        self.assertTrue(any("ref_marks[0]" in e and "missing field(s)" in e for e in errors), errors)

    def test_a_concept_with_a_well_formed_row_passes(self):
        errors, _ = self._lint("ref_marks:\n" + rmark("src/a.py", "remove"))
        self.assertEqual(errors, [], errors)

    def test_a_concept_without_rows_is_unchanged(self):
        errors, _ = self._lint("")
        self.assertEqual(errors, [], errors)


class TestDeliveredTopicCannotBeDeleted(GuardCase):
    """gate round 15 R15-2 (Sol MAJOR, Grok MAJOR 2; owner-facing ruling): a record the index delivered
    as a TOPIC cannot be deleted or moved away without an error, chain or not. Records delivered as an
    incident, investigation or concept keep the pre-existing deletion policy."""
    SHAPES = TestRefMarksRowsValidatedOnEveryRecord.SHAPES
    GONE = "topic file deleted or renamed after being recorded"

    def _both(self, mutate):
        """Run `mutate()` then the worktree and the staged guard; return (rc, out) after asserting they agree."""
        mutate()
        rc_wt, out_wt = run_memlint(["--against-ref", "HEAD", str(self.root)])
        git(["add", "-A"], self.root)
        rc_st, out_st = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        git(["reset", "-q"], self.root)
        self.assertEqual(rc_wt, rc_st, f"worktree vs staged disagree:\n{out_wt}\n{out_st}")
        return rc_wt, out_wt, out_st

    def test_deleting_a_delivered_note_is_refused_on_both_paths(self):
        for name, text in self.SHAPES.items():
            with self.subTest(name):
                self.store(text)
                rc, out_wt, out_st = self._both(lambda: (self.root / "topics" / "foo.md").unlink())
                self.assertEqual(rc, 1, out_wt)
                self.assertIn(self.GONE, out_wt)
                self.assertIn(self.GONE, out_st)

    def test_moving_a_delivered_note_to_inbox_is_refused(self):
        # a move is a deletion plus an added file; the added file is free, the deletion half errors
        for name, text in self.SHAPES.items():
            with self.subTest(name):
                self.store(text)

                def move():
                    (self.root / "inbox").mkdir(exist_ok=True)
                    (self.root / "topics" / "foo.md").rename(self.root / "inbox" / "foo.md")
                rc, out_wt, out_st = self._both(move)
                self.assertEqual(rc, 1, out_wt)
                self.assertIn(self.GONE, out_wt)
                self.assertIn(self.GONE, out_st)

    def test_renaming_a_delivered_note_within_topics_is_refused(self):
        self.store(self.SHAPES["typeless"])
        rc, out_wt, _ = self._both(lambda: (self.root / "topics" / "foo.md").rename(self.root / "topics" / "bar.md"))
        self.assertEqual(rc, 1, out_wt)
        self.assertIn(self.GONE, out_wt)

    def test_a_delivered_incident_stays_deletable(self):
        root = git_store(self.tmp / "inc_store")
        self.root = root
        (root / "incidents").mkdir(exist_ok=True)
        (root / "incidents" / "x.md").write_text(
            "---\ntype: incident\nid: INC-9400\ntitle: x\ncode_refs:\n  - src/a.py\n---\n\nBody.\n")
        commit_all(root)
        rc, out_wt, out_st = self._both(lambda: (root / "incidents" / "x.md").unlink())
        self.assertEqual(rc, 0, f"{out_wt}\n{out_st}")

    def test_a_delivered_concept_stays_deletable(self):
        root = git_store(self.tmp / "concept_store")
        self.root = root
        (root / "concepts").mkdir(exist_ok=True)
        (root / "concepts" / "x.md").write_text("---\ntype: concept\ntitle: x\n---\n\nBody.\n")
        commit_all(root)
        rc, out_wt, out_st = self._both(lambda: (root / "concepts" / "x.md").unlink())
        self.assertEqual(rc, 0, f"{out_wt}\n{out_st}")


class TestDeliveredRecordCannotBecomeASymlink(GuardCase):
    """gate round 16 R16-1 (Sol MAJOR): the index skips symlinks, so replacing a delivered record by
    one drops its for-path and digest entries. The guard decides from the git type (diff status `T`, the
    index mode `120000`, a symlink on the worktree path) and never parses the symlink's blob as content,
    even when the target text equals the old Markdown. An ADDED symlink stays free."""
    SHAPES = dict(TestRefMarksRowsValidatedOnEveryRecord.SHAPES)
    MSG = "record replaced by a symlink"

    def _both(self, rel, target, root=None):
        """Replace `rel` by a symlink whose target text is `target`: the worktree guard sees a real
        symlink; the staged guard sees an index entry of mode 120000 (the worktree file is put back, so
        only the index differs). Returns (rc_wt, out_wt, rc_st, out_st)."""
        root = root or self.root
        path = root / rel
        original = path.read_bytes() if path.exists() and not path.is_symlink() else None
        path.unlink()
        os.symlink(target, path)
        rc_wt, out_wt = run_memlint(["--against-ref", "HEAD", str(root)])
        path.unlink()
        if original is not None:
            path.write_bytes(original)
        blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(root),
                              input=target.encode(), capture_output=True, check=True).stdout.decode().strip()
        git(["update-index", "--cacheinfo", f"120000,{blob},{rel}"], root)
        self.assertEqual(self._mode_in(root, rel), "120000")
        rc_st, out_st = run_memlint(["--against-ref", "HEAD", "--staged", str(root)])
        git(["reset", "-q"], root)
        return rc_wt, out_wt, rc_st, out_st

    @staticmethod
    def _mode_in(root, rel):
        return git(["ls-files", "-s", "--", rel], root).stdout.split(" ", 1)[0]

    def test_a_delivered_record_replaced_by_a_symlink_is_refused_on_both_paths(self):
        # Sol's fixture: the symlink target text IS the old Markdown, so a content comparison sees no change
        for name, text in self.SHAPES.items():
            with self.subTest(name):
                self.store(text)
                rc_wt, out_wt, rc_st, out_st = self._both("topics/foo.md", text)
                self.assertEqual((rc_wt, rc_st), (1, 1), f"{out_wt}\n{out_st}")
                self.assertIn(self.MSG, out_wt)
                self.assertIn(self.MSG, out_st)

    def test_a_topic_with_a_link_chain_replaced_by_a_symlink_is_refused(self):
        self.store()
        text = (self.root / "topics" / "foo.md").read_text()
        rc_wt, out_wt, rc_st, out_st = self._both("topics/foo.md", text)
        self.assertEqual((rc_wt, rc_st), (1, 1), f"{out_wt}\n{out_st}")
        self.assertIn(self.MSG, out_wt)
        self.assertIn(self.MSG, out_st)

    def test_a_delivered_incident_replaced_by_a_symlink_is_refused(self):
        root = git_store(self.tmp / "inc_store")
        self.root = root
        (root / "incidents").mkdir(exist_ok=True)
        text = "---\ntype: incident\nid: INC-9500\ntitle: x\n---\n\nBody.\n"
        (root / "incidents" / "x.md").write_text(text)
        commit_all(root)
        rc_wt, out_wt, rc_st, out_st = self._both("incidents/x.md", "elsewhere.md")
        self.assertEqual((rc_wt, rc_st), (1, 1), f"{out_wt}\n{out_st}")
        self.assertIn(self.MSG, out_wt)
        self.assertIn(self.MSG, out_st)

    def test_the_symlink_blob_is_never_parsed_as_content(self):
        # a target that is not Markdown at all gives the same single error, not a parse error
        self.store(self.SHAPES["typeless"])
        rc_wt, out_wt, rc_st, out_st = self._both("topics/foo.md", "../elsewhere.md")
        self.assertEqual((rc_wt, rc_st), (1, 1), f"{out_wt}\n{out_st}")
        for out in (out_wt, out_st):
            self.assertIn(self.MSG, out)
            self.assertNotIn("type changed", out)

    def test_a_symlink_replacing_a_non_delivered_note_is_not_an_error(self):
        # an `inbox/` note is delivered with a non-canonical type: nothing to lose, nothing else fires
        root = git_store(self.tmp / "inbox_store")
        self.root = root
        (root / "inbox").mkdir()
        (root / "inbox" / "n.md").write_text("---\ntitle: a note\n---\n\nBody.\n")
        commit_all(root)
        rc_wt, out_wt, rc_st, out_st = self._both("inbox/n.md", "elsewhere.md")
        self.assertEqual((rc_wt, rc_st), (0, 0), f"{out_wt}\n{out_st}")
        self.assertNotIn(self.MSG, out_wt + out_st)

    def test_an_added_symlink_is_free(self):
        self.store()
        for staged in (False, True):
            with self.subTest(staged=staged):
                path = self.root / "topics" / "new.md"
                os.symlink("foo.md", path)
                if staged:
                    git(["add", "topics/new.md"], self.root)
                    self.assertEqual(self._mode_in(self.root, "topics/new.md"), "120000")
                rc, out = run_memlint(["--against-ref", "HEAD"] + (["--staged"] if staged else []) + [str(self.root)])
                git(["reset", "-q"], self.root)
                path.unlink()
                self.assertEqual(rc, 0, out)

    def test_a_regular_edit_is_still_judged_as_content(self):
        self.store(self.SHAPES["typeless"])
        self.assertClean(self.SHAPES["typeless"].replace("Body.", "Body edited."))


class TestSymlinkOrGitlinkAtRefDeliveredNothing(GuardCase):
    """gate round 17 R17-1 (Sol MINOR, Grok NIT): a symlink (120000) or gitlink (160000) at REF
    delivered nothing to the index, so the guard has no delivered record to protect at that path. A
    regular file replacing it is judged as an ADDED file; a deletion, a retarget or a gitlink left in
    place is not judged. A regular delivered record replaced by a symlink or a gitlink is still refused."""
    REL = "topics/x.md"
    MSG = "record replaced by a symlink or gitlink"

    @staticmethod
    def clean(tid):
        return topic_text(tid=tid, standing="standing: []\n")

    def _base(self, kind):
        """A store whose REF holds topics/foo.md plus topics/x.md as `kind`: symlink, gitlink or file."""
        self.store()
        root, path = self.root, self.root / self.REL
        if kind == "symlink":
            os.symlink("missing-target.md", path)
            commit_all(root)
        elif kind == "gitlink":
            sha = git(["rev-parse", "HEAD"], root).stdout.strip()
            git(["update-index", "--add", "--cacheinfo", f"160000,{sha},{self.REL}"], root)
            git(["commit", "-q", "-m", "gitlink"], root)
            self.assertEqual(git(["ls-tree", "HEAD", "--", self.REL], root).stdout.split(" ", 1)[0], "160000")
        else:
            path.write_text(self.clean("TOP-9102"))
            commit_all(root)
        return root, path

    def _run(self, change, staged_change=None):
        """Apply `change()` to the worktree and run the worktree guard; then stage everything (or apply
        `staged_change`) and run the staged guard; reset. Returns (rc_wt, out_wt, rc_st, out_st)."""
        change()
        rc_wt, out_wt = run_memlint(["--against-ref", "HEAD", str(self.root)])
        if staged_change is not None:
            staged_change()
        else:
            git(["add", "-A"], self.root)
        rc_st, out_st = run_memlint(["--against-ref", "HEAD", "--staged", str(self.root)])
        git(["reset", "-q"], self.root)
        return rc_wt, out_wt, rc_st, out_st

    def _replace_with_file(self, path, text):
        def go():
            if path.is_symlink() or path.exists():
                path.unlink()
            path.write_text(text)
        return go

    def test_a_gitlink_at_ref_replaced_by_a_regular_topic_is_clean(self):
        _root, path = self._base("gitlink")
        rc_wt, out_wt, rc_st, out_st = self._run(self._replace_with_file(path, self.clean("TOP-9103")))
        self.assertEqual((rc_wt, rc_st), (0, 0), f"{out_wt}\n{out_st}")

    def test_the_regular_file_replacing_a_gitlink_is_judged_as_an_added_file(self):
        _root, path = self._base("gitlink")
        rc_wt, out_wt, rc_st, out_st = self._run(self._replace_with_file(path, topic_text(tid="TOP-9101")))
        self.assertEqual((rc_wt, rc_st), (1, 1), f"{out_wt}\n{out_st}")
        # its default `standing: [L2]` has no `add` row: a brand-new topic is judged on every link and mark
        self.assertIn("standing changed for L2 (add)", out_wt)
        self.assertIn("standing changed for L2 (add)", out_st)
        self.assertNotIn(self.MSG, out_wt + out_st)
        self.assertNotIn("type changed", out_wt + out_st)

    def test_a_symlink_at_ref_replaced_by_a_regular_topic_is_clean(self):
        _root, path = self._base("symlink")
        rc_wt, out_wt, rc_st, out_st = self._run(self._replace_with_file(path, self.clean("TOP-9104")))
        self.assertEqual((rc_wt, rc_st), (0, 0), f"{out_wt}\n{out_st}")

    def test_a_symlink_at_ref_retargeted_to_a_missing_target_is_clean(self):
        _root, path = self._base("symlink")

        def retarget():
            path.unlink()
            os.symlink("another-missing.md", path)
        rc_wt, out_wt, rc_st, out_st = self._run(retarget)
        self.assertEqual((rc_wt, rc_st), (0, 0), f"{out_wt}\n{out_st}")
        self.assertNotIn("deleted", out_wt + out_st)

    def test_a_symlink_at_ref_whose_target_text_looks_like_a_topic_is_clean_when_retargeted(self):
        self.store()
        root, path = self.root, self.root / self.REL
        os.symlink(self.clean("TOP-9105"), path)
        commit_all(root)

        def retarget():
            path.unlink()
            os.symlink("elsewhere", path)
        rc_wt, out_wt, rc_st, out_st = self._run(retarget)
        self.assertEqual((rc_wt, rc_st), (0, 0), f"{out_wt}\n{out_st}")

    def test_a_symlink_at_ref_deleted_is_clean(self):
        _root, path = self._base("symlink")
        rc_wt, out_wt, rc_st, out_st = self._run(path.unlink)
        self.assertEqual((rc_wt, rc_st), (0, 0), f"{out_wt}\n{out_st}")
        self.assertNotIn("deleted", out_wt + out_st)

    def test_a_gitlink_at_ref_deleted_is_clean(self):
        _root, path = self._base("gitlink")
        rc_wt, out_wt, rc_st, out_st = self._run(
            lambda: None, lambda: git(["rm", "-q", "--cached", self.REL], self.root))
        self.assertEqual((rc_wt, rc_st), (0, 0), f"{out_wt}\n{out_st}")

    def test_a_regular_delivered_topic_replaced_by_a_symlink_is_still_refused(self):
        _root, path = self._base("file")
        text = path.read_text()

        def to_symlink():
            path.unlink()
            os.symlink(text, path)

        def stage_symlink():
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=str(self.root),
                                  input=text.encode(), capture_output=True, check=True).stdout.decode().strip()
            git(["update-index", "--cacheinfo", f"120000,{blob},{self.REL}"], self.root)
        rc_wt, out_wt, rc_st, out_st = self._run(to_symlink, stage_symlink)
        self.assertEqual((rc_wt, rc_st), (1, 1), f"{out_wt}\n{out_st}")
        self.assertIn(self.MSG, out_wt)
        self.assertIn(self.MSG, out_st)

    def test_a_regular_delivered_topic_replaced_by_a_gitlink_is_refused(self):
        _root, path = self._base("file")
        sha = git(["rev-parse", "HEAD"], self.root).stdout.strip()

        def to_gitlink():
            path.unlink()
            path.mkdir()  # a submodule checkout is a directory on the worktree
        rc_wt, out_wt, rc_st, out_st = self._run(
            to_gitlink, lambda: git(["update-index", "--cacheinfo", f"160000,{sha},{self.REL}"], self.root))
        self.assertEqual((rc_st), 1, f"{out_wt}\n{out_st}")
        self.assertIn(self.MSG, out_st)
        # the worktree side sees a directory the index (still the regular file) does not record as a gitlink
        # until it is staged, so only the staged guard is the delivery gate here
        git(["update-index", "--cacheinfo", f"160000,{sha},{self.REL}"], self.root)
        rc_wt, out_wt = run_memlint(["--against-ref", "HEAD", str(self.root)])
        git(["reset", "-q"], self.root)
        self.assertEqual(rc_wt, 1, out_wt)
        self.assertIn(self.MSG, out_wt)


class TestSharedBlockItemDelete(unittest.TestCase):
    def test_the_helper_deletes_the_first_match_and_ignores_comments_and_quotes(self):
        lines = ["code_refs:\n", '  - "src/a.py"  # why\n', "  - src/b.py\n", "  - src/a.py\n", "x: 1\n"]
        self.assertTrue(memidx._delete_block_item(lines, 0, 3, "code_refs", "src/a.py", "\n"))
        self.assertEqual(lines, ["code_refs:\n", "  - src/b.py\n", "  - src/a.py\n", "x: 1\n"])

    def test_the_helper_reads_a_quoted_item_up_to_its_closing_quote(self):
        lines = ["code_refs:\n", '  - "docs/* # drafts"  # note\n', "  - 'it''s # x'\n", "x: 1\n"]
        self.assertFalse(memidx._delete_block_item(lines, 0, 2, "code_refs", "docs/*", "\n"))
        self.assertTrue(memidx._delete_block_item(lines, 0, 2, "code_refs", "docs/* # drafts", "\n"))
        self.assertTrue(memidx._delete_block_item(lines, 0, 1, "code_refs", "it's # x", "\n"))
        self.assertEqual(lines, ["code_refs: []\n", "x: 1\n"])

    def test_the_helper_leaves_an_empty_list_for_the_last_item_and_reports_a_miss(self):
        lines = ["standing:\n", "  - L2\n", "x: 1\n"]
        self.assertFalse(memidx._delete_block_item(lines, 0, 1, "standing", "L9", "\n"))
        self.assertEqual(lines, ["standing:\n", "  - L2\n", "x: 1\n"])
        self.assertTrue(memidx._delete_block_item(lines, 0, 1, "standing", "L2", "\n"))
        self.assertEqual(lines, ["standing: []\n", "x: 1\n"])

    def test_remove_code_ref_and_edit_standing_line_both_go_through_it(self):
        text = "---\ntype: topic\ncode_refs:\n  - src/a.py\nstanding:\n  - L2\n  - L3\nlinks: []\n---\nB\n"
        real = memidx._delete_block_item
        with mock.patch.object(memidx, "_delete_block_item", wraps=real) as spy:
            out = memidx.remove_code_ref(text, "src/a.py")
            self.assertEqual(spy.call_count, 1)
            self.assertIn("code_refs: []\n", out)
            out2 = memidx.edit_standing_line(text, "L3", add=False)
            self.assertEqual(spy.call_count, 2)
            self.assertNotIn("L3", out2)
            self.assertEqual(memidx.edit_standing_line(text, "L9", add=False), text)
        with self.assertRaises(ValueError):
            memidx.remove_code_ref(text, "src/zzz.py")

# ---------------------------------------------------------------------------
# plain lint: the rows and the anchor rule
# ---------------------------------------------------------------------------

class TestRefMarksPlainLint(ErrCase):
    def lint(self, *marks, refs=("src/a.py",), **kw):
        return lint_text(ttext(list(refs), *marks, **kw))

    def test_valid_rows_pass_for_every_authority(self):
        for auth in ("owner-verbatim", "owner-ratified", "agent-inference", "reviewer-finding", "code-derived"):
            with self.subTest(auth):
                errors, _ = self.lint(rmark("src/a.py", authority=auth))
                self.assertEqual(errors, [], errors)

    def test_rows_are_never_required(self):
        errors, _ = self.lint()
        self.assertEqual(errors, [], errors)

    def test_shape_errors(self):
        errors, _ = lint_text(topic_text(extra_fm="ref_marks:\n  - just-a-string\n"))
        self.assertError(errors, "ref_marks must be a list of mappings")
        errors, _ = self.lint(rmark("src/a.py", action="toggle"))
        self.assertError(errors, "action 'toggle'")
        errors, _ = self.lint(rmark("src/a.py", authority="somebody"))
        self.assertError(errors, "authority 'somebody'")
        errors, _ = self.lint(rmark("src/a.py").replace('    why: "because"\n', ""))
        self.assertError(errors, "ref_marks[0]", "missing field(s)", "why")
        errors, _ = self.lint(rmark("src/a.py") + "    note: hi\n")
        self.assertError(errors, "unknown field(s)", "note")
        errors, _ = self.lint(rmark("src/a.py", why="two\\nlines"))
        self.assertError(errors, "why must be a single line")
        errors, _ = self.lint(rmark("src/a.py", recorded_at="yesterday"))
        self.assertError(errors, "recorded_at")
        errors, _ = self.lint(rmark("src/a.py"), rmark("src/a.py", why="again"))
        self.assertError(errors, "duplicate row")
        errors, _ = self.lint(rmark("#frag"))
        self.assertError(errors, "fragment-only")

    def anchor_topic(self, refs, *marks):
        return binding_topic([good_row()], code_refs=refs, marks_text=("ref_marks:\n" + "".join(marks)) if marks else "")

    def test_an_anchor_for_a_ref_not_in_code_refs_is_an_error_without_an_unbind(self):
        errors, _ = lint_text(self.anchor_topic(()))
        self.assertError(errors, "anchors[0]", "not present verbatim in this topic's code_refs")

    def test_the_newest_remove_row_excuses_the_missing_ref(self):
        errors, _ = lint_text(self.anchor_topic((), rmark("src/foo.py", "remove", LATER), rmark("src/foo.py")))
        self.assertEqual(errors, [], errors)

    def test_a_prefix_anchor_row_is_an_error_even_when_its_ref_is_unbound(self):
        # gate round 10 R10-3 (Sol MINOR): the unbound exemption only lifts "ref must be in code_refs"
        text = binding_topic([good_row(ref="src/")], code_refs=(), marks_text="ref_marks:\n" + rmark("src/", "remove", LATER)
                             + rmark("src/"))
        errors, _ = lint_text(text)
        self.assertError(errors, "anchors[0]", "ref is a prefix or glob")
        self.assertFalse(any("not present verbatim" in e for e in errors), errors)

    def test_a_glob_anchor_row_is_an_error_when_its_ref_is_unbound(self):
        text = binding_topic([good_row(ref="src/*.py")], code_refs=(), marks_text="ref_marks:\n" + rmark("src/*.py", "remove", LATER)
                             + rmark("src/*.py"))
        errors, _ = lint_text(text)
        self.assertError(errors, "anchors[0]", "ref is a prefix or glob")

    def test_a_later_add_rearms_the_requirement(self):
        marks = (rmark("src/foo.py", "add", "2026-11-05T09:12:41-05:00"), rmark("src/foo.py", "remove", LATER))
        errors, _ = lint_text(self.anchor_topic((), *marks))
        self.assertError(errors, "anchors[0]", "not present verbatim")
        ok, _ = lint_text(self.anchor_topic(("src/foo.py",), *marks))
        self.assertEqual(ok, [], ok)


# ---------------------------------------------------------------------------
# bind / unbind end to end
# ---------------------------------------------------------------------------

class BindCase(TmpCase):
    TODAY = "2026-11-02"

    def setUp(self):
        super().setUp()
        self.code = make_code_root(self.tmp, {"src/foo.py": PY_SRC, "src/other.py": "x = 1\n"})
        self.store = git_store(self.tmp / "store")
        self.clock = [datetime(2026, 11, 3, 9, 0, 0, tzinfo=timezone(timedelta(hours=-5)))]

        def now():
            self.clock[0] += timedelta(seconds=1)
            return self.clock[0]

        for patcher in (mock.patch.object(memidx, "_anchor_today", return_value=self.TODAY),
                        mock.patch.object(memidx, "_now", side_effect=now),
                        mock.patch.dict(os.environ, {"MEMCONTINUUM_TRANSCRIPTS_DIR": str(self.tmp / "tr")})):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop("MEMCONTINUUM_RECORDED_BY", None)
        self.path = self.store / "topics" / "foo.md"

    def put(self, text):
        self.path.write_text(text)
        commit_all(self.store)

    def text(self):
        return self.path.read_text()

    def fm(self):
        return memidx.parse_record_raw(self.text(), self.path).frontmatter

    def bind(self, ref="src/foo.py", *extra):
        return run_memidx(["bind", "TOP-1", ref, "--root", str(self.store), "--code-root", str(self.code), *extra])

    def unbind(self, ref="src/foo.py", *extra):
        return run_memidx(["unbind", "TOP-1", ref, "--root", str(self.store), *extra])

    def assertGuardsClean(self):
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)
        rc, out = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 0, out)
        git(["add", "-A"], self.store)
        rc, out = run_memlint(["--against-ref", "HEAD", "--staged", str(self.store)])
        self.assertEqual(rc, 0, out)
        git(["reset", "-q"], self.store)

    def changed_lines(self, sign):
        diff = git(["diff", "-U0", "HEAD", "--", "topics/foo.md"], self.store).stdout
        return [ln[1:] for ln in diff.splitlines()
                if ln.startswith(sign) and not ln.startswith(sign * 3)]


class TestBindWritesItsRow(BindCase):
    def test_a_new_ref_gets_one_add_row_and_one_anchor(self):
        self.put(binding_topic(code_refs=()))
        rc, out, err = self.bind()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fm()["code_refs"], ["src/foo.py"])
        rows = memidx.ref_mark_rows(self.fm())
        self.assertEqual([(r["ref"], r["action"], r["authority"]) for r in rows],
                         [("src/foo.py", "add", "agent-inference")])
        self.assertEqual(rows[0]["source"], "memidx bind")
        self.assertEqual(rows[0]["recorded_by"], "memidx")
        self.assertEqual(len(memidx.anchor_rows(self.fm())), 1)
        text = self.text()
        self.assertLess(text.index("code_refs:"), text.index("ref_marks:"))
        self.assertLess(text.index("ref_marks:"), text.index("anchors:"))
        self.assertGuardsClean()

    def test_the_ref_mark_sits_after_an_existing_code_refs_block(self):
        self.put(binding_topic(code_refs=("src/other.py",)))
        self.assertEqual(self.bind()[0], 0)
        text = self.text()
        self.assertLess(text.index("src/foo.py"), text.index("ref_marks:"))
        self.assertEqual(self.fm()["code_refs"], ["src/other.py", "src/foo.py"])
        self.assertGuardsClean()

    def test_a_ref_already_in_code_refs_gets_an_anchor_but_no_row(self):
        self.put(binding_topic())
        self.assertEqual(self.bind()[0], 0)
        self.assertEqual(memidx.ref_mark_rows(self.fm()), [])
        self.assertEqual(len(memidx.anchor_rows(self.fm())), 1)
        self.assertGuardsClean()

    def test_a_second_bind_of_a_bound_ref_is_a_noop(self):
        self.put(binding_topic(code_refs=()))
        self.bind()
        before = self.text()
        commit_all(self.store)
        rc, out, err = self.bind()
        self.assertEqual(rc, 0, err)
        self.assertIn("already has an anchor", out)
        self.assertEqual(self.text(), before)

    def test_a_derived_owner_tier_is_recorded_when_asked_for(self):
        write_mark_transcript(self.tmp / "tr")
        self.put(binding_topic(code_refs=()))
        rc, _o, err = self.bind("src/foo.py", *MARK_FLAGS, "--why", "the owner approved this binding")
        self.assertEqual(rc, 0, err)
        row = memidx.ref_mark_rows(self.fm())[0]
        self.assertEqual(row["authority"], "owner-ratified")
        self.assertEqual(row["why"], "the owner approved this binding")
        self.assertTrue(row["source"].startswith(f"owner yes-message {MARK_YES}, session {MARK_SID}"))
        self.assertGuardsClean()

    def test_a_failed_derivation_warns_and_records_agent_inference(self):
        self.put(binding_topic(code_refs=()))
        rc, _o, err = self.bind("src/foo.py", "--session", MARK_SID, "--yes-message", MARK_YES)
        self.assertEqual(rc, 0, err)
        self.assertIn("authority not derived", err)
        row = memidx.ref_mark_rows(self.fm())[0]
        self.assertEqual(row["authority"], "agent-inference")
        self.assertTrue(row["source"].startswith("transcript unavailable:"))

    def test_a_verbatim_flag_pair_that_is_incomplete_refuses(self):
        self.put(binding_topic(code_refs=()))
        before = self.text()
        rc, _o, err = self.bind("src/foo.py", "--session", MARK_SID, "--owner-message", MARK_OWNER)
        self.assertEqual(rc, 1, err)
        self.assertEqual(self.text(), before)


class TestUnbind(BindCase):
    def bound(self, refs=("src/other.py",)):
        self.put(binding_topic(code_refs=refs))
        self.assertEqual(self.bind()[0], 0)
        commit_all(self.store)

    def test_unbind_removes_the_item_adds_one_remove_row_and_touches_no_anchor(self):
        self.bound()
        anchors_before = memidx.anchor_rows(self.fm())
        before = self.text()
        rc, out, err = self.unbind()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fm()["code_refs"], ["src/other.py"])
        rows = memidx.ref_mark_rows(self.fm())
        self.assertEqual([(r["ref"], r["action"]) for r in rows], [("src/foo.py", "remove"), ("src/foo.py", "add")])
        self.assertEqual(memidx.anchor_rows(self.fm()), anchors_before)
        removed = self.changed_lines("-")
        self.assertEqual([ln.strip() for ln in removed], ['- "src/foo.py"'])
        added = self.changed_lines("+")
        self.assertEqual(len(added), 8)  # exactly the new row's eight fields
        self.assertTrue(all("anchors" not in ln and "digest" not in ln for ln in added))
        # the anchors block and every link are byte-identical
        spans_a = memlint._link_raw_spans(before, memidx.parse_record_raw(before, "x").frontmatter["links"])
        spans_b = memlint._link_raw_spans(self.text(), self.fm()["links"])
        self.assertEqual(spans_a, spans_b)
        self.assertIn("- ref: \"src/foo.py\"\n    action: remove", self.text())
        self.assertGuardsClean()

    def test_a_quoted_ref_containing_a_space_hash_can_be_unbound(self):
        # the accepted quoted form `"docs/* # drafts"`: the ` #` is inside the quotes, not a comment
        ref = "docs/* # drafts"
        self.put(binding_topic(code_refs=(f'"{ref}"', "src/other.py")))
        self.assertIn(f'"{ref}"', self.text())
        self.assertEqual(self.fm()["code_refs"], [ref, "src/other.py"])
        rc, out, err = self.unbind(ref)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fm()["code_refs"], ["src/other.py"])
        rows = memidx.ref_mark_rows(self.fm())
        self.assertEqual([(r["ref"], r["action"]) for r in rows], [(ref, "remove")])
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)

    def test_the_anchor_rule_accepts_the_unbound_state_but_not_a_silent_removal(self):
        self.bound()
        self.assertEqual(self.unbind()[0], 0)
        errors, _ = memlint.lint_root(self.store)
        self.assertEqual(errors, [], errors)
        # a hand edit that drops the ref without a row: plain lint after a re-bind sees it
        self.bind()
        text = self.text().replace('  - "src/foo.py"\n', "", 1)
        self.path.write_text(text)
        errors, _ = memlint.lint_root(self.store)
        self.assertTrue(any("not present verbatim in this topic's code_refs" in e for e in errors), errors)
        rc, out = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 1, out)

    def test_a_fabricated_prefix_anchor_row_beside_a_remove_row_is_refused_by_lint_and_the_guard(self):
        # gate round 10 R10-3: removing `src/` with its row must not also admit a prefix anchor row
        self.put(binding_topic(code_refs=("src/", "src/other.py")))
        self.assertEqual(self.unbind("src/")[0], 0)
        commit_all(self.store)
        self.assertGuardsClean()
        text = self.text()
        self.assertNotIn("anchors:", text)
        self.path.write_text(text.replace("links:\n", anchor_block([good_row(ref="src/")]) + "links:\n", 1))
        errors, _ = memlint.lint_root(self.store)
        self.assertTrue(any("ref is a prefix or glob" in e for e in errors), errors)
        rc, out = run_memlint(["--against-ref", "HEAD", str(self.store)])
        self.assertEqual(rc, 1, out)
        self.assertIn("ref is a prefix or glob", out)

    def test_for_path_stops_matching_the_unbound_path(self):
        db = self.tmp / "idx.sqlite"
        self.bound(refs=())

        def for_path():
            rc, _o, err = run_memidx(["reindex", "--root", str(self.store), "--project", "p",
                                      "--db", str(db), "--no-embed"])
            self.assertEqual(rc, 0, err)
            rc, out, err = run_memidx(["for-path", "src/foo.py", "--project", "p", "--db", str(db),
                                       "--root", str(self.store)])
            self.assertEqual(rc, 0, err)
            return out

        self.assertIn("TOP-1", for_path())
        self.assertEqual(self.unbind()[0], 0)
        self.assertNotIn("TOP-1", for_path())

    def test_rebind_after_unbind_adds_a_row_and_no_second_anchor(self):
        self.bound()
        anchor = memidx.anchor_rows(self.fm())
        self.assertEqual(self.unbind()[0], 0)
        commit_all(self.store)
        rc, out, err = self.bind()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fm()["code_refs"], ["src/other.py", "src/foo.py"])
        self.assertEqual(memidx.anchor_rows(self.fm()), anchor)
        rows = memidx.ref_mark_rows(self.fm())
        self.assertEqual([r["action"] for r in rows], ["add", "remove", "add"])
        self.assertEqual(memidx.ref_latest_action(self.fm(), "src/foo.py"), "add")
        self.assertGuardsClean()
        # the requirement is re-armed: the ref cannot go missing silently again
        self.path.write_text(self.text().replace('  - "src/foo.py"\n', "", 1))
        errors, _ = memlint.lint_root(self.store)
        self.assertTrue(any("not present verbatim" in e for e in errors), errors)

    def test_unbinding_the_last_ref_leaves_an_empty_list(self):
        self.bound(refs=())
        self.assertEqual(self.unbind()[0], 0)
        self.assertIn("code_refs: []\n", self.text())
        self.assertEqual(self.fm()["code_refs"], [])
        self.assertGuardsClean()

    def test_prefix_and_glob_refs_can_be_unbound(self):
        self.put(binding_topic(code_refs=("src/lib/", "src/*.py", "src/other.py")))
        for ref in ("src/lib/", "src/*.py"):
            with self.subTest(ref):
                rc, _o, err = self.unbind(ref)
                self.assertEqual(rc, 0, err)
        self.assertEqual(self.fm()["code_refs"], ["src/other.py"])
        self.assertEqual([(r["ref"], r["action"]) for r in memidx.ref_mark_rows(self.fm())],
                         [("src/*.py", "remove"), ("src/lib/", "remove")])
        self.assertGuardsClean()
        rc, out, err = self.bind("src/*.py")  # a glob is bound again, without an anchor (TOP-0137 L29)
        self.assertEqual(rc, 0, err)
        self.assertIn("bound without an anchor", out)
        self.assertFalse(self.fm().get("anchors"))

    def test_a_ref_that_is_not_bound_is_a_noop(self):
        self.put(binding_topic())
        before = self.text()
        rc, out, err = self.unbind("src/nope.py")
        self.assertEqual(rc, 0, err)
        self.assertIn("is not in", out)
        self.assertEqual(self.text(), before)

    def test_a_non_empty_flow_list_is_refused(self):
        self.put(binding_topic().replace("code_refs:\n  - src/foo.py\n", "code_refs: [src/foo.py, src/other.py]\n"))
        before = self.text()
        rc, _o, err = self.unbind()
        self.assertEqual(rc, 1)
        self.assertIn("flow list", err)
        self.assertEqual(self.text(), before)

    def test_a_multi_line_quoted_reference_is_not_unbindable_and_nothing_is_written(self):
        # gate round 3 R3-4: the folded quoted scalar parses to `docs/* # drafts`, but the editor
        # compares one physical line, so unbind refuses (fail-closed) until it is on one line
        ref = "docs/* # drafts"
        self.put(binding_topic(code_refs=('"docs/*\n    # drafts"', "src/other.py")))
        self.assertEqual(self.fm()["code_refs"], [ref, "src/other.py"])
        before = self.text()
        rc, _o, err = self.unbind(ref)
        self.assertNotEqual(rc, 0)
        self.assertTrue(err.strip(), err)
        self.assertEqual(self.text(), before)

    def test_dry_run_writes_nothing(self):
        self.bound()
        before = self.text()
        rc, out, err = self.unbind("src/foo.py", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry-run: nothing written", out)
        self.assertIn("+    action: remove", out)
        self.assertEqual(self.text(), before)

    def test_unknown_topic_and_bad_text_are_refused(self):
        self.bound()
        before = self.text()
        self.assertEqual(run_memidx(["unbind", "TOP-9", "src/foo.py", "--root", str(self.store)])[0], 1)
        self.assertEqual(self.unbind("src/foo.py", "--why", "two\nlines")[0], 1)
        self.assertEqual(self.text(), before)

    def test_a_derived_owner_tier_is_recorded_for_the_remove_row(self):
        write_mark_transcript(self.tmp / "tr")
        self.bound()
        rc, _o, err = self.unbind("src/foo.py", *MARK_FLAGS, "--why", "no longer relevant")
        self.assertEqual(rc, 0, err)
        row = memidx.ref_mark_rows(self.fm())[0]
        self.assertEqual((row["action"], row["authority"], row["why"]), ("remove", "owner-ratified", "no longer relevant"))
        self.assertGuardsClean()


class TestRemoveCodeRefEditor(unittest.TestCase):
    def test_removes_only_the_named_item_and_keeps_every_other_byte(self):
        text = "---\nid: T\ncode_refs:\n  - a.py   # keep\n  - 'b.py'\n  - \"c.py\"\nlinks: []\n---\n"
        out = memidx.remove_code_ref(text, "b.py")
        self.assertEqual(out, "---\nid: T\ncode_refs:\n  - a.py   # keep\n  - \"c.py\"\nlinks: []\n---\n")
        self.assertEqual(memidx.remove_code_ref(out, "a.py"), "---\nid: T\ncode_refs:\n  - \"c.py\"\nlinks: []\n---\n")

    def test_refusals(self):
        for text, needle in (("---\nid: T\nlinks: []\n---\n", "no `code_refs:`"),
                             ("---\nid: T\ncode_refs: [a.py]\n---\n", "flow list"),
                             ("---\nid: T\ncode_refs: []\n---\n", "empty"),
                             ("---\nid: T\ncode_refs:\n  - a.py\n---\n", "not an item")):
            with self.subTest(needle):
                with self.assertRaises(ValueError) as cm:
                    memidx.remove_code_ref(text, "zzz.py")
                self.assertIn(needle, str(cm.exception))


if __name__ == "__main__":
    unittest.main()
